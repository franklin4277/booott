import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from services.api_gateway.app.main import app as api_gateway_app
from services.api_gateway.app.routes import _set_expected_api_key


def _make_mock_http_client():
    responses = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in responses:
            return responses[url]
        if "/health" in url:
            return httpx.Response(200, json={"status": "ok", "service": "market-data"})
        if "/v1/ingest/bar" in url:
            return httpx.Response(202, json={"status": "accepted", "symbol": "EURUSD"})
        if "/reconcile" in url:
            return httpx.Response(202, json={"trigger": "manual"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=5.0)


class ApiGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _set_expected_api_key("test-api-key")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        api_gateway_app.state.http_client = _make_mock_http_client()

    async def asyncTearDown(self):
        if hasattr(api_gateway_app.state, "http_client"):
            client = api_gateway_app.state.http_client
            del api_gateway_app.state.http_client
            await client.aclose()
        await super().asyncTearDown()

    async def test_missing_api_key_rejects(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/ingest/bar", json={})
        self.assertEqual(response.status_code, 401)

    async def test_wrong_api_key_rejects(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/ingest/bar", json={}, headers={"X-Api-Key": "bad-key"}
            )
        self.assertEqual(response.status_code, 401)

    async def test_valid_api_key_proxies_to_market_data(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/ingest/bar",
                json={
                    "symbol": "EURUSD",
                    "timeframe": "M5",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "tick_volume": 100,
                },
                headers={"X-Api-Key": "test-api-key"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    async def test_composite_health_reports_services(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health", headers={"X-Api-Key": "test-api-key"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("market-data", body["services"])


if __name__ == "__main__":
    unittest.main()
