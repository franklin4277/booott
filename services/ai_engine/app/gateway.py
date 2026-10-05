import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from prometheus_client import Counter, Gauge, Histogram

from event_bus.redis_bus import RedisEventBus
from schemas.messages import AIAnalysisResult, AIRecommendation
from services.ai_engine.app.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from services.ai_engine.app.models import (
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIAvailability,
    AIStateMessage,
)
from services.ai_engine.app.provider import (
    AIProvider,
    InstructorProvider,
    UnavailableProvider,
)
from services.ai_engine.app.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)
AI_REQUEST_LATENCY = Histogram(
    "ai_gateway_request_latency_seconds",
    "AI provider request latency.",
    ["provider", "outcome"],
)
AI_TOKENS_ESTIMATED = Counter(
    "ai_gateway_tokens_estimated_total",
    "Estimated AI prompt/completion tokens (character count divided by four).",
    ["provider", "kind"],
)
AI_CIRCUIT_STATE = Gauge(
    "ai_gateway_circuit_state",
    "AI breaker state encoded as 0=CLOSED, 0.5=HALF-OPEN, 1=OPEN.",
)
AI_REQUESTS = Counter(
    "ai_gateway_requests_total", "AI gateway analysis outcomes.", ["outcome"]
)
AI_STATE_CHANNEL = "ai.state"


