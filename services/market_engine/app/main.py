import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from event_bus.redis_bus import RedisEventBus
from services.market_engine.app.backfill import BackfillOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BACKFILL_INTERVAL_SECONDS = int(
    os.environ.get("MT5_MARKET_BACKFILL_INTERVAL_SECONDS", "300")
)
SYMBOLS = [
    s.strip()
    for s in os.environ.get("MT5_MARKET_SYMBOLS", "EURUSD").split(",")
    if s.strip()
]
TIMEFRAMES = [
    t.strip()
    for t in os.environ.get("MT5_MARKET_TIMEFRAMES", "M5,M15,H1").split(",")
    if t.strip()
]
HISTORY_BARS = int(os.environ.get("MT5_MARKET_HISTORY_BARS", "128"))

ENGINE_STATUS = Gauge(
    "market_engine_backfill_status",
    "1 when last backfill succeeded, 0 otherwise.",
)
LAST_BACKFILL_AT = Gauge(
    "market_engine_last_backfill_timestamp_seconds",
    "Unix timestamp of the last backfill run.",
)
ADAPTER_AVAILABLE = Gauge(
    "market_engine_adapter_available",
    "1 when the MT5 adapter is reachable, 0 otherwise.",
)


class MarketEngine:
    def __init__(self, bus: RedisEventBus, client: httpx.AsyncClient) -> None:
        self.bus = bus
        self.client = client
        self.orchestrator = BackfillOrchestrator(
            bus=bus,
            client=client,
            symbols=SYMBOLS,
            timeframes=TIMEFRAMES,
            history_bars=HISTORY_BARS,
        )
        self._running = False

    async def initialize(self) -> None:
        self._running = True
        await self._run_backfill_loop()

    async def _run_backfill_loop(self) -> None:
        while self._running:
            backfill_succeeded = False
            try:
                backfill_succeeded = await self._backfill_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market-engine backfill iteration failed")
            try:
                await asyncio.wait_for(
                    asyncio.Event().wait(),
                    timeout=(BACKFILL_INTERVAL_SECONDS if backfill_succeeded else 5),
                )
            except TimeoutError:
                continue

    async def _backfill_once(self) -> bool:
        healthy = await self.orchestrator.check_adapter_health()
        ADAPTER_AVAILABLE.set(1 if healthy else 0)
        if not healthy:
            logger.warning("Skipping backfill because MT5 adapter is unavailable")
            return False
        await self.orchestrator.run_backfill()
        ENGINE_STATUS.set(1)
        LAST_BACKFILL_AT.set(datetime.now(UTC).timestamp())
        await self.bus.publish(
            "market.engine.status",
            {
                "status": "backfill_complete",
                "symbols": SYMBOLS,
                "timeframes": TIMEFRAMES,
                "history_bars": HISTORY_BARS,
            },
            event_type="MarketEngineStatus",
        )
        return True

    async def aclose(self) -> None:
        self._running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=2.0),
        follow_redirects=False,
    )
    engine = MarketEngine(bus, client)
    worker = asyncio.create_task(engine.initialize(), name="market-engine-backfill")
    app.state.market_engine = engine
    try:
        yield
    finally:
        await engine.aclose()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.aclose()
        await bus.aclose()


app = FastAPI(title="market-engine", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    status_val = "ok" if ENGINE_STATUS._value.get() == 1.0 else "degraded"  # type: ignore[attr-defined]
    return {
        "status": status_val,
        "service": "market-engine",
        "adapter_available": "yes" if ADAPTER_AVAILABLE._value.get() == 1.0 else "no",  # type: ignore[attr-defined]
    }


@app.post("/v1/backfill", status_code=status.HTTP_202_ACCEPTED)
async def backfill(
    request: dict[str, Any],
) -> dict[str, str]:
    engine: MarketEngine = app.state.market_engine
    symbol = request.get("symbol")
    timeframe = request.get("timeframe")
    bars = request.get("bars", [])
    if not symbol or not timeframe or not isinstance(bars, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol, timeframe, and bars[] are required.",
        )
    await engine.orchestrator.ingest_bars(symbol, timeframe, bars)
    return {"status": "accepted", "symbol": symbol, "timeframe": timeframe}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
