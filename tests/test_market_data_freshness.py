import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from schemas.messages import TickData
from scripts import mt5_host_adapter
from services.market_data.app.consumer import MarketDataConsumer
from services.market_data.app.freshness import (
    StaleMarketTickError,
    is_fresh_market_tick,
    require_fresh_market_tick,
)


class MarketDataFreshnessTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_recent_tick_and_rejects_stale_or_future_tick(self) -> None:
        now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        with patch.dict(os.environ, {"MT5_MARKET_MAX_TICK_AGE_SECONDS": "30"}):
            self.assertTrue(
                is_fresh_market_tick(now - timedelta(seconds=30), now=now)
            )
            self.assertFalse(
                is_fresh_market_tick(now - timedelta(seconds=31), now=now)
            )
            self.assertFalse(
                is_fresh_market_tick(now + timedelta(seconds=6), now=now)
            )
            with self.assertRaises(StaleMarketTickError):
                require_fresh_market_tick(now - timedelta(days=2), now=now)

    async def test_market_consumer_does_not_store_or_publish_stale_tick(self) -> None:
        class FakeBus:
            def __init__(self) -> None:
                self.published = []

            async def publish(self, channel, payload, **kwargs):
                self.published.append((channel, payload, kwargs))
                return 1

        class FakeStore:
            def __init__(self) -> None:
                self.ticks = []

            async def store_tick(self, tick):
                self.ticks.append(tick)

        bus = FakeBus()
        store = FakeStore()
        consumer = MarketDataConsumer(bus, store)  # type: ignore[arg-type]
        tick = TickData(
            symbol="XAUUSD",
            timestamp=datetime.now(UTC) - timedelta(days=2),
            bid=Decimal(4100),
            ask=Decimal(4101),
            source="mt5",
        )
        try:
            with self.assertRaises(StaleMarketTickError):
                await consumer.handle_tick(tick, publish_event=True)
        finally:
            consumer._zmq_context.term()

        self.assertEqual(store.ticks, [])
        self.assertEqual(bus.published, [])

    def test_adapter_skips_stale_quote_without_refetching_same_tick(self) -> None:
        stale_time = datetime.now(UTC) - timedelta(days=2)
        timestamp_milliseconds = int(stale_time.timestamp() * 1000)
        tick = SimpleNamespace(
            time_msc=timestamp_milliseconds,
            time=int(stale_time.timestamp()),
            bid=4100.0,
            ask=4101.0,
            last=0.0,
            flags=0,
            volume_real=0.0,
        )

        class FakeMT5:
            TIMEFRAME_M5 = 5

            @staticmethod
            def symbol_select(symbol, selected):
                return True

            @staticmethod
            def symbol_info_tick(symbol):
                return tick

            @staticmethod
            def copy_rates_from_pos(symbol, timeframe, start, count):
                return []

            @staticmethod
            def last_error():
                return (1, "Success")

        tick_cursors = {}
        with (
            patch.dict(
                os.environ,
                {
                    "MT5_MARKET_SYMBOLS": "XAUUSD",
                    "MT5_MARKET_TIMEFRAMES": "M5",
                    "MT5_MARKET_MAX_TICK_AGE_SECONDS": "30",
                },
            ),
            patch.object(mt5_host_adapter, "_mt5", return_value=FakeMT5()),
        ):
            messages = mt5_host_adapter._collect_market_data({}, tick_cursors)

        self.assertEqual(messages, [])
        self.assertEqual(tick_cursors, {"XAUUSD": timestamp_milliseconds})


if __name__ == "__main__":
    unittest.main()
