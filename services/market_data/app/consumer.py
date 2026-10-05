import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

import zmq
import zmq.asyncio
from prometheus_client import Counter, Histogram
from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope
from schemas.messages import (
    BarData,
    SpreadMetrics,
    TechnicalIndicatorSnapshot,
    TickData,
)
from services.market_data.app.indicators import IndicatorSnapshot, IndicatorState
from services.market_data.app.storage import MarketDataStore

logger = logging.getLogger(__name__)
ZMQ_MESSAGES = Counter(
    "market_data_zmq_messages_total",
    "MT5 messages received via ZeroMQ.",
    ["data_type"],
)
ZMQ_INGEST_LAG = Histogram(
    "market_data_zmq_ingest_lag_seconds",
    "Difference between the MT5 payload timestamp and ZeroMQ receive time.",
    ["data_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5, 30, 300),
)
TICK_CHANNEL = "market.ticks"
BAR_CHANNEL = "market.bars"
Key = TypeVar("Key", bound=Hashable)


class MarketDataConsumer:
    def __init__(
        self,
        bus: RedisEventBus,
        store: MarketDataStore,
        *,
        on_bar: Callable[[BarData, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.on_bar = on_bar
        self.indicators: dict[tuple[str, str], IndicatorState] = {}
        self._seen_tick_keys: set[UUID] = set()
        self._seen_tick_order: deque[UUID] = deque(maxlen=100_000)
        self._seen_bar_keys: set[tuple[str, str, object]] = set()
        self._seen_bar_order: deque[tuple[str, str, object]] = deque(maxlen=100_000)
        self._zmq_context = zmq.asyncio.Context()
        self._stopping = asyncio.Event()

    @staticmethod
    def _remember_key(key: Key, keys: set[Key], order: deque[Key]) -> bool:
        if key in keys:
            return False
        if len(order) == order.maxlen:
            keys.discard(order[0])
        order.append(key)
        keys.add(key)
        return True

    def _state(self, symbol: str, timeframe: str) -> IndicatorState:
        key = (symbol, timeframe)
        if key not in self.indicators:
            self.indicators[key] = IndicatorState(
                ema_period=int(os.environ.get("INDICATOR_EMA_PERIOD", "20")),
                atr_period=int(os.environ.get("INDICATOR_ATR_PERIOD", "14")),
                rsi_period=int(os.environ.get("INDICATOR_RSI_PERIOD", "14")),
                spread_period=int(os.environ.get("INDICATOR_SPREAD_PERIOD", "20")),
            )
        return self.indicators[key]

    async def handle_tick(
        self,
        tick: TickData,
        *,
        trace_id: str | None = None,
        publish_event: bool = False,
    ) -> IndicatorSnapshot:
        key = tick.tick_id
        if key in self._seen_tick_keys:
            return self._state(tick.symbol, "tick").snapshot()

        resolved_trace_id = trace_id if trace_id else uuid4().hex
        await self.store.store_tick(tick)
        snapshot = self._state(tick.symbol, "tick").update_tick(tick)
        self._remember_key(key, self._seen_tick_keys, self._seen_tick_order)
        if publish_event:
            await self.bus.publish(
                TICK_CHANNEL,
                tick,
                trace_id=resolved_trace_id,
                event_type="TickData",
            )
        spread = tick.ask - tick.bid
        await self.bus.publish(
            "market.indicators",
            SpreadMetrics(
                symbol=tick.symbol,
                timestamp=tick.timestamp,
                spread=spread,
                spread_ma=snapshot.spread_ma or spread,
                spread_ratio=snapshot.spread_ratio,
                sample_count=snapshot.spread_sample_count,
            ),
            trace_id=resolved_trace_id,
            event_type="SpreadMetrics",
        )
        logger.debug(
            "Tick stored symbol=%s bid=%s ask=%s spread_ma=%s",
            tick.symbol,
            tick.bid,
            tick.ask,
            snapshot.spread_ma,
        )
        return snapshot

    async def handle_bar(
        self,
        bar: BarData,
        trace_id: str | None = None,
        *,
        publish_event: bool = False,
    ) -> IndicatorSnapshot:
        key = (bar.symbol, bar.timeframe, bar.timestamp)
        if key in self._seen_bar_keys:
            return self._state(bar.symbol, bar.timeframe).snapshot()

        resolved_trace_id = trace_id if trace_id else uuid4().hex
        await self.store.store_bar(bar)
        snapshot = self._state(bar.symbol, bar.timeframe).update_bar(bar)
        self._remember_key(key, self._seen_bar_keys, self._seen_bar_order)
        if publish_event:
            await self.bus.publish(
                BAR_CHANNEL,
                bar,
                trace_id=resolved_trace_id,
                event_type="BarData",
            )
        await self.bus.publish(
            "market.technicals",
            TechnicalIndicatorSnapshot(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                timestamp=bar.timestamp,
                ema=snapshot.ema,
                atr=snapshot.atr,
                rsi=snapshot.rsi,
            ),
            trace_id=resolved_trace_id,
            event_type="TechnicalIndicatorSnapshot",
        )
        if self.on_bar is not None:
            await self.on_bar(bar, resolved_trace_id)
        logger.debug(
            "Bar stored symbol=%s timeframe=%s ema=%s atr=%s rsi=%s",
            bar.symbol,
            bar.timeframe,
            snapshot.ema,
            snapshot.atr,
            snapshot.rsi,
        )
        return snapshot

    async def process_event(self, event: EventEnvelope) -> None:
        try:
            if event.event_type == "TickData":
                tick = TickData.model_validate_json(json.dumps(event.payload))
                await self.handle_tick(tick, trace_id=event.trace_id)
            elif event.event_type == "BarData":
                bar = BarData.model_validate_json(json.dumps(event.payload))
                await self.handle_bar(bar, event.trace_id)
            else:
                logger.warning("Ignoring unsupported market event type %s", event.event_type)
        except ValidationError:
            logger.exception("Rejected invalid market event %s", event.event_id)

    async def consume_redis(self) -> None:
        async def consume_channel(channel: str) -> None:
            async for event in self.bus.subscribe(channel):
                await self.process_event(event)

        await asyncio.gather(
            consume_channel(TICK_CHANNEL),
            consume_channel(BAR_CHANNEL),
        )

    async def _consume_zmq_socket(
        self,
        port_variable: str,
        expected_type: str,
    ) -> None:
        port = int(os.environ.get(port_variable, "0"))
        if port == 0:
            return

        socket = self._zmq_context.socket(zmq.PULL)
        socket.setsockopt(zmq.LINGER, 0)
        socket.bind(f"tcp://0.0.0.0:{port}")
        logger.info("Listening for MT5 %s messages on tcp://0.0.0.0:%d", expected_type, port)
        try:
            while not self._stopping.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                except TimeoutError:
                    continue

                try:
                    payload: Any = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise TypeError("ZMQ message must be a JSON object")
                    trace_id = payload.pop("trace_id", None)
                    if expected_type == "tick":
                        tick = TickData.model_validate(payload)
                        ZMQ_INGEST_LAG.labels(data_type="tick").observe(
                            max(
                                0.0,
                                (
                                    datetime.now(UTC) - tick.timestamp
                                ).total_seconds(),
                            )
                        )
                        ZMQ_MESSAGES.labels(data_type="tick").inc()
                        await self.handle_tick(
                            tick,
                            trace_id=trace_id if isinstance(trace_id, str) else None,
                            publish_event=True,
                        )
                    else:
                        bar = BarData.model_validate(payload)
                        ZMQ_INGEST_LAG.labels(data_type="bar").observe(
                            max(
                                0.0,
                                (
                                    datetime.now(UTC) - bar.timestamp
                                ).total_seconds(),
                            )
                        )
                        ZMQ_MESSAGES.labels(data_type="bar").inc()
                        await self.handle_bar(
                            bar,
                            trace_id if isinstance(trace_id, str) else None,
                            publish_event=True,
                        )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
                    logger.exception("Rejected invalid MT5 ZMQ %s message", expected_type)
        finally:
            socket.close(linger=0)

    async def consume_zmq(self) -> None:
        await asyncio.gather(
            self._consume_zmq_socket("MT5_ZMQ_TICK_PORT", "tick"),
            self._consume_zmq_socket("MT5_ZMQ_BAR_PORT", "bar"),
        )

    async def run(self) -> None:
        consumers = [asyncio.create_task(self.consume_redis(), name="redis-market-consumer")]
        if os.environ.get("MT5_ZMQ_ENABLED", "true").lower() in {"1", "true", "yes"}:
            consumers.append(asyncio.create_task(self.consume_zmq(), name="zmq-market-consumer"))
        try:
            await asyncio.gather(*consumers)
        finally:
            self._stopping.set()
            for task in consumers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            self._zmq_context.term()
