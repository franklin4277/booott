import asyncio
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus, RedisEventBusError
from schemas.messages import ExecutionReport, ExecutionStatus

logger = logging.getLogger(__name__)

EXECUTION_REPORTS_CHANNEL = "execution.reports"
SYSTEM_ALERTS_CHANNEL = "system.alerts"


class OpenTradeLedger:
    def __init__(self) -> None:
        self._trades: dict[str, dict[str, Any]] = {}

    def upsert(self, report: ExecutionReport) -> None:
        trade_id = str(report.execution_id)
        if report.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIALLY_FILLED):
            self._trades[trade_id] = {
                "execution_id": trade_id,
                "order_id": str(report.order_id),
                "status": report.status.value,
                "executed_volume": str(report.executed_volume),
                "fill_price": str(report.fill_price) if report.fill_price is not None else None,
                "broker_order_id": report.broker_order_id,
                "timestamp": report.timestamp.isoformat(),
                "trace_id": report.trace_id,
                "opened_at": datetime.now(UTC).isoformat(),
                "adverse_checks": 0,
            }
        elif report.status in (ExecutionStatus.CANCELLED, ExecutionStatus.REJECTED, ExecutionStatus.FAILED):
            self._trades.pop(trade_id, None)

    def get_open_trades(self) -> list[dict[str, Any]]:
        return [
            {**trade, "opened_at": trade["opened_at"]}
            for trade in self._trades.values()
        ]

    def count(self) -> int:
        return len(self._trades)

    def increment_adverse(self, execution_id: str) -> int:
        trade = self._trades.get(execution_id)
        if trade is None:
            return 0
        trade["adverse_checks"] = trade.get("adverse_checks", 0) + 1
        return trade["adverse_checks"]


class TradeMonitor:
    def __init__(
        self,
        bus: RedisEventBus,
        client: httpx.AsyncClient,
        ledger: OpenTradeLedger,
    ) -> None:
        self.bus = bus
        self.client = client
        self.ledger = ledger
        self.drawdown_warning_pct = Decimal(
            os.environ.get("TELEGRAM_DRAWDOWN_WARNING_PCT", "3.0")
        )
        self.max_age_hours = int(os.environ.get("TRADE_MAX_AGE_HOURS", "24"))
        self.adverse_move_pct = Decimal(
            os.environ.get("TRADE_ADVERSE_MOVE_PCT", "5.0")
        )
        self._poll_interval = int(os.environ.get("TRADE_MONITOR_POLL_SECONDS", "15"))
        self._mt5_url = os.environ.get(
            "MT5_ADAPTER_URL", "http://localhost:8765"
        ).rstrip("/")
        self._adapter_token = os.environ.get("MT5_ADAPTER_TOKEN", "")
        self._last_equity: Decimal | None = None
        self._stop = asyncio.Event()

    async def consume_reports(self) -> None:
        async for event in self.bus.subscribe(EXECUTION_REPORTS_CHANNEL):
            try:
                report = ExecutionReport.model_validate_json(
                    __import__("json").dumps(event.payload)
                )
                self.ledger.upsert(report)
            except (ValidationError, TypeError, ValueError):
                logger.exception("Invalid execution report on %s", EXECUTION_REPORTS_CHANNEL)

    async def surveillance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._check_open_trades()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trade-monitor surveillance iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def _check_open_trades(self) -> None:
        equity = await self._fetch_account_equity()
        if equity is None:
            return
        open_trades = self.ledger.get_open_trades()
        now = datetime.now(UTC)
        for trade in open_trades:
            await self._evaluate_trade(trade, equity, now)

    async def _fetch_account_equity(self) -> Decimal | None:
        try:
            response = await self.client.get(
                f"{self._mt5_url}/v1/account",
                headers={"Authorization": f"Bearer {self._adapter_token}"},
                timeout=httpx.Timeout(5.0, connect=2.0),
            )
            response.raise_for_status()
            data = response.json()
            account = data.get("account", {})
            equity = account.get("equity")
            if equity is None:
                return None
            self._last_equity = Decimal(str(equity))
            return self._last_equity
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _evaluate_trade(self, trade: dict[str, Any], equity: Decimal, now: datetime) -> None:
        execution_id = trade["execution_id"]
        opened_at = datetime.fromisoformat(trade["opened_at"])
        age_hours = (now - opened_at).total_seconds() / 3600

        if age_hours >= self.max_age_hours:
            consecutive = self.ledger.increment_adverse(execution_id)
            if consecutive >= 2:
                await self._publish_alert(
                    execution_id,
                    "trade_max_age",
                    f"Trade {execution_id} has been open for {age_hours:.1f} hours (limit {self.max_age_hours}h).",
                )
        else:
            adverse = self._simulate_adverse_move(trade, equity)
            if adverse >= self.adverse_move_pct:
                consecutive = self.ledger.increment_adverse(execution_id)
                if consecutive >= 2:
                    await self._publish_alert(
                        execution_id,
                        "adverse_move",
                        f"Trade {execution_id} adverse move {adverse:.2f}% exceeds threshold {self.adverse_move_pct}%.",
                    )
            else:
                trade["adverse_checks"] = 0

    def _simulate_adverse_move(self, trade: dict[str, Any], equity: Decimal) -> Decimal:
        return Decimal(0)

    async def _publish_alert(self, execution_id: str, alert_type: str, message: str) -> None:
        try:
            await self.bus.publish(
                SYSTEM_ALERTS_CHANNEL,
                {
                    "event": "trade_alert",
                    "alert_type": alert_type,
                    "execution_id": execution_id,
                    "reason": message,
                    "severity": "warning",
                },
                event_type="TradeStatus",
            )
        except RedisEventBusError:
            logger.exception("Failed to publish trade alert for %s", execution_id)

    async def aclose(self) -> None:
        self._stop.set()
