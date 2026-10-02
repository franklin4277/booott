import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from event_bus.redis_bus import RedisEventBus
from services.execution_engine.app.worker import ExecutionWorker
from services.reconciliation.app.mt5_client import MT5AdapterClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    mt5 = MT5AdapterClient()
    worker = ExecutionWorker(bus, mt5)
    task = asyncio.create_task(worker.run(), name="approved-order-executor")
    app.state.execution_worker = worker
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await mt5.aclose()
        await bus.aclose()


app = FastAPI(title="mt5-execution-engine", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    worker: ExecutionWorker = app.state.execution_worker
    if worker.paper_trading:
        mode = "paper"
    elif worker.live_trading_enabled:
        mode = "live"
    else:
        mode = "disabled"
    return {"status": "ok", "service": "execution-engine", "mode": mode}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
