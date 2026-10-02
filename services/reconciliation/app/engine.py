import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any
from uuid import uuid4

from database.repository import LedgerRepository
from event_bus.redis_bus import RedisEventBus
from schemas.messages import AccountState, TradeSide
from services.reconciliation.app.models import (
    ReconciliationReport,
    SafeModeState,
)
from services.risk_engine.app.models import PortfolioSnapshot, PositionExposure
from services.reconciliation.app.mt5_client import (
    MT5AdapterClient,
    MT5AdapterUnavailable,
)
from services.reconciliation.app.notifier import CriticalNotifier

logger = logging.getLogger(__name__)
SAFE_MODE_KEY = "system:safe_mode"
SAFE_MODE_CHANNEL = "system.safe_mode"


class ReconciliationEngine:
    def __init__(
        self,
        repository: LedgerRepository,
        mt5: MT5AdapterClient,
        bus: RedisEventBus,
        notifier: CriticalNotifier,
        *,
        poll_seconds: float | None = None,
        manual_token: str | None = None,
    ) -> None:
        self.repository = repository
        self.mt5 = mt5
        self.bus = bus
        self.notifier = notifier
        self.poll_seconds = poll_seconds or float(
            os.environ.get("RECONCILIATION_INTERVAL_SECONDS", "15")
        )
        if self.poll_seconds < 1:
            raise ValueError("RECONCILIATION_INTERVAL_SECONDS must be at least 1")
        self.manual_token = manual_token or os.environ.get(
            "RECONCILIATION_API_TOKEN", ""
        )
        if len(self.manual_token.encode("utf-8")) < 32:
            raise ValueError("RECONCILIATION_API_TOKEN must contain at least 32 bytes")
        self.state = SafeModeState(
            enabled=True,
            reason="Initial MT5 reconciliation is pending.",
            manual_clear_required=False,
            updated_at=datetime.now(timezone.utc),
        )
        self.last_reconciliation: ReconciliationReport | None = None
        self.last_snapshot_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_reconciliation_failed = False
        self._last_closed_deal_query: datetime | None = None
        self._last_unknown_position_count = 0
        self._last_unknown_position_ids: set[int] = set()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        persisted = await self.repository.get_safe_mode()
        redis_state = await self.bus.get_state(SAFE_MODE_KEY)
        value = persisted or redis_state
        if value is not None:
            self.state = SafeModeState.model_validate_json(json.dumps(value))
            if self.state.enabled:
                await self._publish_safe_mode(
                    self.state,
                    event_reason="persisted_state_restored",
                )
        else:
            await self.repository.update_safe_mode(
                self.state.model_dump(mode="json"), self.state.updated_at
            )
            await self._publish_safe_mode(
                self.state,
                event_reason="initial_reconciliation_pending",
            )
            return
        if not self.state.enabled:
            self.state = self.state.model_copy(
                update={
                    "enabled": True,
                    "reason": "Initial MT5 reconciliation is pending.",
                    "manual_clear_required": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await self.repository.update_safe_mode(
                self.state.model_dump(mode="json"), self.state.updated_at
            )
            await self._publish_safe_mode(
                self.state,
                event_reason="initial_reconciliation_pending",
            )

    def authenticate_manual_request(self, token: str | None) -> bool:
        return bool(token) and compare_digest(token, self.manual_token)

    async def run_once(self, trigger: str = "manual") -> ReconciliationReport:
        async with self._lock:
            previous_safe_mode = self.state.enabled
            since = self._last_closed_deal_query
            try:
                oldest_opened_at = await self.repository.oldest_open_trade_time()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._enter_safe_mode(
                    "PostgreSQL ledger is unavailable; broker state cannot be reconciled.",
                    manual_clear_required=False,
                    alert=True,
                )
                raise
            if oldest_opened_at is not None and (
                since is None or oldest_opened_at < since
            ):
                since = oldest_opened_at
            if since is None:
                since = datetime.now(timezone.utc) - timedelta(days=30)
            try:
                snapshot = await self.mt5.get_snapshot(since)
            except MT5AdapterUnavailable:
                await self._enter_safe_mode(
                    "MT5 state adapter is unavailable; broker state cannot be verified.",
                    manual_clear_required=False,
                    alert=True,
                )
                raise
            reconciled_at = datetime.now(timezone.utc)
            proposed_state = self.state.model_copy(
                update={
                    "enabled": False,
                    "reason": None,
                    "manual_clear_required": False,
                    "updated_at": reconciled_at,
                }
            )
            try:
                outcome = await self.repository.apply_snapshot(
                    snapshot,
                    proposed_safe_mode=proposed_state.model_dump(mode="json"),
                    reconciled_at=reconciled_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._enter_safe_mode(
                    "PostgreSQL ledger update failed; trading remains fail-closed.",
                    manual_clear_required=False,
                    alert=True,
                )
                raise
            self.last_snapshot_at = snapshot.fetched_at
            self._last_success_at = reconciled_at
            self._last_reconciliation_failed = False
            self._last_closed_deal_query = snapshot.fetched_at
            unknown_ids = {item.identifier for item in outcome.unknown_positions}
            newly_unknown_ids = unknown_ids - self._last_unknown_position_ids
            self._last_unknown_position_count = len(unknown_ids)
            self._last_unknown_position_ids = unknown_ids
            if outcome.unknown_positions:
                reason = (
                    "Unrecorded MT5 open position(s): "
                    + ", ".join(
                        f"{position.identifier} ({position.symbol})"
                        for position in outcome.unknown_positions
                    )
                )
                next_state = SafeModeState(
                    enabled=True,
                    reason=reason,
                    manual_clear_required=True,
                    updated_at=reconciled_at,
                )
            elif self.state.manual_clear_required:
                next_state = self.state.model_copy(
                    update={"enabled": True, "updated_at": reconciled_at}
                )
            else:
                next_state = proposed_state
            self.state = next_state
            try:
                await self._publish_portfolio_snapshot(snapshot)
            except Exception:
                await self._enter_safe_mode(
                    "Current account and portfolio risk state could not be published; trading remains fail-closed.",
                    manual_clear_required=False,
                    alert=True,
                )
                raise
            if previous_safe_mode != self.state.enabled or outcome.unknown_positions:
                await self._publish_safe_mode(
                    self.state,
                    event_reason=(
                        "unknown_open_position"
                        if outcome.unknown_positions
                        else "broker_state_reconciled"
                    ),
                )
            report = ReconciliationReport(
                trigger=trigger,
                account_id=snapshot.account.login,
                positions_seen=len(snapshot.positions),
                pending_orders_seen=len(snapshot.pending_orders),
                closed_trades_reconciled=outcome.closed_trades_reconciled,
                unknown_position_tickets=[
                    position.identifier for position in outcome.unknown_positions
                ],
                safe_mode=self.state.enabled,
                reconciled_at=reconciled_at,
            )
            self.last_reconciliation = report
            if newly_unknown_ids:
                newly_unknown_positions = [
                    position
                    for position in outcome.unknown_positions
                    if position.identifier in newly_unknown_ids
                ]
                details = ", ".join(
                        f"{position.identifier} ({position.symbol})"
                        for position in newly_unknown_positions
                    )
                await self._send_critical_alert(
                    summary="Unknown open position detected in MT5; trading set to SAFE_MODE.",
                    details=details,
                    dedup_key="mt5-unknown-position-"
                    + "-".join(str(item) for item in sorted(unknown_ids)),
                    alert_payload={
                        "severity": "critical",
                        "event": "UNKNOWN_MT5_OPEN_POSITION",
                        "position_tickets": sorted(unknown_ids),
                        "reason": reason,
                        "occurred_at": reconciled_at.isoformat(),
                    },
                )
            if outcome.unknown_pending_orders:
                logger.error(
                    "Unknown MT5 pending orders found: %s",
                    outcome.unknown_pending_orders,
                )
                await self.bus.publish(
                    "system.alerts",
                    {
                        "severity": "high",
                        "event": "UNKNOWN_PENDING_ORDERS",
                        "tickets": outcome.unknown_pending_orders,
                        "occurred_at": reconciled_at.isoformat(),
                    },
                    trace_id=uuid4().hex,
                    event_type="ReconciliationAlert",
                )
            logger.info(
                "MT5 reconciliation completed trigger=%s account=%s positions=%d unknown=%d closed=%d",
                trigger,
                report.account_id,
                report.positions_seen,
                len(outcome.unknown_positions),
                report.closed_trades_reconciled,
            )
            return report

    async def _publish_portfolio_snapshot(self, snapshot) -> None:
        starting_equity = await self.repository.get_daily_starting_equity(
            snapshot.account.login
        )
        if starting_equity is None or starting_equity <= 0:
            raise RuntimeError(
                "The reconciled account has no positive daily equity baseline."
            )
        account = AccountState(
            account_id=snapshot.account.login,
            currency=snapshot.account.currency,
            balance=snapshot.account.balance,
            equity=snapshot.account.equity,
            margin=snapshot.account.margin,
            free_margin=snapshot.account.free_margin,
            open_positions=len(snapshot.positions),
            timestamp=snapshot.account.timestamp,
        )
        positions = [
            PositionExposure(
                position_id=str(position.identifier),
                source_order_id=position.client_order_id,
                symbol=position.symbol,
                side=TradeSide(position.side),
                risk_amount=(
                    position.risk_amount
                    if position.risk_amount is not None
                    else snapshot.account.equity
                ),
            )
            for position in snapshot.positions
        ]
        portfolio = PortfolioSnapshot(
            account=account,
            daily_starting_equity=starting_equity,
            positions=positions,
        )
        await self.bus.publish(
            "risk.portfolio",
            portfolio,
            trace_id=uuid4().hex,
            event_type="PortfolioSnapshot",
        )

    async def clear_safe_mode(self) -> SafeModeState:
        async with self._lock:
            if self._last_success_at is None or (
                datetime.now(timezone.utc) - self._last_success_at
            ).total_seconds() > self.poll_seconds * 2:
                raise RuntimeError(
                    "Cannot clear SAFE_MODE without a recent successful reconciliation."
                )
            if self._last_reconciliation_failed:
                raise RuntimeError(
                    "Cannot clear SAFE_MODE after a failed reconciliation; reconcile successfully first."
                )
            if self._last_unknown_position_count:
                raise RuntimeError(
                    "Cannot clear SAFE_MODE while unknown MT5 positions remain."
                )
            self.state = SafeModeState(
                enabled=False,
                reason=None,
                manual_clear_required=False,
                updated_at=datetime.now(timezone.utc),
            )
            await self.repository.update_safe_mode(
                self.state.model_dump(mode="json"), self.state.updated_at
            )
            await self._publish_safe_mode(
                self.state, event_reason="manual_safe_mode_clear"
            )
            return self.state

    async def activate_safe_mode(self, reason: str) -> SafeModeState:
        if not reason.strip():
            raise ValueError("SAFE_MODE activation reason must not be empty")
        async with self._lock:
            now = datetime.now(timezone.utc)
            self.state = SafeModeState(
                enabled=True,
                reason=reason.strip()[:1000],
                manual_clear_required=True,
                updated_at=now,
            )
            await self._publish_safe_mode(
                self.state, event_reason="operator_emergency_kill"
            )
            await self.repository.update_safe_mode(
                self.state.model_dump(mode="json"), now
            )
            await self._send_critical_alert(
                summary="Operator activated the MT5 trading SAFE_MODE.",
                details=self.state.reason or "No reason provided.",
                dedup_key="operator-emergency-safe-mode",
                alert_payload={
                    "severity": "critical",
                    "event": "OPERATOR_SAFE_MODE",
                    "reason": self.state.reason,
                    "occurred_at": now.isoformat(),
                },
            )
            return self.state

    async def run_forever(self) -> None:
        trigger = "startup"
        while True:
            try:
                await self.run_once(trigger)
                trigger = "poll"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled MT5 reconciliation failed")
                trigger = "connection_recovery"
            await asyncio.sleep(self.poll_seconds)

    async def _enter_safe_mode(
        self,
        reason: str,
        *,
        manual_clear_required: bool,
        alert: bool,
    ) -> None:
        previous = self.state
        now = datetime.now(timezone.utc)
        self._last_reconciliation_failed = True
        self.state = SafeModeState(
            enabled=True,
            reason=reason,
            manual_clear_required=(
                manual_clear_required or previous.manual_clear_required
            ),
            updated_at=now,
        )
        failures: list[Exception] = []
        for persist_state in (
            self._publish_safe_mode(
                self.state, event_reason="broker_state_unavailable"
            ),
            self.repository.update_safe_mode(
                self.state.model_dump(mode="json"), now
            ),
        ):
            try:
                await persist_state
            except Exception as exc:
                failures.append(exc)
                logger.exception("Could not persist or publish SAFE_MODE state")
        if alert and (not previous.enabled or previous.reason != reason):
            await self._send_critical_alert(
                summary="MT5 broker state cannot be verified; trading set to SAFE_MODE.",
                details=reason,
                dedup_key="mt5-broker-state-unavailable",
                alert_payload={
                    "severity": "critical",
                    "event": "MT5_STATE_UNAVAILABLE",
                    "reason": reason,
                    "occurred_at": now.isoformat(),
                },
            )
        if failures:
            raise RuntimeError(
                "SAFE_MODE was activated in memory, but one or more persistence channels failed."
            ) from failures[0]

    async def _send_critical_alert(
        self,
        *,
        summary: str,
        details: str,
        dedup_key: str,
        alert_payload: dict[str, Any],
    ) -> None:
        outcomes = await asyncio.gather(
            self.bus.publish(
                "system.alerts",
                alert_payload,
                trace_id=uuid4().hex,
                event_type="CriticalReconciliationAlert",
            ),
            self.notifier.send_critical(
                summary,
                details,
                dedup_key=dedup_key,
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.error(
                    "Critical reconciliation alert delivery failed",
                    exc_info=(type(outcome), outcome, outcome.__traceback__),
                )

    async def _publish_safe_mode(
        self,
        state: SafeModeState,
        *,
        event_reason: str,
    ) -> None:
        values: dict[str, Any] = {
            **state.model_dump(mode="json"),
            "event_reason": event_reason,
        }
        await self.bus.set_state(
            SAFE_MODE_KEY,
            state.model_dump(mode="json"),
        )
        await self.bus.publish(
            SAFE_MODE_CHANNEL,
            values,
            trace_id=uuid4().hex,
            event_type="SafeModeState",
        )
