import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from event_bus.redis_bus import RedisEventBus
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


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "market-data"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    store: MarketDataStore = app.state.market_data_store
    pool = store.engine.pool
    DB_CONNECTIONS.labels(state="checked_out").set(pool.checkedout())
    DB_CONNECTIONS.labels(state="pool_size").set(pool.size())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
