import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from services.api_gateway.app.routes import _set_expected_api_key, router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    api_key = app.state.api_key
    _set_expected_api_key(api_key)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=2.0),
        follow_redirects=False,
    )
    app.state.http_client = client
    logger.info("api-gateway started with shared httpx client")
    try:
        yield
    finally:
        await client.aclose()


API_GATEWAY_KEY = os.environ.get("API_GATEWAY_KEY", "replace-with-a-separate-random-32-byte-secret")

app = FastAPI(title="api-gateway", version="0.1.0", lifespan=lifespan)
app.state.api_key = API_GATEWAY_KEY
app.state.mt5_adapter_token = os.environ.get("MT5_ADAPTER_TOKEN", "")
app.include_router(router)
