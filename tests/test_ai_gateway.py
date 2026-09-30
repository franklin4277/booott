import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope
from schemas.messages import (
    AIAnalysisResult,
    AIRecommendation,
    PatternSetupSignal,
    TradeSide,
)
from services.ai_engine.app.circuit_breaker import CircuitState
from services.ai_engine.app.gateway import AIGateway
from services.ai_engine.app.main import consume_pattern_signals
from services.ai_engine.app.models import (
    AIAnalysisRequest,
    AIAvailability,
)
from services.ai_engine.app.rate_limiter import TokenBucketRateLimiter


def make_signal() -> PatternSetupSignal:
    now = datetime.now(timezone.utc)
    return PatternSetupSignal(
        strategy_id="test-strategy",
        symbol="EURUSD",
        timeframe="M5",
        side=TradeSide.BUY,
        entry_price=Decimal("1.1000"),
        stop_loss=Decimal("1.0900"),
        take_profit=Decimal("1.1200"),
        confidence=Decimal("0.8"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        trace_id="ai-test-trace",
    )


def make_result(signal: PatternSetupSignal) -> AIAnalysisResult:
    return AIAnalysisResult(
        signal_id=signal.signal_id,
        decision=AIRecommendation.BUY,
        confidence_score=Decimal("0.85"),
        risk_multiplier=Decimal("0.5"),
        reasoning="Higher-timeframe confirmation agrees with the setup.",
        invalidated_by=["Price closes below the setup stop level."],
        trace_id=signal.trace_id,
    )


class FakeBus:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, channel: str, payload: object, **kwargs: object) -> int:
        self.published.append((channel, payload, kwargs))
        return 1


class AIGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_failures_open_circuit_publish_offline_and_fail_safe(self) -> None:
        class FailingProvider:
            calls = 0

            async def analyze(self, signal, market_context):
                self.calls += 1
                raise RuntimeError("provider unavailable")

        bus = FakeBus()
        provider = FailingProvider()
        gateway = AIGateway(
            bus,  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            failure_threshold=3,
            recovery_seconds=60,
            rate_limit_capacity=100,
            rate_limit_refill_per_second=100,
        )
        request = AIAnalysisRequest(signal=make_signal())

        with self.assertLogs("services.ai_engine.app.gateway", level="ERROR"):
            responses = [await gateway.analyze(request) for _ in range(3)]
        offline_response = await gateway.analyze(request)

        self.assertEqual(provider.calls, 3)
        self.assertEqual(gateway.breaker.state, CircuitState.OPEN)
        self.assertEqual(offline_response.ai_state, AIAvailability.OFFLINE)
        self.assertEqual(offline_response.result.decision, AIRecommendation.NO_TRADE)
        self.assertEqual(offline_response.result.risk_multiplier, Decimal("0"))
        self.assertTrue(any(
            channel == "ai.state" and payload.ai_state == AIAvailability.OFFLINE
            for channel, payload, _ in bus.published
        ))
        self.assertTrue(all(
            response.result.decision == AIRecommendation.NO_TRADE
            for response in responses
        ))

    async def test_successful_half_open_probe_closes_circuit(self) -> None:
        class FakeClock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        class FlakyProvider:
            calls = 0

            async def analyze(self, signal, market_context):
                self.calls += 1
                if self.calls <= 3:
                    raise RuntimeError("temporary provider error")
                return make_result(signal)

        bus = FakeBus()
        provider = FlakyProvider()
        clock = FakeClock()
        gateway = AIGateway(
            bus,  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            recovery_seconds=30,
            rate_limit_capacity=100,
            rate_limit_refill_per_second=100,
            monotonic_clock=clock,
        )

        with self.assertLogs("services.ai_engine.app.gateway", level="ERROR"):
            for _ in range(3):
                await gateway.analyze(AIAnalysisRequest(signal=make_signal()))
        self.assertEqual(gateway.breaker.state, CircuitState.OPEN)

        clock.now = 31
        recovered = await gateway.analyze(AIAnalysisRequest(signal=make_signal()))

        self.assertEqual(provider.calls, 4)
        self.assertEqual(gateway.breaker.state, CircuitState.CLOSED)
        self.assertEqual(recovered.ai_state, AIAvailability.ONLINE)
        self.assertEqual(recovered.result.decision, AIRecommendation.BUY)

    async def test_quantitative_override_requires_both_explicit_flags(self) -> None:
        class FailingProvider:
            async def analyze(self, signal, market_context):
                raise RuntimeError("provider unavailable")

        bus = FakeBus()
        gateway = AIGateway(
            bus,  # type: ignore[arg-type]
            provider=FailingProvider(),  # type: ignore[arg-type]
            failure_threshold=1,
            rate_limit_capacity=10,
            rate_limit_refill_per_second=10,
            quantitative_override_enabled=True,
        )
        request = AIAnalysisRequest(
            signal=make_signal(),
            quantitative_override_enabled=True,
            quantitative_override_decision=AIRecommendation.BUY,
            quantitative_override_confidence=Decimal("0.9"),
            quantitative_override_reasoning="Deterministic rules and risk checks passed.",
        )

        with self.assertLogs("services.ai_engine.app.gateway", level="ERROR"):
            response = await gateway.analyze(request)

        self.assertEqual(response.ai_state, AIAvailability.OFFLINE)
        self.assertEqual(response.result.decision, AIRecommendation.BUY)
        self.assertEqual(response.result.risk_multiplier, Decimal("1"))

    async def test_incomplete_quantitative_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AIAnalysisRequest(
                signal=make_signal(),
                quantitative_override_enabled=True,
                quantitative_override_decision=AIRecommendation.BUY,
            )

    async def test_rate_limiter_waits_for_refill(self) -> None:
        class FakeClock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        sleeps = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.now += seconds

        limiter = TokenBucketRateLimiter(
            capacity=1,
            refill_rate=2,
            clock=clock,
            sleep=fake_sleep,
        )
        await limiter.acquire()
        await limiter.acquire()

        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0], 0.5)

    async def test_schema_invalid_provider_response_counts_as_failure(self) -> None:
        class InvalidProvider:
            async def analyze(self, signal, market_context):
                return {"decision": "buy", "unknown": True}

        gateway = AIGateway(
            FakeBus(),  # type: ignore[arg-type]
            provider=InvalidProvider(),  # type: ignore[arg-type]
            failure_threshold=1,
            rate_limit_capacity=10,
            rate_limit_refill_per_second=10,
        )

        with self.assertLogs("services.ai_engine.app.gateway", level="ERROR"):
            response = await gateway.analyze(AIAnalysisRequest(signal=make_signal()))

        self.assertEqual(gateway.breaker.state, CircuitState.OPEN)
        self.assertEqual(response.result.decision, AIRecommendation.NO_TRADE)

    async def test_provider_output_is_correlated_to_signal(self) -> None:
        class SuccessfulProvider:
            async def analyze(self, signal, market_context):
                return make_result(signal)

        gateway = AIGateway(
            FakeBus(),  # type: ignore[arg-type]
            provider=SuccessfulProvider(),  # type: ignore[arg-type]
            rate_limit_capacity=10,
            rate_limit_refill_per_second=10,
        )
        signal = make_signal()
        response = await gateway.analyze(AIAnalysisRequest(signal=signal))

        self.assertEqual(response.result.signal_id, signal.signal_id)
        self.assertEqual(response.result.trace_id, "ai-test-trace")
        self.assertEqual(response.result.provider, "openai")

    async def test_pattern_consumer_publishes_typed_analysis(self) -> None:
        signal = make_signal()
        incoming = EventEnvelope(
            event_type="PatternSetupSignal",
            trace_id="consumer-trace",
            payload=signal.model_dump(mode="json"),
        )

        class ConsumerBus(FakeBus):
            async def subscribe(self, channel: str):
                self.subscribed_channel = channel
                yield incoming

        class SuccessfulProvider:
            async def analyze(self, signal, market_context):
                return make_result(signal)

        bus = ConsumerBus()
        gateway = AIGateway(
            bus,  # type: ignore[arg-type]
            provider=SuccessfulProvider(),  # type: ignore[arg-type]
            rate_limit_capacity=10,
            rate_limit_refill_per_second=10,
        )

        await consume_pattern_signals(bus, gateway)  # type: ignore[arg-type]

        self.assertEqual(bus.subscribed_channel, "signals.pattern")
        output = next(
            item for item in bus.published if item[0] == "signals.ai"
        )
        self.assertIsInstance(output[1], AIAnalysisResult)
        self.assertEqual(output[1].trace_id, "consumer-trace")
        self.assertEqual(output[2]["event_type"], "AIAnalysisResult")


if __name__ == "__main__":
    unittest.main()
