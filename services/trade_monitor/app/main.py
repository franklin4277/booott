import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from event_bus.redis_bus import RedisEventBus
from services.trade_monitor.app.monitor import (
    OpenTradeLedger,
    TradeMonitor,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LEDGER_SIZE = Gauge(
    "trade_monitor_open_trades",
    "Current number of open trades tracked by trade-monitor.",
)
MT5_CONNECTED = Gauge(
    "trade_monitor_mt5_connected",
    "Whether the MT5 adapter is reachable from trade-monitor.",
)
ALERTS_PUBLISHED = Gauge(
    "trade_monitor_alerts_published_total",
    "Number of trade status alerts published to system.alerts.",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=2.0),
        follow_redirects=False,
    )
    ledger = OpenTradeLedger()
    monitor = TradeMonitor(bus, client, ledger)
    workers = [
        asyncio.create_task(monitor.consume_reports(), name="trade-monitor-consumer"),
        asyncio.create_task(monitor.surveillance_loop(), name="trade-monitor-surveillance"),
    ]
    app.state.trade_monitor = monitor
    try:
        yield
    finally:
        await monitor.aclose()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await client.aclose()
        await bus.aclose()


app = FastAPI(title="trade-monitor", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    monitor: TradeMonitor = app.state.trade_monitor
    count = monitor.ledger.count()
    return {
        "status": "ok",
        "service": "trade-monitor",
        "open_trades": str(count),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    monitor: TradeMonitor = app.state.trade_monitor
    LEDGER_SIZE.set(monitor.ledger.count())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/open-trades", tags=["operations"])
async def open_trades() -> dict[str, Any]:
    monitor: TradeMonitor = app.state.trade_monitor
    trades = monitor.ledger.get_open_trades()
    return {"open_trades": trades}
