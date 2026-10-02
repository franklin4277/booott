import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from schemas.events import EventEnvelope
from schemas.messages import BarData, PatternSetupSignal, TradeSide
from services.pattern_engine.app.consumer import PatternConsumer


class FakeBus:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, channel: str, payload: object, **kwargs: object) -> int:
        self.published.append((channel, payload, kwargs))
        return 1


class SignalEngine:
    def on_bar(self, bar: BarData) -> PatternSetupSignal:
        now = datetime.now(timezone.utc)
        return PatternSetupSignal(
            strategy_id="test",
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            side=TradeSide.BUY,
            entry_price=Decimal("1.1"),
            stop_loss=Decimal("1.0"),
            take_profit=Decimal("1.2"),
            confidence=Decimal("0.8"),
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            attributes={"evidence": ["test"]},
        )


class PatternFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def process_bar(self, timestamp: datetime) -> FakeBus:
        bar = BarData(
            symbol="EURUSD",
            timeframe="M5",
            timestamp=timestamp,
            open=Decimal("1.1"),
            high=Decimal("1.2"),
            low=Decimal("1.0"),
            close=Decimal("1.1"),
            tick_volume=100,
        )
        event = EventEnvelope(
            event_type="BarData",
            trace_id="freshness-test",
            payload=bar.model_dump(mode="json"),
        )
        bus = FakeBus()
        consumer = PatternConsumer(bus, SignalEngine())  # type: ignore[arg-type]
        await consumer.process_event(event)
        return bus

    async def test_ignores_setup_triggered_by_stale_bar(self):
        bus = await self.process_bar(
            datetime.now(timezone.utc) - timedelta(minutes=20)
        )

        self.assertEqual(bus.published, [])

    async def test_accepts_setup_from_latest_closed_bar(self):
        bus = await self.process_bar(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )

        self.assertEqual(len(bus.published), 1)
        self.assertEqual(bus.published[0][0], "signals.pattern")
