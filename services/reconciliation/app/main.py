import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from database.repository import LedgerRepository
from database.session import create_database_engine
from event_bus.redis_bus import RedisEventBus
from services.reconciliation.app.consumer import LedgerEventConsumer
from services.reconciliation.app.engine import ReconciliationEngine
from services.reconciliation.app.models import ReconciliationReport, SafeModeState
from services.reconciliation.app.mt5_client import (
    MT5AdapterClient,
    MT5AdapterUnavailable,
)
from services.reconciliation.app.notifier import CriticalNotifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_database_engine()
    repository = LedgerRepository(engine)
    bus = RedisEventBus()
    mt5 = MT5AdapterClient()
    notifier = CriticalNotifier()
    reconciliation = ReconciliationEngine(repository, mt5, bus, notifier)
    await reconciliation.initialize()
    ledger_consumer = LedgerEventConsumer(bus, repository)
    reconciliation_worker = asyncio.create_task(
        reconciliation.run_forever(), name="mt5-state-reconciliation"
    )
    ledger_worker = asyncio.create_task(
        ledger_consumer.run(), name="ledger-event-consumer"
    )
    app.state.reconciliation = reconciliation
    try:
        yield
    finally:
        for worker in (reconciliation_worker, ledger_worker):
            worker.cancel()
        await asyncio.gather(
            reconciliation_worker, ledger_worker, return_exceptions=True
        )
        await notifier.aclose()
        await mt5.aclose()
        await bus.aclose()
        await repository.aclose()


app = FastAPI(
    title="mt5-state-reconciliation",
    version="0.1.0",
    lifespan=lifespan,
)


def _authorize(engine: ReconciliationEngine, token: str | None) -> None:
    if not engine.authenticate_manual_request(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reconciliation authorization token.",
        )


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    engine: ReconciliationEngine = app.state.reconciliation
    healthy = engine.last_snapshot_at is not None and not engine.state.enabled
    return {
        "status": "ok" if healthy else "degraded",
        "service": "reconciliation",
        "safe_mode": "enabled" if engine.state.enabled else "disabled",
        "last_reconciliation": (
            "available" if engine.last_reconciliation is not None else "pending"
        ),
    }


@app.get("/state", response_model=SafeModeState, tags=["operations"])
async def state() -> SafeModeState:
    return app.state.reconciliation.state


@app.post(
    "/reconcile",
    response_model=ReconciliationReport,
    tags=["operations"],
)
async def reconcile(
    x_reconciliation_token: str | None = Header(default=None),
) -> ReconciliationReport:
    engine: ReconciliationEngine = app.state.reconciliation
    _authorize(engine, x_reconciliation_token)
    try:
        return await engine.run_once("manual")
    except MT5AdapterUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MT5 broker state is unavailable; SAFE_MODE remains enabled.",
        ) from exc


@app.post(
    "/safe-mode/clear",
    response_model=SafeModeState,
    tags=["operations"],
)
async def clear_safe_mode(
    x_reconciliation_token: str | None = Header(default=None),
) -> SafeModeState:
    engine: ReconciliationEngine = app.state.reconciliation
    _authorize(engine, x_reconciliation_token)
    try:
        return await engine.clear_safe_mode()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@app.post(
    "/safe-mode/activate",
    response_model=SafeModeState,
    tags=["operations"],
)
async def activate_safe_mode(
    reason: str = "Operator activated SAFE_MODE.",
    x_reconciliation_token: str | None = Header(default=None),
) -> SafeModeState:
    engine: ReconciliationEngine = app.state.reconciliation
    _authorize(engine, x_reconciliation_token)
    try:
        return await engine.activate_safe_mode(reason)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
