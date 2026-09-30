"""Create account, signal, trade, audit and system-state ledger tables."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260930_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("account_id", sa.String(length=128), primary_key=True),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("balance", sa.Numeric(24, 8), nullable=False),
        sa.Column("equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("margin", sa.Numeric(24, 8), nullable=False),
        sa.Column("free_margin", sa.Numeric(24, 8), nullable=False),
        sa.Column("peak_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("daily_starting_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "signals",
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(length=128)),
        sa.Column("market_data", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("ai_analysis", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("trade_intent", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), unique=True),
        sa.Column("status", sa.String(length=24), nullable=False),
    )
    op.create_index(
        "ix_signals_symbol_created_at", "signals", ["symbol", "created_at"]
    )
    op.create_table(
        "trades",
        sa.Column("trade_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket", sa.BigInteger()),
        sa.Column("mt5_ticket", sa.BigInteger()),
        sa.Column("order_ticket", sa.BigInteger()),
        sa.Column("client_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("broker_order_id", sa.String(length=128)),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.signal_id")),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("lot", sa.Numeric(20, 8), nullable=False),
        sa.Column("initial_lot", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10)),
        sa.Column("exit_price", sa.Numeric(24, 10)),
        sa.Column("stop_loss", sa.Numeric(24, 10)),
        sa.Column("take_profit", sa.Numeric(24, 10)),
        sa.Column("pnl", sa.Numeric(24, 8)),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("closed_volume", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "mt5_deal_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("commission", sa.Numeric(24, 8), nullable=False),
        sa.Column("swap", sa.Numeric(24, 8), nullable=False),
        sa.Column("fees", sa.Numeric(24, 8), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("ticket", name="uq_trades_ticket"),
        sa.UniqueConstraint("client_order_id", name="uq_trades_client_order_id"),
    )
    op.create_index("ix_trades_status_symbol", "trades", ["status", "symbol"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128)),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_trace_id", "audit_logs", ["trace_id"])
    op.create_index(
        "ix_audit_logs_entity",
        "audit_logs",
        ["entity_type", "entity_id", "occurred_at"],
    )
    op.create_index("ix_audit_logs_occurred_at", "audit_logs", ["occurred_at"])
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("system_state")
    op.drop_index("ix_audit_logs_occurred_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_trace_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_trades_status_symbol", table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_signals_symbol_created_at", table_name="signals")
    op.drop_table("signals")
    op.drop_table("accounts")
