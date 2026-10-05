import asyncio
import hmac
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from services.api_gateway.app.dashboard import DASHBOARD_HTML

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


_TRADE_MONITOR_URL = os.environ.get(
    "TRADE_MONITOR_URL", f"http://127.0.0.1:{_port('TRADE_MONITOR_PORT', 8007)}"
).rstrip("/")
_MT5_ADAPTER_URL = os.environ.get(
    "MT5_ADAPTER_URL", f"http://127.0.0.1:{_port('MT5_ADAPTER_PORT', 8765)}"
).rstrip("/")


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


def _require_local_dashboard(request: Request) -> None:
    """Dashboard data is intentionally available only on the local machine."""
    client = request.client
    if client is None or client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The dashboard is available only from localhost.",
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
        headers["Authorization"] = mt5_token
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
        headers["Authorization"] = mt5_token
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


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    _require_local_dashboard(request)
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/dashboard/api/overview", include_in_schema=False)
async def dashboard_overview(request: Request) -> dict[str, Any]:
    """Read-only health and trade data used by the local dashboard."""
    _require_local_dashboard(request)
    health = await composite_health(request)
    open_trades: list[dict[str, Any]] = []
    broker_positions: list[dict[str, Any]] = []
    account: dict[str, Any] | None = None
    account_error: str | None = None
    try:
        response = await request.app.state.http_client.get(
            f"{_TRADE_MONITOR_URL}/v1/open-trades",
            timeout=httpx.Timeout(2.0, connect=1.0),
        )
        response.raise_for_status()
        payload = response.json()
        candidate = payload.get("open_trades", [])
        if isinstance(candidate, list):
            open_trades = candidate
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("Trade monitor data is unavailable for dashboard")
    try:
        response = await request.app.state.http_client.get(
            f"{_MT5_ADAPTER_URL}/v1/account",
            headers={
                "Authorization": f"Bearer {request.app.state.mt5_adapter_token}"
            },
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
        response.raise_for_status()
        payload = response.json()
        account_payload = payload.get("account")
        positions_payload = payload.get("positions")
        if not isinstance(account_payload, dict) or not isinstance(positions_payload, list):
            raise ValueError("MT5 account response is missing account or positions data")
        account = account_payload
        broker_positions = [
            position for position in positions_payload if isinstance(position, dict)
        ]
    except httpx.HTTPStatusError as exc:
        account_error = f"MT5 adapter returned HTTP {exc.response.status_code}"
        logger.warning("MT5 account data is unavailable: %s", account_error)
    except httpx.HTTPError as exc:
        account_error = "MT5 adapter is unreachable"
        logger.warning("MT5 account data is unavailable: %s", exc)
    except (ValueError, TypeError) as exc:
        account_error = "MT5 adapter returned invalid account data"
        logger.warning("MT5 account data is invalid: %s", exc)
    return {
        "health": health,
        "open_trades": open_trades,
        "account": account,
        "broker_positions": broker_positions,
        "account_error": account_error,
    }


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
