import asyncio
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

import httpx

from schemas.events import EventEnvelope
from schemas.messages import BarData
from services.market_engine.app.backfill import BackfillOrchestrator


class FakeBus:
    def __init__(self):
        self.states = {}
        self.published = []

    async def get_state(self, key):
        return self.states.get(key)

    async def publish(self, channel, payload, **kwargs):
        self.published.append((channel, payload, kwargs))
        return 1


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeResponse:
    def __init__(self, status_code, json_data=None, json=None):
        self.status_code = status_code
        self._json = json or json_data or {}
        self.request = FakeRequest("http://fake")

    def json(self):
        return self._json

    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=FakeRequest("http://fake"),
                response=self,
            )


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url in self.responses:
            return self.responses[url]
        return FakeResponse(200, json={"status": "ok"})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url in self.responses:
            return self.responses[url]
        return FakeResponse(202, json={"status": "accepted"})


class BackfillOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def make_orchestrator(self, client, symbols=None, timeframes=None, history_bars=128):
        return BackfillOrchestrator(
            bus=FakeBus(),
            client=client,
            symbols=symbols or ["EURUSD"],
            timeframes=timeframes or ["M5"],
            history_bars=history_bars,
        )

    async def test_check_adapter_health_returns_true_on_200(self):
        client = FakeClient({
            "http://localhost:8765/v1/state": FakeResponse(200, json={}),
        })
        orchestrator = self.make_orchestrator(client)
        healthy = await orchestrator.check_adapter_health()
        self.assertTrue(healthy)

    async def test_check_adapter_health_returns_false_on_error(self):
        client = FakeClient({
            "http://localhost:8765/v1/state": FakeResponse(500),
        })
        orchestrator = self.make_orchestrator(client)
        healthy = await orchestrator.check_adapter_health()
        self.assertFalse(healthy)

    async def test_ingest_bars_posts_chunks_of_500(self):
        bars = [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "open": Decimal("1.0") + i * Decimal("0.001"),
                "high": Decimal("1.1") + i * Decimal("0.001"),
                "low": Decimal("0.9") + i * Decimal("0.001"),
                "close": Decimal("1.0") + i * Decimal("0.001"),
                "tick_volume": 100,
            }
            for i in range(1200)
        ]
        client = FakeClient({
            "http://market-data:8000/v1/ingest/bar": FakeResponse(202, json={}),
        })
        orchestrator = self.make_orchestrator(client)
        await orchestrator.ingest_bars("EURUSD", "M5", bars)
        self.assertEqual(len(client.calls), 1200)

    async def test_ingest_bars_skips_invalid_bar(self):
        bars = [
            {"timestamp": "bad", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0},
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "open": Decimal("1.0"),
                "high": Decimal("1.1"),
                "low": Decimal("0.9"),
                "close": Decimal("1.0"),
                "tick_volume": 100,
            },
        ]
        client = FakeClient({
            "http://market-data:8000/v1/ingest/bar": FakeResponse(202, json={}),
        })
        orchestrator = self.make_orchestrator(client)
        await orchestrator.ingest_bars("EURUSD", "M5", bars)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
