import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from event_bus.redis_bus import RedisEventBus, trace_id_context
from schemas.events import EventEnvelope
from schemas.messages import (
    BarData,
    OrderType,
    SignedOrderPayload,
    TickData,
    TradeSide,
)
from utils.security import sign_payload, verify_payload


class SharedContractTests(unittest.TestCase):
    def make_order(self) -> SignedOrderPayload:
        return SignedOrderPayload(
            intent_id=uuid4(),
            symbol="EURUSD",
            side=TradeSide.BUY,
            order_type=OrderType.MARKET,
            volume=Decimal("0.10"),
            issued_at=datetime.now(timezone.utc),
            trace_id="test-trace-1",
        )

    def test_signed_payload_verifies_and_detects_tampering(self) -> None:
        secret = "test-secret-that-is-long-enough-for-hmac"
        signed = sign_payload(self.make_order(), secret)

        self.assertTrue(verify_payload(signed, secret))
        changed = signed.model_copy(update={"volume": Decimal("0.20")})
        self.assertFalse(verify_payload(changed, secret))

    def test_signing_rejects_short_secrets(self) -> None:
        with self.assertRaises(ValueError):
            sign_payload(self.make_order(), "too-short")

    def test_strict_model_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            BarData.model_validate(
                {
                    "symbol": "EURUSD",
                    "timeframe": "M1",
                    "timestamp": datetime.now(timezone.utc),
                    "open": Decimal("1.0"),
                    "high": Decimal("1.1"),
                    "low": Decimal("0.9"),
                    "close": Decimal("1.0"),
                    "tick_volume": 10,
                    "surprise": True,
                }
            )

    def test_tick_data_parses_json_numbers_as_decimals(self) -> None:
        tick = TickData.model_validate_json(
            '{"symbol":"EURUSD","timestamp":"2026-09-30T10:00:00Z",'
            '"bid":1.1,"ask":1.2,"source":"mt5"}'
        )
        self.assertEqual(tick.bid, Decimal("1.1"))

    def test_signed_order_requires_price_for_pending_type(self) -> None:
        with self.assertRaises(ValidationError):
            SignedOrderPayload(
                intent_id=uuid4(),
                symbol="EURUSD",
                side=TradeSide.BUY,
                order_type=OrderType.LIMIT,
                volume=Decimal("0.10"),
                issued_at=datetime.now(timezone.utc),
                trace_id="test-trace-2",
            )


class RedisBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_wraps_payload_and_propagates_trace_id(self) -> None:
        class FakeRedis:
            published_channel = None
            published_body = None

            async def publish(self, channel: str, body: str) -> int:
                self.published_channel = channel
                self.published_body = body
                return 2

            async def aclose(self) -> None:
                return None

        client = FakeRedis()
        bus = RedisEventBus("redis://localhost")
        bus._make_client = lambda: client
        order = SharedContractTests().make_order()

        receiver_count = await bus.publish("orders", order)
        event = EventEnvelope.model_validate_json(client.published_body)

        self.assertEqual(receiver_count, 2)
        self.assertEqual(client.published_channel, "orders")
        self.assertEqual(event.trace_id, "test-trace-1")
        self.assertEqual(event.event_type, "SignedOrderPayload")
        self.assertEqual(event.payload["symbol"], "EURUSD")
        await bus.aclose()

    async def test_publish_uses_trace_context_or_generates_trace_id(self) -> None:
        class FakeRedis:
            published_body = None

            async def publish(self, channel: str, body: str) -> int:
                self.published_body = body
                return 1

            async def aclose(self) -> None:
                return None

        client = FakeRedis()
        bus = RedisEventBus("redis://localhost")
        bus._make_client = lambda: client

        with trace_id_context("context-trace"):
            await bus.publish("events", {"value": 1})
        contextual_event = EventEnvelope.model_validate_json(client.published_body)

        await bus.publish("events", {"value": 2})
        generated_event = EventEnvelope.model_validate_json(client.published_body)

        self.assertEqual(contextual_event.trace_id, "context-trace")
        self.assertTrue(generated_event.trace_id)
        self.assertNotEqual(generated_event.trace_id, contextual_event.trace_id)
        await bus.aclose()

    async def test_persisted_state_round_trips(self) -> None:
        class FakeRedis:
            value = None

            async def set(self, key: str, value: str) -> None:
                self.value = value

            async def get(self, key: str) -> str | None:
                return self.value

            async def aclose(self) -> None:
                return None

        client = FakeRedis()
        bus = RedisEventBus("redis://localhost")
        bus._make_client = lambda: client

        await bus.set_state("system:safe_mode", {"enabled": True, "reason": "test"})

        self.assertEqual(
            await bus.get_state("system:safe_mode"),
            {"enabled": True, "reason": "test"},
        )
        await bus.aclose()

    async def test_subscribe_reconnects_and_resubscribes(self) -> None:
        envelope = EventEnvelope(
            event_type="test",
            trace_id="reconnect-trace",
            payload={"value": 1},
        ).model_dump_json()

        class FakePubSub:
            def __init__(self, fail_first_read: bool) -> None:
                self.fail_first_read = fail_first_read
                self.did_read = False
                self.closed = False
                self.subscribed = None

            async def subscribe(self, channel: str) -> None:
                self.subscribed = channel

            async def get_message(self, **kwargs: object) -> dict[str, str] | None:
                if self.fail_first_read and not self.did_read:
                    self.did_read = True
                    raise RedisConnectionError("simulated connection loss")
                self.did_read = True
                return {"type": "message", "data": envelope}

            async def aclose(self) -> None:
                self.closed = True

        class FakeRedis:
            def __init__(self, pubsub: FakePubSub) -> None:
                self._pubsub = pubsub

            def pubsub(self) -> FakePubSub:
                return self._pubsub

            async def aclose(self) -> None:
                return None

        pubsubs = [FakePubSub(True), FakePubSub(False)]
        clients = iter(FakeRedis(pubsub) for pubsub in pubsubs)
        bus = RedisEventBus(
            "redis://localhost",
            retry_base_seconds=0.001,
            retry_max_seconds=0.01,
        )
        bus._make_client = lambda: next(clients)

        events = []
        iterator = bus.subscribe("signals")
        with self.assertLogs("event_bus.redis_bus", level="WARNING"):
            async for event in iterator:
                events.append(event)
                break
        await iterator.aclose()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trace_id, "reconnect-trace")
        self.assertEqual([pubsub.subscribed for pubsub in pubsubs], ["signals", "signals"])
        self.assertTrue(pubsubs[0].closed)
        await bus.aclose()


if __name__ == "__main__":
    unittest.main()
