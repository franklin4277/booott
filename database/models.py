from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON().with_variant(JSONB, "postgresql")}


class Account(Base):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    margin: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    free_margin: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    daily_starting_equity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_symbol_created_at", "symbol", "created_at"),)

    signal_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str | None] = mapped_column(String(128))
    market_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    ai_analysis: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    trade_intent: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    order_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), unique=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="SIGNAL")
    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("ticket", name="uq_trades_ticket"),
        UniqueConstraint("client_order_id", name="uq_trades_client_order_id"),
        Index("ix_trades_status_symbol", "status", "symbol"),
    )

    trade_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    ticket: Mapped[int | None] = mapped_column(BigInteger)
    mt5_ticket: Mapped[int | None] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    client_order_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    intent_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    signal_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("signals.signal_id")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    lot: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    initial_lot: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal(0)
    )
    closed_volume: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal(0)
    )
    mt5_deal_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
    )
    commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal(0)
    )
    swap: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal(0)
    )
    fees: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal(0)
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    signal: Mapped[Signal | None] = relationship(back_populates="trades")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_trace_id", "trace_id"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_logs_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False, unique=True
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
