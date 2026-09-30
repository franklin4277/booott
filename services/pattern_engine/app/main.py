import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from event_bus.redis_bus import RedisEventBus
from services.pattern_engine.app.consumer import PatternConsumer

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    consumer = PatternConsumer(bus)
    worker = asyncio.create_task(consumer.run(), name="pattern-engine-consumer")
    app.state.pattern_consumer = consumer
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bus.aclose()


app = FastAPI(title="pattern-engine", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pattern-engine"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
