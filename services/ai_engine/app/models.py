from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from schemas.base import StrictModel
from schemas.messages import AIAnalysisResult, AIRecommendation, PatternSetupSignal
from services.ai_engine.app.circuit_breaker import CircuitState


class AIAvailability(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class AIAnalysisRequest(StrictModel):
    signal: PatternSetupSignal
    market_context: dict[str, Any] = Field(default_factory=dict)
    quantitative_override_enabled: bool = False
    quantitative_override_decision: AIRecommendation | None = None
    quantitative_override_confidence: Decimal | None = Field(
        default=None,
        ge=Decimal(0),
        le=Decimal(1),
        allow_inf_nan=False,
    )
    quantitative_override_reasoning: str | None = Field(
        default=None,
        min_length=1,
        max_length=8000,
    )

    @model_validator(mode="after")
    def validate_override(self) -> "AIAnalysisRequest":
        provided = (
            self.quantitative_override_decision is not None
            or self.quantitative_override_confidence is not None
            or self.quantitative_override_reasoning is not None
        )
        complete = (
            self.quantitative_override_decision is not None
            and self.quantitative_override_confidence is not None
            and self.quantitative_override_reasoning is not None
        )
        if provided and not complete:
            raise ValueError("quantitative override fields must be supplied together")
        return self


class AIAnalysisResponse(StrictModel):
    state: CircuitState
    ai_state: AIAvailability
    result: AIAnalysisResult
    fallback_reason: str | None = None


class AIStateMessage(StrictModel):
    ai_state: AIAvailability
    circuit_state: CircuitState
    provider: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)
    consecutive_failures: int = Field(ge=0)
    recovery_remaining_seconds: float = Field(ge=0)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
