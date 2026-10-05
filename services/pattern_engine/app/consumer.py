import asyncio
import json
import logging
from datetime import UTC, datetime

from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope
from schemas.messages import BarData, SpreadMetrics
from services.pattern_engine.app.engine import PatternEngine

logger = logging.getLogger(__name__)
BAR_CHANNEL = "market.bars"
SIGNAL_CHANNEL = "signals.pattern"
BASE_TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
}


class PatternConsumer:
    def __init__(
        self,
        bus: RedisEventBus,
        engine: PatternEngine | None = None,
    ) -> None:
        self.bus = bus
        self.engine = engine or PatternEngine()

    async def process_event(self, event: EventEnvelope) -> None:
        if event.event_type == "SpreadMetrics":
            try:
                metrics = SpreadMetrics.model_validate_json(json.dumps(event.payload))
            except ValidationError:
                logger.exception("Rejected invalid spread metrics event %s", event.event_id)
                return
            if metrics.spread_ratio is not None:
                self.engine.record_spread_ratio(metrics.symbol, metrics.spread_ratio)
            return
        if event.event_type != "BarData":
            logger.warning("Ignoring unsupported market event type %s", event.event_type)
            return
        try:
            bar = BarData.model_validate_json(json.dumps(event.payload))
        except ValidationError:
            logger.exception("Rejected invalid bar event %s", event.event_id)
            return

        signal = self.engine.on_bar(bar)
        if signal is None:
            return
        timeframe_minutes = BASE_TIMEFRAME_MINUTES.get(bar.timeframe)
        if timeframe_minutes is None:
            logger.error(
                "Pattern engine produced a signal for unsupported timeframe %s",
                bar.timeframe,
            )
            return
        bar_age_seconds = (
            datetime.now(UTC) - bar.timestamp.astimezone(UTC)
        ).total_seconds()
        bar_close_age_seconds = bar_age_seconds - timeframe_minutes * 60
        if bar_close_age_seconds < -5 or bar_close_age_seconds > 60:
            logger.info(
                "Skipping stale pattern signal symbol=%s timeframe=%s close_age_seconds=%.1f",
                bar.symbol,
                bar.timeframe,
                bar_close_age_seconds,
            )
            return

        signal = signal.model_copy(update={"trace_id": event.trace_id})
        await self.bus.publish(
            SIGNAL_CHANNEL,
            signal,
            trace_id=event.trace_id,
            event_type="PatternSetupSignal",
        )
        logger.info(
            "Published pattern signal id=%s symbol=%s side=%s evidence=%s",
            signal.signal_id,
            signal.symbol,
            signal.side,
            signal.attributes["evidence"],
        )

    async def run(self) -> None:
        async def consume(channel: str) -> None:
            async for event in self.bus.subscribe(channel):
                await self.process_event(event)

        await asyncio.gather(
            consume(BAR_CHANNEL),
            consume("market.indicators"),
        )
