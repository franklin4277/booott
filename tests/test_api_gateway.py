import unittest

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
        if "/v1/open-trades" in url:
            return httpx.Response(200, json={"open_trades": []})
        if "/v1/account" in url:
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={
                    "account": {
                        "login": "12345678",
                        "currency": "USD",
                        "balance": "10000",
                        "equity": "10100",
                        "margin": "500",
                        "free_margin": "9600",
                    },
                    "positions": [
                        {
                            "ticket": 42,
                            "symbol": "EURUSD",
                            "side": "buy",
                            "volume": "0.1",
                            "price_open": "1.1",
                            "stop_loss": "1.09",
                            "take_profit": "1.12",
                            "profit": "10",
                            "swap": "0",
                            "opened_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
            )
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

    async def test_dashboard_and_overview_are_available_locally(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app, client=("127.0.0.1", 1234)),
            base_url="http://test",
        ) as client:
            page = await client.get("/dashboard")
            overview = await client.get("/dashboard/api/overview")
        self.assertEqual(page.status_code, 200)
        self.assertIn("MT5 Trading Monitor", page.text)
        self.assertEqual(overview.status_code, 200)
        self.assertIn("health", overview.json())
        self.assertEqual(overview.json()["account"]["balance"], "10000")
        self.assertEqual(overview.json()["broker_positions"][0]["ticket"], 42)
        self.assertIsNone(overview.json()["account_error"])

    async def test_dashboard_renders_account_and_live_position_sections(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_gateway_app, client=("127.0.0.1", 1234)),
            base_url="http://test",
        ) as client:
            page = await client.get("/dashboard")

        self.assertIn("Broker account", page.text)
        self.assertIn("Live MT5 positions", page.text)
        self.assertIn("Floating P/L", page.text)


if __name__ == "__main__":
    unittest.main()
