import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from schemas.events import EventEnvelope
from schemas.messages import BarData, PatternSetupSignal, TickData, TradeSide
from services.market_data.app.consumer import MarketDataConsumer
from services.market_data.app.indicators import IndicatorState
from services.pattern_engine.app.consumer import PatternConsumer
from services.pattern_engine.app.engine import PatternConfig, PatternEngine


def make_bar(
    timestamp: datetime,
    *,
    timeframe: str = "M5",
    open_price: str = "100.00",
    high: str = "100.10",
    low: str = "99.90",
    close: str = "100.00",
    spread: int = 10,
) -> BarData:
    return BarData(
        symbol="EURUSD",
        timeframe=timeframe,
        timestamp=timestamp,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=100,
        spread=spread,
    )


class IndicatorTests(unittest.TestCase):
    def test_calculates_ema_atr_rsi_and_spread_moving_average(self) -> None:
        state = IndicatorState(
            ema_period=2,
            atr_period=2,
            rsi_period=2,
            spread_period=3,
        )
        start = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
        state.update_bar(make_bar(start, open_price="1.00", high="1.10", low="0.90", close="1.00"))
        state.update_bar(make_bar(start + timedelta(minutes=1), open_price="1.00", high="1.20", low="0.95", close="1.10"))
        snapshot = state.update_bar(
            make_bar(
                start + timedelta(minutes=2),
                open_price="1.10",
                high="1.30",
                low="1.05",
                close="1.20",
            )
        )

        self.assertEqual(snapshot.ema, Decimal("1.155555555555555555555555556"))
        self.assertGreater(snapshot.atr, Decimal("0"))
        self.assertEqual(snapshot.rsi, Decimal("100"))

        for spread in (
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.3"),
            Decimal("0.4"),
        ):
            tick = TickData(
                symbol="EURUSD",
                timestamp=start,
                bid=Decimal("1.0"),
                ask=Decimal("1.0") + spread,
                source="test",
            )
            spread_snapshot = state.update_tick(tick)
        self.assertEqual(spread_snapshot.spread_ma, Decimal("0.3"))
        self.assertEqual(spread_snapshot.spread_ratio, Decimal("2"))


class PatternEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PatternConfig(
            base_timeframe="M5",
            confirmation_timeframes=("M15", "H1"),
            history_size=16,
            structure_lookback=2,
            atr_period=2,
            atr_baseline_period=3,
            atr_expansion_ratio=Decimal("1.2"),
            max_spread_ratio=Decimal("2.5"),
        )
        self.engine = PatternEngine(self.config)
        self.start = datetime(2026, 1, 15, 15, tzinfo=timezone.utc)
        self.engine.on_bar(
            make_bar(self.start, timeframe="M15", open_price="99.9", close="100.0")
        )
        self.engine.on_bar(
            make_bar(self.start, timeframe="H1", open_price="99.9", close="100.0")
        )
        self.engine.record_spread_ratio("EURUSD", Decimal("1.2"))

    def test_publishes_candidate_with_mtf_pattern_structure_and_volatility_evidence(self) -> None:
        calm = [
            make_bar(
                self.start + timedelta(minutes=i * 5),
                open_price="100.00",
                high="100.10",
                low="99.90",
                close="100.00",
            )
            for i in range(4)
        ]
        bearish = make_bar(
            self.start + timedelta(minutes=20),
            open_price="100.05",
            high="100.10",
            low="99.90",
            close="99.95",
        )
        for bar in calm + [bearish]:
            self.assertIsNone(self.engine.on_bar(bar))

        signal = self.engine.on_bar(
            make_bar(
                self.start + timedelta(minutes=25),
                open_price="99.90",
                high="100.60",
                low="99.80",
                close="100.50",
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, TradeSide.BUY)
        self.assertIn("bullish_engulfing", signal.attributes["evidence"])
        self.assertIn("bullish_structure_break", signal.attributes["evidence"])
        self.assertGreater(Decimal(signal.attributes["volatility_ratio"]), Decimal("1.2"))

    def test_blocks_high_spread_and_unmeasured_spread(self) -> None:
        self.engine.record_spread_ratio("EURUSD", Decimal("2.51"))
        self.assertIsNone(
            self.engine.on_bar(
                make_bar(
                    self.start + timedelta(minutes=5),
                    open_price="100.00",
                    high="100.20",
                    low="99.80",
                    close="100.10",
                )
            )
        )

        engine_without_spread = PatternEngine(self.config)
        engine_without_spread.on_bar(
            make_bar(self.start, timeframe="M15", open_price="99.9", close="100.0")
        )
        engine_without_spread.on_bar(
            make_bar(self.start, timeframe="H1", open_price="99.9", close="100.0")
        )
        self.assertIsNone(
            engine_without_spread.on_bar(
                make_bar(
                    self.start + timedelta(minutes=5),
                    open_price="100.00",
                    high="100.20",
                    low="99.80",
                    close="100.10",
                )
            )
        )

    def test_blocks_new_york_rollover_window_and_requires_mtf_confirmation(self) -> None:
        rollover = datetime(2026, 1, 15, 21, 55, tzinfo=timezone.utc)
        self.assertTrue(self.engine.is_rollover(rollover))
        self.assertTrue(
            self.engine.is_rollover(datetime(2026, 7, 15, 20, 55, tzinfo=timezone.utc))
        )
        self.assertFalse(
            self.engine.is_rollover(datetime(2026, 1, 15, 22, 15, tzinfo=timezone.utc))
        )

        engine = PatternEngine(self.config)
        engine.record_spread_ratio("EURUSD", Decimal("1.1"))
        bar = make_bar(self.start + timedelta(minutes=5))
        self.assertIsNone(engine.on_bar(bar))


class PatternConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_validated_signal_with_input_trace_id(self) -> None:
        class FakeBus:
            published = []

            async def publish(self, channel: str, payload: object, **kwargs: object) -> int:
                self.published.append((channel, payload, kwargs))
                return 1

        config = PatternConfig(
            base_timeframe="M5",
            confirmation_timeframes=("M15", "H1"),
            history_size=16,
            structure_lookback=2,
            atr_period=2,
            atr_baseline_period=3,
            atr_expansion_ratio=Decimal("1.2"),
        )
        engine = PatternEngine(config)
        start = datetime(2026, 1, 15, 15, tzinfo=timezone.utc)
        engine.record_spread_ratio("EURUSD", Decimal("1.1"))
        engine.on_bar(
            make_bar(start, timeframe="M15", open_price="99.9", close="100.0")
        )
        engine.on_bar(
            make_bar(start, timeframe="H1", open_price="99.9", close="100.0")
        )
        for index in range(4):
            engine.on_bar(
                make_bar(
                    start + timedelta(minutes=index * 5),
                    open_price="100.00",
                    high="100.10",
                    low="99.90",
                    close="100.00",
                )
            )
        engine.on_bar(
            make_bar(
                start + timedelta(minutes=20),
                open_price="100.05",
                high="100.10",
                low="99.90",
                close="99.95",
            )
        )

        final_bar = make_bar(
            start + timedelta(minutes=25),
            open_price="99.90",
            high="100.60",
            low="99.80",
            close="100.50",
        )
        event = EventEnvelope(
            event_type="BarData",
            trace_id="incoming-trace",
            payload=final_bar.model_dump(mode="json"),
        )
        bus = FakeBus()
        consumer = PatternConsumer(bus, engine)  # type: ignore[arg-type]

        await consumer.process_event(event)

        self.assertEqual(len(bus.published), 1)
        channel, signal, options = bus.published[0]
        self.assertEqual(channel, "signals.pattern")
        self.assertIsInstance(signal, PatternSetupSignal)
        self.assertEqual(signal.trace_id, "incoming-trace")
        self.assertEqual(options["event_type"], "PatternSetupSignal")


class MarketDataConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_direct_ingress_and_emits_indicators_and_normalized_event(self) -> None:
        class FakeBus:
            published = []

            async def publish(self, channel: str, payload: object, **kwargs: object) -> int:
                self.published.append((channel, payload, kwargs))
                return 1

        class FakeStore:
            ticks = []

            async def store_tick(self, tick: TickData) -> None:
                self.ticks.append(tick)

        bus = FakeBus()
        store = FakeStore()
        consumer = MarketDataConsumer(bus, store)  # type: ignore[arg-type]
        tick = TickData(
            symbol="EURUSD",
            timestamp=self._timestamp(),
            bid=Decimal("1.1000"),
            ask=Decimal("1.1002"),
            source="mt5",
        )

        snapshot = await consumer.handle_tick(
            tick,
            trace_id="direct-zmq",
            publish_event=True,
        )

        self.assertEqual(store.ticks, [tick])
        self.assertEqual(snapshot.spread_sample_count, 1)
        self.assertIsNone(snapshot.spread_ratio)
        self.assertEqual(bus.published[0][0], "market.ticks")
        self.assertEqual(bus.published[1][0], "market.indicators")
        self.assertEqual(bus.published[1][2]["trace_id"], "direct-zmq")
        echo = EventEnvelope(
            event_type="TickData",
            trace_id="direct-zmq",
            payload=tick.model_dump(mode="json"),
        )
        with self.assertNoLogs("services.market_data.app.consumer", level="ERROR"):
            await consumer.process_event(echo)
        self.assertEqual(store.ticks, [tick])
        self.assertEqual(len(bus.published), 2)

        same_timestamp_tick = TickData(
            symbol="EURUSD",
            timestamp=tick.timestamp,
            bid=Decimal("1.1001"),
            ask=Decimal("1.1003"),
            source="mt5",
        )
        await consumer.handle_tick(same_timestamp_tick)
        self.assertEqual(store.ticks, [tick, same_timestamp_tick])
        consumer._zmq_context.term()

    async def test_persists_bars_and_publishes_technical_indicators(self) -> None:
        class FakeBus:
            def __init__(self) -> None:
                self.published = []

            async def publish(self, channel: str, payload: object, **kwargs: object) -> int:
                self.published.append((channel, payload, kwargs))
                return 1

        class FakeStore:
            def __init__(self) -> None:
                self.bars = []

            async def store_bar(self, bar: BarData) -> None:
                self.bars.append(bar)

        bus = FakeBus()
        store = FakeStore()
        consumer = MarketDataConsumer(bus, store)  # type: ignore[arg-type]
        bar = make_bar(self._timestamp())

        snapshot = await consumer.handle_bar(
            bar,
            trace_id="bar-trace",
            publish_event=True,
        )

        self.assertEqual(store.bars, [bar])
        self.assertIsNotNone(snapshot.ema)
        self.assertEqual(bus.published[0][0], "market.bars")
        self.assertEqual(bus.published[1][0], "market.technicals")
        self.assertEqual(bus.published[1][1].ema, bar.close)
        self.assertEqual(bus.published[1][2]["trace_id"], "bar-trace")
        consumer._zmq_context.term()

    @staticmethod
    def _timestamp() -> datetime:
        return datetime(2026, 1, 15, 15, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
