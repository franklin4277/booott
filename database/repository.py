import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from database.models import Account, AuditLog, Signal, SystemState, Trade
from schemas.events import EventEnvelope
from schemas.messages import (
    AIAnalysisResult,
    ExecutionReport,
    ExecutionStatus,
    PatternSetupSignal,
    SignedOrderPayload,
)
from services.reconciliation.app.models import (
    MT5AccountSnapshot,
    MT5Position,
    MT5Snapshot,
)

logger = logging.getLogger(__name__)
OPEN_TRADE_STATUSES = {
    "APPROVED",
    "ACCEPTED",
    "PENDING",
    "OPEN",
    "PARTIALLY_FILLED",
    "PARTIALLY_CLOSED",
}


@dataclass(frozen=True)
class ReconciliationOutcome:
    unknown_positions: list[MT5Position]
    unknown_pending_orders: list[int]
    closed_trades_reconciled: int


class LedgerRepository:
    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker | None = None,
    ) -> None:
        self.engine = engine
        self.sessions = sessions or async_sessionmaker(engine, expire_on_commit=False)

    async def record_event(self, event: EventEnvelope) -> None:
        payload = event.payload
        async with self.sessions.begin() as session:
            audit = insert(AuditLog).values(
                event_id=event.event_id,
                trace_id=event.trace_id,
                event_type=event.event_type,
                entity_type=self._entity_type(event.event_type),
                entity_id=self._entity_id(payload),
                payload=payload,
                occurred_at=event.occurred_at,
            ).on_conflict_do_nothing(index_elements=["event_id"])
            result = await session.execute(audit)
            if result.rowcount == 0:
                return
            if event.event_type == "PatternSetupSignal":
                signal = PatternSetupSignal.model_validate_json(json.dumps(payload))
                statement = insert(Signal).values(
                    signal_id=signal.signal_id,
                    strategy_id=signal.strategy_id,
                    symbol=signal.symbol,
                    timeframe=signal.timeframe,
                    side=signal.side.value,
                    created_at=signal.created_at,
                    expires_at=signal.expires_at,
                    trace_id=event.trace_id,
                    market_data=signal.model_dump(mode="json"),
                    status="SIGNAL",
                ).on_conflict_do_nothing(index_elements=["signal_id"])
                await session.execute(statement)
                existing_ai = await session.execute(
                    select(AuditLog.payload)
                    .where(
                        AuditLog.event_type == "AIAnalysisResult",
                        AuditLog.entity_id == str(signal.signal_id),
                    )
                    .order_by(AuditLog.occurred_at.desc())
                    .limit(1)
                )
                ai_payload = existing_ai.scalar_one_or_none()
                if ai_payload is not None:
                    await session.execute(
                        Signal.__table__.update()
                        .where(Signal.signal_id == signal.signal_id)
                        .values(ai_analysis=ai_payload)
                    )
                await session.execute(
                    Trade.__table__.update()
                    .where(
                        Trade.intent_id == signal.signal_id,
                        Trade.signal_id.is_(None),
                    )
                    .values(signal_id=signal.signal_id)
                )
                linked_trade = await session.execute(
                    select(Trade.client_order_id, Trade.status)
                    .where(Trade.intent_id == signal.signal_id)
                    .order_by(Trade.updated_at.desc())
                    .limit(1)
                )
                linked_order = linked_trade.one_or_none()
                if linked_order is not None:
                    await session.execute(
                        Signal.__table__.update()
                        .where(Signal.signal_id == signal.signal_id)
                        .values(
                            order_id=linked_order.client_order_id,
                            status=linked_order.status,
                        )
                    )
                elif ai_payload is not None:
                    await session.execute(
                        Signal.__table__.update()
                        .where(Signal.signal_id == signal.signal_id)
                        .values(status="AI_ANALYZED")
                    )
            elif event.event_type == "AIAnalysisResult":
                analysis = AIAnalysisResult.model_validate_json(json.dumps(payload))
                if analysis.signal_id is not None:
                    await session.execute(
                        Signal.__table__.update()
                        .where(Signal.signal_id == analysis.signal_id)
                        .values(ai_analysis=analysis.model_dump(mode="json"))
                    )
            elif event.event_type == "SignedOrderPayload":
                order = SignedOrderPayload.model_validate_json(json.dumps(payload))
                signal_result = await session.execute(
                    select(Signal).where(Signal.signal_id == order.intent_id)
                )
                signal = signal_result.scalar_one_or_none()
                if signal is not None:
                    signal.trade_intent = order.model_dump(mode="json")
                    signal.order_id = order.order_id
                    signal.status = "APPROVED"
                statement = insert(Trade).values(
                    client_order_id=order.order_id,
                    intent_id=order.intent_id,
                    signal_id=order.intent_id if signal is not None else None,
                    symbol=order.symbol,
                    side=order.side.value,
                    lot=order.volume,
                    initial_lot=order.volume,
                    entry_price=order.price,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    status="APPROVED",
                    opened_at=order.issued_at,
                ).on_conflict_do_nothing(index_elements=["client_order_id"])
                await session.execute(statement)
                trade_result = await session.execute(
                    select(Trade).where(Trade.client_order_id == order.order_id)
                )
                trade = trade_result.scalar_one_or_none()
                previous_execution = await session.execute(
                    select(AuditLog.payload)
                    .where(
                        AuditLog.event_type == "ExecutionReport",
                        AuditLog.entity_id == str(order.order_id),
                    )
                    .order_by(AuditLog.occurred_at.desc())
                    .limit(1)
                )
                execution_payload = previous_execution.scalar_one_or_none()
                if trade is not None and execution_payload is not None:
                    report = ExecutionReport.model_validate_json(
                        json.dumps(execution_payload)
                    )
                    self._apply_execution_report(trade, report)
                    await self._sync_signal_status(session, trade)
            elif event.event_type == "ExecutionReport":
                report = ExecutionReport.model_validate_json(json.dumps(payload))
                trade_result = await session.execute(
                    select(Trade).where(Trade.client_order_id == report.order_id)
                )
                trade = trade_result.scalar_one_or_none()
                if trade is not None:
                    self._apply_execution_report(trade, report)
                    await self._sync_signal_status(session, trade)

    async def apply_snapshot(
        self,
        snapshot: MT5Snapshot,
        *,
        proposed_safe_mode: dict[str, Any],
        reconciled_at: datetime,
    ) -> ReconciliationOutcome:
        async with self.sessions.begin() as session:
            account = snapshot.account
            result = await session.execute(
                select(Account).where(Account.account_id == account.login)
            )
            stored_account = result.scalar_one_or_none()
            if stored_account is None:
                stored_account = Account(
                    account_id=account.login,
                    currency=account.currency,
                    balance=account.balance,
                    equity=account.equity,
                    margin=account.margin,
                    free_margin=account.free_margin,
                    peak_equity=account.equity,
                    daily_starting_equity=account.equity,
                    updated_at=account.timestamp,
                )
                session.add(stored_account)
            else:
                stored_account.currency = account.currency
                stored_account.balance = account.balance
                stored_account.equity = account.equity
                stored_account.margin = account.margin
                stored_account.free_margin = account.free_margin
                stored_account.peak_equity = max(
                    stored_account.peak_equity, account.equity
                )
                if stored_account.updated_at.date() < account.timestamp.date():
                    stored_account.daily_starting_equity = account.equity
                stored_account.updated_at = account.timestamp

            unknown_positions, unknown_pending, closed_count = (
                await self._reconcile_positions(session, snapshot)
            )
            current_state_result = await session.execute(
                select(SystemState.value).where(SystemState.key == "safe_mode")
            )
            current_state = current_state_result.scalar_one_or_none() or {}
            if unknown_positions:
                state = {
                    "enabled": True,
                    "reason": (
                        "Unrecorded MT5 open position(s): "
                        + ", ".join(str(item.identifier) for item in unknown_positions)
                    ),
                    "manual_clear_required": True,
                    "updated_at": reconciled_at.isoformat(),
                }
            elif current_state.get("manual_clear_required"):
                state = {**current_state, "enabled": True}
                state["updated_at"] = reconciled_at.isoformat()
            else:
                state = proposed_safe_mode
            statement = insert(SystemState).values(
                key="safe_mode", value=state, updated_at=reconciled_at
            ).on_conflict_do_update(
                index_elements=["key"],
                set_={"value": state, "updated_at": reconciled_at},
            )
            await session.execute(statement)
        return ReconciliationOutcome(
            unknown_positions=unknown_positions,
            unknown_pending_orders=unknown_pending,
            closed_trades_reconciled=closed_count,
        )

    async def oldest_open_trade_time(self) -> datetime | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(Trade.opened_at)
                .where(
                    Trade.status.in_(OPEN_TRADE_STATUSES),
                    or_(
                        Trade.ticket.is_not(None),
                        Trade.order_ticket.is_not(None),
                    ),
                    Trade.opened_at.is_not(None),
                )
                .order_by(Trade.opened_at.asc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def update_safe_mode(self, value: dict[str, Any], at: datetime) -> None:
        statement = insert(SystemState).values(
            key="safe_mode", value=value, updated_at=at
        ).on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value, "updated_at": at},
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def get_safe_mode(self) -> dict[str, Any] | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(SystemState.value).where(SystemState.key == "safe_mode")
            )
            return result.scalar_one_or_none()

    async def get_daily_starting_equity(self, account_id: str) -> Decimal | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(Account.daily_starting_equity).where(
                    Account.account_id == account_id
                )
            )
            return result.scalar_one_or_none()

    async def _reconcile_positions(
        self, session, snapshot: MT5Snapshot
    ) -> tuple[list[MT5Position], list[int], int]:
        positions_by_identifier = {
            position.identifier: position for position in snapshot.positions
        }
        positions_by_ticket = {position.ticket: position for position in snapshot.positions}
        pending_by_ticket = {order.ticket: order for order in snapshot.pending_orders}
        trades_result = await session.execute(select(Trade))
        trades = list(trades_result.scalars())
        matched_position_ids: set[int] = set()

        closed_count = 0
        for trade in trades:
            if trade.signal_id is None and trade.intent_id is not None:
                signal = await session.get(Signal, trade.intent_id)
                if signal is not None:
                    trade.signal_id = signal.signal_id
                    if signal.order_id is None:
                        signal.order_id = trade.client_order_id
                        signal.status = trade.status
            position = (
                positions_by_identifier.get(trade.ticket)
                if trade.ticket is not None
                else None
            ) or (
                positions_by_ticket.get(trade.mt5_ticket)
                if trade.mt5_ticket is not None
                else None
            ) or (
                positions_by_ticket.get(trade.order_ticket)
                if trade.order_ticket is not None
                else None
            )
            if trade.client_order_id is not None and position is None:
                position = next(
                    (
                        candidate
                        for candidate in snapshot.positions
                        if candidate.client_order_id == trade.client_order_id
                    ),
                    None,
                )
            if position is not None:
                self._reconcile_closed_trade(
                    trade,
                    snapshot,
                    position.identifier,
                )
                self._update_open_trade(trade, position)
                matched_position_ids.add(position.identifier)
                continue

            pending = (
                pending_by_ticket.get(trade.order_ticket)
                if trade.order_ticket is not None
                else None
            )
            if pending is None and trade.client_order_id is not None:
                pending = next(
                    (
                        candidate
                        for candidate in snapshot.pending_orders
                        if candidate.client_order_id == trade.client_order_id
                    ),
                    None,
                )
            if pending is not None:
                trade.order_ticket = pending.ticket
                trade.status = "PENDING"
                trade.symbol = pending.symbol
                trade.lot = pending.volume
                trade.initial_lot = max(trade.initial_lot, pending.volume)
                trade.stop_loss = pending.stop_loss
                trade.take_profit = pending.take_profit
                continue
            if (
                trade.ticket is not None or trade.order_ticket is not None
            ) and trade.status in OPEN_TRADE_STATUSES:
                position_id = trade.ticket or trade.order_ticket
                if self._reconcile_closed_trade(trade, snapshot, position_id):
                    closed_count += 1

        unknown_positions = [
            position
            for position in snapshot.positions
            if position.identifier not in matched_position_ids
        ]
        known_order_tickets = {
            trade.order_ticket for trade in trades if trade.order_ticket is not None
        }
        known_client_order_ids = {
            trade.client_order_id for trade in trades if trade.client_order_id is not None
        }
        unknown_pending = [
            order.ticket
            for order in snapshot.pending_orders
            if order.ticket not in known_order_tickets
            and order.client_order_id not in known_client_order_ids
        ]
        for position in unknown_positions:
            logger.critical(
                "Unknown MT5 open position found during ledger reconciliation ticket=%s symbol=%s",
                position.identifier,
                position.symbol,
            )
        return unknown_positions, unknown_pending, closed_count

    @staticmethod
    def _apply_execution_report(trade: Trade, report: ExecutionReport) -> None:
        if report.broker_order_id:
            trade.broker_order_id = report.broker_order_id
            try:
                trade.order_ticket = int(report.broker_order_id)
            except ValueError:
                logger.warning(
                    "Non-numeric broker order ID recorded for order %s",
                    report.order_id,
                )
        if report.status == ExecutionStatus.FILLED:
            trade.status = "OPEN"
            trade.lot = report.executed_volume
            trade.entry_price = report.fill_price or trade.entry_price
        elif report.status == ExecutionStatus.PARTIALLY_FILLED:
            trade.status = "PARTIALLY_FILLED"
            trade.lot = report.executed_volume
            trade.entry_price = report.fill_price or trade.entry_price
        elif report.status == ExecutionStatus.ACCEPTED:
            trade.status = "ACCEPTED"
        else:
            trade.status = report.status.value.upper()

    @staticmethod
    async def _sync_signal_status(session, trade: Trade) -> None:
        if trade.signal_id is not None:
            await session.execute(
                Signal.__table__.update()
                .where(Signal.signal_id == trade.signal_id)
                .values(status=trade.status)
            )

    @staticmethod
    def _update_open_trade(trade: Trade, position: MT5Position) -> None:
        trade.ticket = position.identifier
        trade.mt5_ticket = position.ticket
        trade.symbol = position.symbol
        trade.side = position.side
        trade.lot = position.volume
        trade.initial_lot = position.volume + trade.closed_volume
        trade.entry_price = position.price_open
        trade.stop_loss = position.stop_loss
        trade.take_profit = position.take_profit
        trade.pnl = trade.realized_pnl + position.profit + position.swap
        trade.status = "OPEN"
        trade.opened_at = position.opened_at

    @staticmethod
    def _reconcile_closed_trade(
        trade: Trade, snapshot: MT5Snapshot, position_id: int
    ) -> bool:
        close_deals = [
            deal
            for deal in snapshot.closed_deals
            if deal.position_id == position_id
            and deal.ticket not in trade.mt5_deal_ids
        ]
        if not close_deals:
            return False
        incremental_volume = sum((deal.volume for deal in close_deals), Decimal(0))
        prior_closed_volume = trade.closed_volume
        closed_volume = prior_closed_volume + incremental_volume
        prior_exit_value = (
            trade.exit_price * prior_closed_volume
            if trade.exit_price is not None
            else Decimal(0)
        )
        incremental_exit_value = sum(
            (deal.price * deal.volume for deal in close_deals), Decimal(0)
        )
        trade.closed_volume = closed_volume
        trade.mt5_deal_ids = [
            *trade.mt5_deal_ids,
            *(deal.ticket for deal in close_deals),
        ]
        trade.exit_price = (
            (prior_exit_value + incremental_exit_value) / closed_volume
            if closed_volume > 0
            else None
        )
        trade.realized_pnl += sum(
            (deal.profit for deal in close_deals), Decimal(0)
        )
        trade.pnl = trade.realized_pnl
        trade.commission += sum(
            (deal.commission for deal in close_deals), Decimal(0)
        )
        trade.swap += sum((deal.swap for deal in close_deals), Decimal(0))
        trade.fees += sum((deal.fee for deal in close_deals), Decimal(0))
        trade.closed_at = max(
            [deal.timestamp for deal in close_deals]
            + ([trade.closed_at] if trade.closed_at is not None else [])
        )
        if closed_volume < trade.initial_lot - Decimal("0.00000001"):
            trade.status = "PARTIALLY_CLOSED"
            return False
        trade.status = "CLOSED"
        return True

    @staticmethod
    def _entity_type(event_type: str) -> str:
        return {
            "PatternSetupSignal": "signal",
            "AIAnalysisResult": "signal",
            "SignedOrderPayload": "order",
            "ExecutionReport": "order",
            "TickData": "market_data",
            "BarData": "market_data",
            "SafeModeState": "system_state",
        }.get(event_type, "event")

    @staticmethod
    def _entity_id(payload: dict[str, Any]) -> str | None:
        for key in ("signal_id", "order_id", "intent_id", "tick_id"):
            value = payload.get(key)
            if value is not None:
                return str(value)
        if "enabled" in payload:
            return "safe_mode"
        return None

    async def aclose(self) -> None:
        await self.engine.dispose()
