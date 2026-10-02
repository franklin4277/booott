"""Canonical Pydantic v2 trading contracts used across microservices and EAs."""

from enum import StrEnum

from schemas.messages import (
    AIAnalysisResult,
    AIRecommendation,
    AccountState,
    BarData,
    ExecutionReport,
    ExecutionStatus,
    HeartbeatMessage,
    HeartbeatStatus,
    OrderType,
    PatternSetupSignal,
    SignedOrderPayload,
    SpreadMetrics,
    TechnicalIndicatorSnapshot,
    TickData,
    TradeIntent,
    TradeSide,
)


class TradingState(StrEnum):
    TRADING_ENABLED = "TRADING_ENABLED"
    NO_NEW_TRADES = "NO_NEW_TRADES"
    SAFE_MODE = "SAFE_MODE"
    EMERGENCY_FLAT = "EMERGENCY_FLAT"


__all__ = [
    "AIAnalysisResult",
    "AIRecommendation",
    "AccountState",
    "BarData",
    "ExecutionReport",
    "ExecutionStatus",
    "HeartbeatMessage",
    "HeartbeatStatus",
    "OrderType",
    "PatternSetupSignal",
    "SignedOrderPayload",
    "SpreadMetrics",
    "TechnicalIndicatorSnapshot",
    "TickData",
    "TradeIntent",
    "TradeSide",
    "TradingState",
]
