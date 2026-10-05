import asyncio
import hmac
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger(__name__)

router = APIRouter()

_MARKET_DATA_INGEST_URL = os.environ.get(
    "MARKET_DATA_INGEST_URL", "http://localhost:8020"
).rstrip("/")
_RECONCILIATION_URL = os.environ.get(
    "RECONCILIATION_URL", "http://localhost:8010"
).rstrip("/")
_EXPECTED_API_KEY = ""


def _port(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is not None:
        try:
            return int(value)
        except ValueError:
            pass
    return default


_HEALTH_TARGETS = [
    ("market-data", f"http://127.0.0.1:{_port('MARKET_DATA_INGEST_PORT', 8020)}/health"),
    ("reconciliation", f"http://127.0.0.1:{_port('RECONCILIATION_PORT', 8010)}/health"),
    ("risk-engine", f"http://127.0.0.1:{_port('RISK_ENGINE_PORT', 8001)}/health"),
    ("pattern-engine", f"http://127.0.0.1:{_port('PATTERN_ENGINE_PORT', 8002)}/health"),
    ("ai-engine", f"http://127.0.0.1:{_port('AI_ENGINE_PORT', 8003)}/health"),
    ("execution-engine", f"http://127.0.0.1:{_port('EXECUTION_ENGINE_PORT', 8004)}/health"),
    ("telegram-bot", f"http://127.0.0.1:{_port('TELEGRAM_BOT_PORT', 8005)}/health"),
    ("market-engine", f"http://127.0.0.1:{_port('MARKET_ENGINE_PORT', 8006)}/health"),
    ("trade-monitor", f"http://127.0.0.1:{_port('TRADE_MONITOR_PORT', 8007)}/health"),
]


def _set_expected_api_key(value: str) -> None:
    global _EXPECTED_API_KEY
    _EXPECTED_API_KEY = value


def _require_api_key(api_key: str | None) -> None:
    expected = _EXPECTED_API_KEY
    if not expected or not api_key or not hmac.compare_digest(
        api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Api-Key.",
        )


def _forward_headers(request: Request, extra: set[str] | None = None) -> dict[str, str]:
    keep = {"authorization", "content-type", "x-reconciliation-token"}
    if extra:
        keep.update(extra)
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() in keep
    }


@router.api_route("/v1/ingest/tick", methods=["POST"], status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def proxy_ingest_tick(request: Request) -> dict[str, str]:
    _require_api_key(request.headers.get("X-Api-Key"))
    body = await request.body()
    headers = _forward_headers(request)
    mt5_token = request.app.state.mt5_adapter_token
    if mt5_token:
        headers["Authorization"] = f"Bearer {mt5_token}"
    response = await request.app.state.http_client.post(
        f"{_MARKET_DATA_INGEST_URL}/v1/ingest/tick",
        content=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


@router.api_route("/v1/ingest/bar", methods=["POST"], status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def proxy_ingest_bar(request: Request) -> dict[str, str]:
    _require_api_key(request.headers.get("X-Api-Key"))
    body = await request.body()
    headers = _forward_headers(request)
    mt5_token = request.app.state.mt5_adapter_token
    if mt5_token:
        headers["Authorization"] = f"Bearer {mt5_token}"
    response = await request.app.state.http_client.post(
        f"{_MARKET_DATA_INGEST_URL}/v1/ingest/bar",
        content=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


@router.post("/reconcile", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def proxy_reconcile(request: Request) -> dict[str, Any]:
    _require_api_key(request.headers.get("X-Api-Key"))
    body = await request.body()
    headers = _forward_headers(request)
    response = await request.app.state.http_client.post(
        f"{_RECONCILIATION_URL}/reconcile",
        content=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


@router.post("/safe-mode/clear", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def proxy_clear_safe_mode(request: Request) -> dict[str, Any]:
    _require_api_key(request.headers.get("X-Api-Key"))
    body = await request.body()
    headers = _forward_headers(request)
    response = await request.app.state.http_client.post(
        f"{_RECONCILIATION_URL}/safe-mode/clear",
        content=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


@router.post("/safe-mode/activate", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def proxy_activate_safe_mode(request: Request) -> dict[str, Any]:
    _require_api_key(request.headers.get("X-Api-Key"))
    body = await request.body()
    headers = _forward_headers(request)
    response = await request.app.state.http_client.post(
        f"{_RECONCILIATION_URL}/safe-mode/activate",
        content=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


@router.get("/health", response_model=None)
async def composite_health(request: Request) -> dict[str, Any]:
    client = request.app.state.http_client
    results: dict[str, Any] = {}
    overall_healthy = True
    tasks = [
        client.get(url, timeout=httpx.Timeout(2.0, connect=1.0))
        for _, url in _HEALTH_TARGETS
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    for (name, _), response in zip(_HEALTH_TARGETS, responses):
        if isinstance(response, Exception):
            results[name] = {"status": "unreachable"}
            overall_healthy = False
            continue
        try:
            results[name] = response.json()
            if response.json().get("status") != "ok":
                overall_healthy = False
        except (ValueError, TypeError):
            results[name] = {"status": "unreachable"}
            overall_healthy = False
    return {"status": "ok" if overall_healthy else "degraded", "services": results}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
