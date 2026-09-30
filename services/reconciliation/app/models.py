from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, Field

from schemas.base import StrictModel


class MT5AccountSnapshot(StrictModel):
    login: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=1, max_length=16)
    balance: Decimal = Field(allow_inf_nan=False)
    equity: Decimal = Field(allow_inf_nan=False)
    margin: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    free_margin: Decimal = Field(allow_inf_nan=False)
    timestamp: AwareDatetime


class MT5Position(StrictModel):
    ticket: int = Field(gt=0)
    identifier: int = Field(gt=0)
    client_order_id: UUID | None = None
    symbol: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern=r"^(buy|sell)$")
    volume: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    price_open: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    stop_loss: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    take_profit: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    profit: Decimal = Field(allow_inf_nan=False)
    swap: Decimal = Field(allow_inf_nan=False)
    opened_at: AwareDatetime


class MT5PendingOrder(StrictModel):
    ticket: int = Field(gt=0)
    client_order_id: UUID | None = None
    symbol: str = Field(min_length=1, max_length=32)
    order_type: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern=r"^(buy|sell)$")
    volume: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    price_open: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    stop_loss: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    take_profit: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    created_at: AwareDatetime


class MT5ClosedDeal(StrictModel):
    ticket: int = Field(gt=0)
    position_id: int = Field(gt=0)
    symbol: str = Field(min_length=1, max_length=32)
    timestamp: AwareDatetime
    price: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    volume: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    profit: Decimal = Field(allow_inf_nan=False)
    commission: Decimal = Field(allow_inf_nan=False)
    swap: Decimal = Field(allow_inf_nan=False)
    fee: Decimal = Field(allow_inf_nan=False)


class MT5Snapshot(StrictModel):
    account: MT5AccountSnapshot
    positions: list[MT5Position]
    pending_orders: list[MT5PendingOrder]
    closed_deals: list[MT5ClosedDeal]
    fetched_at: AwareDatetime
    host_cpu_percent: Decimal | None = Field(
        default=None, ge=Decimal(0), le=Decimal(100), allow_inf_nan=False
    )
    host_memory_percent: Decimal | None = Field(
        default=None, ge=Decimal(0), le=Decimal(100), allow_inf_nan=False
    )


class MT5HostStatus(StrictModel):
    account: MT5AccountSnapshot
    host_cpu_percent: Decimal | None = Field(
        default=None, ge=Decimal(0), le=Decimal(100), allow_inf_nan=False
    )
    host_memory_percent: Decimal | None = Field(
        default=None, ge=Decimal(0), le=Decimal(100), allow_inf_nan=False
    )
    fetched_at: AwareDatetime


class ReconciliationReport(StrictModel):
    trigger: str
    account_id: str
    positions_seen: int = Field(ge=0)
    pending_orders_seen: int = Field(ge=0)
    closed_trades_reconciled: int = Field(ge=0)
    unknown_position_tickets: list[int] = Field(default_factory=list)
    safe_mode: bool
    reconciled_at: AwareDatetime


class SafeModeState(StrictModel):
    enabled: bool
    reason: str | None = None
    manual_clear_required: bool = False
    updated_at: AwareDatetime


class ReconciliationRequest(StrictModel):
    trigger: str = Field(default="manual", min_length=1, max_length=64)
