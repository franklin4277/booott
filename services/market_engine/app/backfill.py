import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from event_bus.redis_bus import RedisEventBus
from schemas.messages import BarData

logger = logging.getLogger(__name__)

_MARKET_DATA_INGEST_URL = os.environ.get(
    "MARKET_DATA_INGEST_URL", "http://localhost:8020"
)
_MT5_ADAPTER_URL = os.environ.get(
    "MT5_ADAPTER_URL", "http://localhost:8765"
)
_MAX_BARS_PER_REQUEST = 500


def _to_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        logger.warning("Skipping bar field %s=%r; cannot convert to Decimal", name, value)
        return None


class BackfillOrchestrator:
    def __init__(
        self,
        bus: RedisEventBus,
        client: httpx.AsyncClient,
        *,
        symbols: list[str],
        timeframes: list[str],
        history_bars: int,
    ) -> None:
        self.bus = bus
        self.client = client
        self.symbols = symbols
        self.timeframes = timeframes
        self.history_bars = history_bars

    async def check_adapter_health(self) -> bool:
        try:
            response = await self.client.get(
                f"{_MT5_ADAPTER_URL.rstrip('/')}/v1/state",
                headers={"Authorization": f"Bearer {os.environ.get('MT5_ADAPTER_TOKEN', '')}"},
                timeout=httpx.Timeout(5.0, connect=2.0),
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPError, OSError):
            return False

    async def run_backfill(self) -> None:
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                await self._backfill_symbol_timeframe(symbol, timeframe)

    async def _backfill_symbol_timeframe(self, symbol: str, timeframe: str) -> None:
        bars = await self._fetch_history(symbol, timeframe)
        if not bars:
            logger.debug("No history to backfill for %s %s", symbol, timeframe)
            return
        await self.ingest_bars(symbol, timeframe, bars)

    async def _fetch_history(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        return []

    async def ingest_bars(self, symbol: str, timeframe: str, bars: list[dict[str, Any]]) -> None:
        if not bars:
            return
        url = f"{_MARKET_DATA_INGEST_URL.rstrip('/')}/v1/ingest/bar"
        for chunk_start in range(0, len(bars), _MAX_BARS_PER_REQUEST):
            chunk = bars[chunk_start : chunk_start + _MAX_BARS_PER_REQUEST]
            for raw in chunk:
                try:
                    open_ = _to_decimal(raw.get("open"), "open")
                    high = _to_decimal(raw.get("high"), "high")
                    low = _to_decimal(raw.get("low"), "low")
                    close = _to_decimal(raw.get("close"), "close")
                    if None in (open_, high, low, close):
                        raise ValueError("missing OHLC")
                    tick_volume = raw.get("tick_volume", 0)
                    if tick_volume is None:
                        tick_volume = 0
                    spread = raw.get("spread")
                    bar = BarData(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=datetime.fromisoformat(raw["timestamp"]),
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        tick_volume=int(tick_volume),
                        spread=int(spread) if spread is not None else None,
                    )
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Skipping invalid bar for %s %s: %s", symbol, timeframe, exc)
                    continue
                try:
                    response = await self.client.post(
                        url,
                        json=bar.model_dump(mode="json"),
                        headers={"Authorization": f"Bearer {os.environ.get('MT5_ADAPTER_TOKEN', '')}"},
                        timeout=httpx.Timeout(10.0, connect=2.0),
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, OSError) as exc:
                    logger.warning("Backfill ingest failed for %s %s: %s", symbol, timeframe, exc)