class AIGateway:
    def __init__(
        self,
        bus: RedisEventBus,
        *,
        provider: AIProvider | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        failure_threshold: int | None = None,
        recovery_seconds: float | None = None,
        request_timeout_seconds: float | None = None,
        rate_limit_capacity: float | None = None,
        rate_limit_refill_per_second: float | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        quantitative_override_enabled: bool | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.bus = bus
        self.provider_name = provider_name or os.environ.get("AI_PROVIDER", "openai").lower()
        self.model = model or os.environ.get("AI_MODEL", "gpt-4o-mini")
        self.request_timeout_seconds = request_timeout_seconds or float(
            os.environ.get("AI_REQUEST_TIMEOUT_SECONDS", "20")
        )
        self.quantitative_override_enabled = (
            quantitative_override_enabled
            if quantitative_override_enabled is not None
            else os.environ.get("AI_QUANT_OVERRIDE_ENABLED", "false").lower()
            in {"1", "true", "yes"}
        )
        if provider is not None:
            self.provider = provider
        else:
            try:
                self.provider = InstructorProvider(
                    provider=self.provider_name,
                    model=self.model,
                    timeout_seconds=self.request_timeout_seconds,
                )
            except ValueError as exc:
                self.provider = UnavailableProvider(str(exc))
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(
            capacity=rate_limit_capacity
            if rate_limit_capacity is not None
            else float(os.environ.get("AI_RATE_LIMIT_CAPACITY", "5")),
            refill_rate=rate_limit_refill_per_second
            if rate_limit_refill_per_second is not None
            else float(os.environ.get("AI_RATE_LIMIT_REFILL_PER_SECOND", "1")),
        )
        self.breaker = CircuitBreaker(
            failure_threshold=failure_threshold
            if failure_threshold is not None
            else int(os.environ.get("AI_CIRCUIT_FAILURE_THRESHOLD", "3")),
            recovery_seconds=recovery_seconds
            if recovery_seconds is not None
            else float(os.environ.get("AI_CIRCUIT_RECOVERY_SECONDS", "300")),
            clock=monotonic_clock if monotonic_clock is not None else time.monotonic,
            on_state_change=self._on_state_change,
        )
        self._last_state_reason = "AI gateway initialized"

    async def _on_state_change(self, state: CircuitState) -> None:
        if state == CircuitState.OPEN:
            self._last_state_reason = "AI provider failed; recovery window active"
        elif state == CircuitState.HALF_OPEN:
            self._last_state_reason = "AI provider recovery probe in progress"
        else:
            self._last_state_reason = "AI provider is available"
        await self.publish_state()

    async def publish_state(self, trace_id: str | None = None) -> None:
        AI_CIRCUIT_STATE.set(
            {
                CircuitState.CLOSED: 0,
                CircuitState.HALF_OPEN: 0.5,
                CircuitState.OPEN: 1,
            }[self.breaker.state]
        )
        state_message = AIStateMessage(
            ai_state=(
                AIAvailability.OFFLINE
                if self.breaker.state != CircuitState.CLOSED
                else AIAvailability.ONLINE
            ),
            circuit_state=self.breaker.state,
            provider=self.provider_name,
            reason=self._last_state_reason,
            consecutive_failures=self.breaker.consecutive_failures,
            recovery_remaining_seconds=self.breaker.recovery_remaining,
            trace_id=trace_id,
        )
        try:
            await self.bus.publish(
                AI_STATE_CHANNEL,
                state_message,
                trace_id=trace_id or uuid4().hex,
                event_type="AIState",
            )
        except Exception:
            logger.exception("Failed to publish AI state %s", self.breaker.state)

    async def publish_offline_while_open(self, stop_event: asyncio.Event) -> None:
        interval = max(0.1, float(os.environ.get("AI_STATE_PUBLISH_INTERVAL_SECONDS", "10")))
        while not stop_event.is_set():
            if self.breaker.state == CircuitState.OPEN:
                await self.publish_state()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _fallback_result(
        self,
        request: AIAnalysisRequest,
        reason: str,
    ) -> AIAnalysisResult:
        signal = request.signal
        if (
            self.quantitative_override_enabled
            and request.quantitative_override_enabled
            and request.quantitative_override_decision is not None
            and request.quantitative_override_confidence is not None
            and request.quantitative_override_reasoning is not None
        ):
            decision = request.quantitative_override_decision
            confidence = request.quantitative_override_confidence
            reasoning = (
                "Deterministic quantitative override used while AI is offline. "
                + request.quantitative_override_reasoning
            )
            risk_multiplier = Decimal(1) if decision != AIRecommendation.NO_TRADE else Decimal(0)
            invalidated_by = ["The deterministic override's configured quantitative conditions no longer hold."]
        else:
            decision = AIRecommendation.NO_TRADE
            confidence = Decimal(0)
            reasoning = f"AI analysis unavailable; fail-safe NO_TRADE applied. {reason}"
            risk_multiplier = Decimal(0)
            invalidated_by = ["A successful, schema-valid AI analysis becomes available."]

        return AIAnalysisResult(
            signal_id=signal.signal_id,
            provider=self.provider_name,
            model=self.model,
            decision=decision,
            confidence_score=confidence,
            risk_multiplier=risk_multiplier,
            reasoning=reasoning,
            invalidated_by=invalidated_by,
            created_at=datetime.now(UTC),
            trace_id=signal.trace_id,
        )

    async def analyze(self, request: AIAnalysisRequest) -> AIAnalysisResponse:
        trace_id = request.signal.trace_id
        try:
            await self.breaker.acquire_permission()
        except CircuitOpenError:
            await self.publish_state(trace_id)
            remaining = self.breaker.recovery_remaining
            reason = (
                f"AI circuit is {self.breaker.state}; "
                f"{remaining:.1f}s remain in its recovery window."
            )
            return AIAnalysisResponse(
                state=self.breaker.state,
                ai_state=AIAvailability.OFFLINE,
                result=self._fallback_result(request, reason),
                fallback_reason=reason,
            )

        call_started = time.monotonic()
        try:
            await self.rate_limiter.acquire()
            prompt_size = len(
                request.signal.model_dump_json()
                + str(request.market_context)
            )
            async with asyncio.timeout(self.request_timeout_seconds):
                result = await self.provider.analyze(
                    request.signal,
                    request.market_context,
                )
            result = AIAnalysisResult.model_validate(result)
            if (
                result.signal_id is not None
                and result.signal_id != request.signal.signal_id
            ):
                raise ValueError("AI response signal_id did not match request")
            result = result.model_copy(
                update={
                    "provider": self.provider_name,
                    "model": self.model,
                    "signal_id": request.signal.signal_id,
                    "trace_id": request.signal.trace_id,
                }
            )
            if result.decision == AIRecommendation.NO_TRADE:
                result = result.model_copy(update={"risk_multiplier": Decimal(0)})
            await self.breaker.record_success()
            AI_REQUEST_LATENCY.labels(
                provider=self.provider_name, outcome="success"
            ).observe(time.monotonic() - call_started)
            AI_TOKENS_ESTIMATED.labels(
                provider=self.provider_name, kind="prompt"
            ).inc(max(1, prompt_size // 4))
            AI_TOKENS_ESTIMATED.labels(
                provider=self.provider_name, kind="completion"
            ).inc(max(1, len(result.model_dump_json()) // 4))
            AI_REQUESTS.labels(outcome="success").inc()
            return AIAnalysisResponse(
                state=self.breaker.state,
                ai_state=AIAvailability.ONLINE,
                result=result,
            )
        except asyncio.CancelledError:
            await self.breaker.record_failure()
            AI_REQUEST_LATENCY.labels(
                provider=self.provider_name, outcome="cancelled"
            ).observe(time.monotonic() - call_started)
            AI_REQUESTS.labels(outcome="cancelled").inc()
            raise
        except Exception as exc:
            await self.breaker.record_failure()
            AI_REQUEST_LATENCY.labels(
                provider=self.provider_name, outcome="failure"
            ).observe(time.monotonic() - call_started)
            AI_REQUESTS.labels(outcome="failure").inc()
            logger.exception(
                "AI analysis failed (consecutive failures: %d)",
                self.breaker.consecutive_failures,
            )
            await self.publish_state(trace_id)
            fallback_reason = f"AI provider request failed ({type(exc).__name__})."
            return AIAnalysisResponse(
                state=self.breaker.state,
                ai_state=AIAvailability.OFFLINE,
                result=self._fallback_result(request, fallback_reason),
                fallback_reason=fallback_reason,
            )
