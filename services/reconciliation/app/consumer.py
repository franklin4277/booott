import asyncio
import logging

from database.repository import LedgerRepository
from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope

logger = logging.getLogger(__name__)
LEDGER_CHANNELS = (
    "market.bars",
    "signals.pattern",
    "signals.ai",
    "orders.approved",
    "execution.reports",
    "risk.portfolio",
    "risk.rejections",
    "system.safe_mode",
    "system.alerts",
)


class LedgerEventConsumer:
    def __init__(self, bus: RedisEventBus, repository: LedgerRepository) -> None:
        self.bus = bus
        self.repository = repository

    async def run(self) -> None:
        async def consume(channel: str) -> None:
            async for event in self.bus.subscribe(channel):
                await self._record(event, channel)

        await asyncio.gather(*(consume(channel) for channel in LEDGER_CHANNELS))

    async def _record(self, event: EventEnvelope, channel: str) -> None:
        try:
            await self.repository.record_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Could not persist ledger event event_id=%s channel=%s",
                event.event_id,
                channel,
            )
