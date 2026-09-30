import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from event_bus.redis_bus import RedisEventBus
from services.risk_engine.app.consumer import RiskEventConsumer
from services.risk_engine.app.engine import RiskEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    engine = RiskEngine(bus)
    safe_mode = await bus.get_state("system:safe_mode")
    engine.handle_safe_mode(
        True if safe_mode is None else bool(safe_mode.get("enabled", True))
    )
    kill_switch = await bus.get_state("system:kill_switch")
    engine.handle_kill_switch(
        str((kill_switch or {}).get("mode", "RUNNING"))
    )
    consumer = RiskEventConsumer(bus, engine)
    worker = asyncio.create_task(consumer.run(), name="risk-event-consumer")
    app.state.risk_engine = engine
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bus.aclose()


app = FastAPI(title="risk-engine", version="0.2.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    engine: RiskEngine = app.state.risk_engine
    return {
        "status": "ok" if engine.portfolio is not None else "degraded",
        "service": "risk-engine",
        "portfolio_state": "available" if engine.portfolio is not None else "unavailable",
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
