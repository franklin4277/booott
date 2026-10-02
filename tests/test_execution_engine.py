import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from schemas.events import EventEnvelope
from schemas.messages import (
    ExecutionStatus,
    OrderType,
    SignedOrderPayload,
    TradeSide,
)
from services.execution_engine.app.worker import ExecutionWorker
from utils.security import sign_payload

SIGNING_SECRET = "test-order-signing-secret-with-at-least-32-bytes"


class FakeBus:
    def __init__(self, states=None):
        self.states = states or {}
        self.published = []

    async def get_state(self, key):
        return self.states.get(key)

    async def publish(self, channel, payload, **kwargs):
        self.published.append((channel, payload, kwargs))


class FakeHttpClient:
    def __init__(self):
        self.posts = []

    async def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("Unexpected broker order request")


class ExecutionWorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_signed_order(self, *, expires_in_seconds=5):
        now = datetime.now(timezone.utc)
        issued_at = now + timedelta(seconds=expires_in_seconds - 5)
        order = SignedOrderPayload(
            intent_id=uuid4(),
            symbol="EURUSD",
            side=TradeSide.BUY,
            order_type=OrderType.MARKET,
            volume=Decimal("0.01"),
            stop_loss=Decimal("1.05"),
            take_profit=Decimal("1.15"),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=5),
            trace_id="trace-execution-test",
        )
        return sign_payload(order, SIGNING_SECRET)

    def make_event(self, order):
        return EventEnvelope(
            event_type="SignedOrderPayload",
            trace_id=order.trace_id,
            payload=order.model_dump(mode="json"),
        )

    def make_worker(self, bus, client, *, paper, live):
        with patch.dict(os.environ, {"ORDER_SIGNING_SECRET": SIGNING_SECRET}):
            return ExecutionWorker(
                bus,
                type("Adapter", (), {"base_url": "http://mt5", "client": client})(),
                paper_trading=paper,
                live_trading_enabled=live,
            )

    async def test_paper_mode_records_report_without_routing_order(self):
        bus = FakeBus()
        client = FakeHttpClient()
        worker = self.make_worker(bus, client, paper=True, live=True)

        await worker.process_event(self.make_event(self.make_signed_order()))

        self.assertEqual(client.posts, [])
        self.assertEqual(bus.published[0][0], "execution.reports")
        self.assertEqual(bus.published[0][1].status, ExecutionStatus.CANCELLED)

    async def test_invalid_signature_is_rejected_without_routing_order(self):
        bus = FakeBus()
        client = FakeHttpClient()
        worker = self.make_worker(bus, client, paper=False, live=True)
        order = self.make_signed_order().model_copy(update={"signature": "0" * 64})

        await worker.process_event(self.make_event(order))

        self.assertEqual(client.posts, [])
        self.assertEqual(bus.published[0][1].status, ExecutionStatus.REJECTED)

    async def test_safe_mode_blocks_live_order_before_adapter_call(self):
        bus = FakeBus(
            {
                "system:safe_mode": {"enabled": True},
                "system:kill_switch": {"mode": "RUNNING"},
            }
        )
        client = FakeHttpClient()
        worker = self.make_worker(bus, client, paper=False, live=True)

        await worker.process_event(self.make_event(self.make_signed_order()))

        self.assertEqual(client.posts, [])
        self.assertEqual(bus.published[0][1].status, ExecutionStatus.REJECTED)

    async def test_expired_order_is_not_routed(self):
        bus = FakeBus(
            {
                "system:safe_mode": {"enabled": False},
                "system:kill_switch": {"mode": "RUNNING"},
            }
        )
        client = FakeHttpClient()
        worker = self.make_worker(bus, client, paper=False, live=True)

        await worker.process_event(
            self.make_event(self.make_signed_order(expires_in_seconds=-1))
        )

        self.assertEqual(client.posts, [])
        self.assertEqual(bus.published[0][1].status, ExecutionStatus.FAILED)
