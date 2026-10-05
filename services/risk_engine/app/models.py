from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from schemas.base import StrictModel
from schemas.messages import AccountState, TradeSide


class RiskRejectionCode(StrEnum):
    INVALID_SIGNAL = "INVALID_SIGNAL"
    MISSING_AI_ANALYSIS = "MISSING_AI_ANALYSIS"
    AI_NO_TRADE = "AI_NO_TRADE"
    AI_CONFIDENCE_TOO_LOW = "AI_CONFIDENCE_TOO_LOW"
    AI_SIDE_MISMATCH = "AI_SIDE_MISMATCH"
    AI_ANALYSIS_STALE = "AI_ANALYSIS_STALE"
    SAFE_MODE = "SAFE_MODE"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_TOO_OLD = "SIGNAL_TOO_OLD"
    ACCOUNT_STATE_UNAVAILABLE = "ACCOUNT_STATE_UNAVAILABLE"
    ACCOUNT_STATE_STALE = "ACCOUNT_STATE_STALE"
    DAILY_EQUITY_BASELINE_UNAVAILABLE = "DAILY_EQUITY_BASELINE_UNAVAILABLE"
    DAILY_DRAWDOWN_LIMIT = "DAILY_DRAWDOWN_LIMIT"
    PORTFOLIO_HEAT_LIMIT = "PORTFOLIO_HEAT_LIMIT"
    CORRELATED_EXPOSURE_LIMIT = "CORRELATED_EXPOSURE_LIMIT"
    NEWS_EMBARGO = "NEWS_EMBARGO"
    NEWS_CALENDAR_UNAVAILABLE = "NEWS_CALENDAR_UNAVAILABLE"
    INVALID_RISK_PARAMETERS = "INVALID_RISK_PARAMETERS"


class PositionExposure(StrictModel):
    position_id: str = Field(min_length=1, max_length=128)
    source_order_id: UUID | None = None
    symbol: str = Field(min_length=1, max_length=32)
    side: TradeSide
    risk_amount: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)


class PortfolioSnapshot(StrictModel):
    account: AccountState
    daily_starting_equity: Decimal = Field(gt=Decimal(0), allow_inf_nan=False)
    positions: list[PositionExposure] = Field(default_factory=list)


class EconomicCalendarEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    timestamp: AwareDatetime
    impact: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=1, max_length=16)


class RiskRejection(StrictModel):
    rejection_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    symbol: str = Field(min_length=1, max_length=32)
    code: RiskRejectionCode
    reason: str = Field(min_length=1, max_length=1000)
    occurred_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class RiskApproval(StrictModel):
    signal_id: UUID
    symbol: str
    side: TradeSide
    proposed_risk_amount: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    portfolio_heat_after: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    correlated_risk_after: Decimal = Field(ge=Decimal(0), allow_inf_nan=False)
    timestamp: AwareDatetime
