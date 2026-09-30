from decimal import Decimal
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from schemas.base import StrictModel


class HeartbeatStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class AIRecommendation(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class ExecutionStatus(StrEnum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class HeartbeatMessage(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    component: str = Field(min_length=1, max_length=64)
    status: HeartbeatStatus = HeartbeatStatus.HEALTHY
    timestamp: AwareDatetime
    sequence: int | None = Field(default=None, ge=0)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TickData(StrictModel):
    tick_id: UUID = Field(default_factory=uuid4)
    symbol: str = Field(min_length=1, max_length=32)
    timestamp: AwareDatetime
    bid: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    ask: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    last: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    volume: Decimal | None = Field(default=None, ge=Decimal("0"), allow_inf_nan=False)
    sequence: int | None = Field(default=None, ge=0)
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_quote(self) -> "TickData":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        return self


class BarData(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    timestamp: AwareDatetime
    open: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    high: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    low: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    close: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    tick_volume: int = Field(ge=0)
    spread: int | None = Field(default=None, ge=0)
    real_volume: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_ohlc(self) -> "BarData":
        if self.low > min(self.open, self.close):
            raise ValueError("low must not exceed open or close")
        if self.high < max(self.open, self.close):
            raise ValueError("high must not be below open or close")
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        return self


class SpreadMetrics(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    timestamp: AwareDatetime
    spread: Decimal = Field(ge=Decimal("0"), allow_inf_nan=False)
    spread_ma: Decimal = Field(ge=Decimal("0"), allow_inf_nan=False)
    spread_ratio: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        allow_inf_nan=False,
    )
    sample_count: int = Field(ge=0)


class TechnicalIndicatorSnapshot(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    timestamp: AwareDatetime
    ema: Decimal | None = Field(default=None, allow_inf_nan=False)
    atr: Decimal | None = Field(default=None, ge=Decimal("0"), allow_inf_nan=False)
    rsi: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("100"),
        allow_inf_nan=False,
    )


class PatternSetupSignal(StrictModel):
    signal_id: UUID = Field(default_factory=uuid4)
    strategy_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    side: TradeSide
    entry_price: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    stop_loss: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    take_profit: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    confidence: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("1"),
        allow_inf_nan=False,
    )
    created_at: AwareDatetime
    expires_at: AwareDatetime
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_expiration(self) -> "PatternSetupSignal":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class AIAnalysisResult(StrictModel):
    analysis_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID | None = None
    provider: str = Field(default="unknown", min_length=1, max_length=64)
    model: str = Field(default="unknown", min_length=1, max_length=128)
    decision: AIRecommendation
    confidence_score: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("1"),
        allow_inf_nan=False,
    )
    risk_multiplier: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("1"),
        allow_inf_nan=False,
    )
    reasoning: str = Field(min_length=1, max_length=8000)
    invalidated_by: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class TradeIntent(StrictModel):
    intent_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID | None = None
    symbol: str = Field(min_length=1, max_length=32)
    side: TradeSide
    order_type: OrderType
    volume: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    price: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    created_at: AwareDatetime
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_order_price(self) -> "TradeIntent":
        if self.order_type != OrderType.MARKET and self.price is None:
            raise ValueError("price is required for non-market orders")
        return self


class SignedOrderPayload(StrictModel):
    order_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    symbol: str = Field(min_length=1, max_length=32)
    side: TradeSide
    order_type: OrderType
    volume: Decimal = Field(gt=Decimal("0"), allow_inf_nan=False)
    price: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    issued_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    nonce: UUID = Field(default_factory=uuid4)
    trace_id: str = Field(min_length=1, max_length=128)
    signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_order(self) -> "SignedOrderPayload":
        if self.order_type != OrderType.MARKET and self.price is None:
            raise ValueError("price is required for non-market orders")
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


class ExecutionReport(StrictModel):
    execution_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    status: ExecutionStatus
    executed_volume: Decimal = Field(ge=Decimal("0"), allow_inf_nan=False)
    fill_price: Decimal | None = Field(default=None, gt=Decimal("0"), allow_inf_nan=False)
    broker_order_id: str | None = Field(default=None, max_length=128)
    timestamp: AwareDatetime
    message: str | None = Field(default=None, max_length=2000)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class AccountState(StrictModel):
    account_id: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=1, max_length=16)
    balance: Decimal = Field(allow_inf_nan=False)
    equity: Decimal = Field(allow_inf_nan=False)
    margin: Decimal = Field(ge=Decimal("0"), allow_inf_nan=False)
    free_margin: Decimal = Field(allow_inf_nan=False)
    open_positions: int = Field(ge=0)
    daily_drawdown_percent: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("100"),
        allow_inf_nan=False,
    )
    timestamp: AwareDatetime
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_free_margin(self) -> "AccountState":
        if self.free_margin > self.equity:
            raise ValueError("free_margin must not exceed equity")
        return self
