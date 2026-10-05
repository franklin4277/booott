import asyncio
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

import httpx

from schemas.events import EventEnvelope
from schemas.messages import ExecutionReport, ExecutionStatus
from services.trade_monitor.app.monitor import OpenTradeLedger, TradeMonitor


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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=FakeRequest("http://fake"),
                response=self,
            )


class FakeBus:
    def __init__(self, states=None):
        self.states = states or {}
        self.published = []

    async def get_state(self, key):
        return self.states.get(key)

    async def publish(self, channel, payload, **kwargs):
        self.published.append((channel, payload, kwargs))
        return 1


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url in self.responses:
            return self.responses[url]
        return FakeResponse(200, json={"account": {"equity": 10000}})

    async def post(self, *args, **kwargs):
        raise AssertionError("Unexpected POST")


def make_report(status: ExecutionStatus) -> ExecutionReport:
    return ExecutionReport(
        execution_id=uuid4(),
        order_id=uuid4(),
        status=status,
        executed_volume=Decimal("0.01"),
        fill_price=Decimal("1.0"),
        timestamp=datetime.now(timezone.utc),
        trace_id="trace-trade-test",
    )


class TradeMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_ledger_adds_filled_trade(self):
        ledger = OpenTradeLedger()
        report = make_report(ExecutionStatus.FILLED)
        ledger.upsert(report)
        self.assertEqual(ledger.count(), 1)
        trades = ledger.get_open_trades()
        self.assertEqual(trades[0]["status"], "filled")

    async def test_ledger_removes_cancelled_trade(self):
        ledger = OpenTradeLedger()
        report = make_report(ExecutionStatus.FILLED)
        ledger.upsert(report)
        self.assertEqual(ledger.count(), 1)
        report2 = make_report(ExecutionStatus.CANCELLED)
        report2.execution_id = report.execution_id
        ledger.upsert(report2)
        self.assertEqual(ledger.count(), 0)

    async def test_trade_monitor_publishes_alert_after_two_consecutive_checks(self):
        bus = FakeBus()
        client = FakeClient({
            "http://localhost:8765/v1/account": FakeResponse(
                200, json={"account": {"equity": 9000}}
            ),
        })
        ledger = OpenTradeLedger()
        with patch.dict(os.environ, {"TRADE_MAX_AGE_HOURS": "0", "TRADE_ADVERSE_MOVE_PCT": "0"}):
            monitor = TradeMonitor(bus, client, ledger)

        report = make_report(ExecutionStatus.FILLED)
        ledger.upsert(report)

        await monitor._check_open_trades()
        self.assertEqual(len(bus.published), 0)

        await monitor._check_open_trades()
        self.assertEqual(len(bus.published), 1)
        self.assertEqual(bus.published[0][0], "system.alerts")
        await monitor.aclose()

    async def test_trade_monitor_skips_alert_on_single_check(self):
        bus = FakeBus()
        client = FakeClient({
            "http://localhost:8765/v1/account": FakeResponse(
                200, json={"account": {"equity": 9000}}
            ),
        })
        ledger = OpenTradeLedger()
        with patch.dict(os.environ, {"TRADE_MAX_AGE_HOURS": "0", "TRADE_ADVERSE_MOVE_PCT": "0"}):
            monitor = TradeMonitor(bus, client, ledger)

        report = make_report(ExecutionStatus.FILLED)
        ledger.upsert(report)

        await monitor._check_open_trades()
        self.assertEqual(len(bus.published), 0)
        await monitor.aclose()


if __name__ == "__main__":
    unittest.main()
