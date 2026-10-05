import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from event_bus.redis_bus import RedisEventBus
from schemas.messages import BarData, TickData
from services.market_data.app.consumer import MarketDataConsumer
from services.market_data.app.storage import MarketDataStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DB_CONNECTIONS = Gauge(
    "trading_db_connections",
    "Current SQLAlchemy database connections in the market-data pool.",
    ["state"],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    store = MarketDataStore()
    await store.initialize()
    consumer = MarketDataConsumer(bus, store)
    worker = asyncio.create_task(consumer.run(), name="market-data-consumer")
    app.state.market_data_store = store
    app.state.market_data_consumer = consumer
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bus.aclose()
        await store.aclose()


app = FastAPI(title="market-data", version="0.1.0", lifespan=lifespan)


def _authorize_ingest(token: str | None) -> None:
    expected = os.environ.get("MT5_ADAPTER_TOKEN", "")
    if len(expected.encode("utf-8")) < 32 or not token or not hmac.compare_digest(
        token, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MT5 market-ingest authorization token.",
        )


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "market-data"}


@app.post("/v1/ingest/tick", status_code=status.HTTP_202_ACCEPTED)
async def ingest_tick(
    tick: TickData,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _authorize_ingest(authorization)
    consumer: MarketDataConsumer = app.state.market_data_consumer
    await consumer.handle_tick(tick, trace_id=tick.tick_id.hex, publish_event=True)
    return {"status": "accepted", "event_id": str(tick.tick_id)}


@app.post("/v1/ingest/bar", status_code=status.HTTP_202_ACCEPTED)
async def ingest_bar(
    bar: BarData,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _authorize_ingest(authorization)
    consumer: MarketDataConsumer = app.state.market_data_consumer
    await consumer.handle_bar(bar, publish_event=True)
    return {
        "status": "accepted",
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "timestamp": bar.timestamp.isoformat(),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    store: MarketDataStore = app.state.market_data_store
    pool = store.engine.pool
    DB_CONNECTIONS.labels(state="checked_out").set(pool.checkedout())
    DB_CONNECTIONS.labels(state="pool_size").set(pool.size())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
