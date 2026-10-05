import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus
from schemas.messages import PatternSetupSignal
from services.ai_engine.app.gateway import AIGateway
from services.ai_engine.app.models import (
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIAvailability,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
PATTERN_CHANNEL = "signals.pattern"
ANALYSIS_CHANNEL = "signals.ai"


async def consume_pattern_signals(
    bus: RedisEventBus,
    gateway: AIGateway,
) -> None:
    async for event in bus.subscribe(PATTERN_CHANNEL):
        try:
            signal = PatternSetupSignal.model_validate_json(
                json.dumps(event.payload)
            )
        except (ValidationError, TypeError, ValueError):
            logger.exception("Rejected invalid pattern signal event %s", event.event_id)
            continue

        signal = signal.model_copy(update={"trace_id": event.trace_id})
        request = AIAnalysisRequest(signal=signal)
        response = await gateway.analyze(request)
        try:
            await bus.publish(
                ANALYSIS_CHANNEL,
                response.result,
                trace_id=event.trace_id,
                event_type="AIAnalysisResult",
            )
        except Exception:
            logger.exception(
                "Could not publish AI analysis for pattern event %s",
                event.event_id,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    gateway = AIGateway(bus)
    await gateway.publish_state()
    stop_status_publisher = asyncio.Event()
    signal_worker = asyncio.create_task(
        consume_pattern_signals(bus, gateway),
        name="ai-pattern-signal-consumer",
    )
    status_worker = asyncio.create_task(
        gateway.publish_offline_while_open(stop_status_publisher),
        name="ai-state-publisher",
    )
    app.state.ai_gateway = gateway
    try:
        yield
    finally:
        stop_status_publisher.set()
        signal_worker.cancel()
        status_worker.cancel()
        await asyncio.gather(signal_worker, status_worker, return_exceptions=True)
        await bus.aclose()


app = FastAPI(title="ai-engine", version="0.2.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    gateway: AIGateway = app.state.ai_gateway
    available = gateway.is_available
    return {
        "status": "ok" if available else "degraded",
        "service": "ai-engine",
        "ai_state": (
            AIAvailability.ONLINE.value
            if available
            else AIAvailability.OFFLINE.value
        ),
        "circuit_state": gateway.breaker.state.value,
        "provider_configured": "yes" if gateway.provider_configured else "no",
    }


@app.post("/analyze", response_model=AIAnalysisResponse, tags=["analysis"])
async def analyze(request: Request) -> AIAnalysisResponse:
    gateway: AIGateway = app.state.ai_gateway
    try:
        analysis_request = AIAnalysisRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False),
        ) from exc
    try:
        return await gateway.analyze(analysis_request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
