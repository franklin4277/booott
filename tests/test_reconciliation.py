from datetime import datetime, timezone
from decimal import Decimal
import unittest

import httpx
from database.models import Base, Trade
from database.repository import LedgerRepository, ReconciliationOutcome
from services.reconciliation.app.engine import (
    SAFE_MODE_CHANNEL,
    SAFE_MODE_KEY,
    ReconciliationEngine,
)
from services.reconciliation.app.models import (
    MT5AccountSnapshot,
    MT5ClosedDeal,
    MT5Position,
    MT5Snapshot,
    SafeModeState,
)
from services.reconciliation.app.mt5_client import (
    MT5AdapterClient,
    MT5AdapterUnavailable,
)

TOKEN = "test-mt5-adapter-token-with-more-than-32-bytes"
API_TOKEN = "test-reconciliation-api-token-with-more-than-32-bytes"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def snapshot(*positions: MT5Position) -> MT5Snapshot:
    return MT5Snapshot(
        account=MT5AccountSnapshot(
            login="123456",
            currency="USD",
            balance=Decimal("10000"),
            equity=Decimal("10000"),
            margin=Decimal("0"),
            free_margin=Decimal("10000"),
            timestamp=NOW,
        ),
        positions=list(positions),
        pending_orders=[],
        closed_deals=[],
        fetched_at=NOW,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.state = None
        self.outcome = ReconciliationOutcome([], [], 0)

    async def get_safe_mode(self):
        return self.state

    async def update_safe_mode(self, value, at):
        self.state = value

    async def oldest_open_trade_time(self):
        return None

    async def apply_snapshot(self, snapshot, *, proposed_safe_mode, reconciled_at):
        self.state = proposed_safe_mode
        if self.outcome.unknown_positions:
            self.state = {
                "enabled": True,
                "reason": "unknown position",
                "manual_clear_required": True,
                "updated_at": reconciled_at.isoformat(),
            }
        return self.outcome


class FakeMT5:
    def __init__(self, value=None, error=None) -> None:
        self.value = value or snapshot()
        self.error = error
        self.since = None

    async def get_snapshot(self, since=None):
        self.since = since
        if self.error is not None:
            raise self.error
        return self.value


class FakeBus:
    def __init__(self) -> None:
        self.state = None
        self.events = []

    async def get_state(self, key):
        return self.state if key == SAFE_MODE_KEY else None

    async def set_state(self, key, value):
        self.state = value

    async def publish(self, channel, payload, **kwargs):
        self.events.append((channel, payload, kwargs))
        return 1


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts = []

    async def send_critical(self, summary, details, **kwargs):
        self.alerts.append((summary, details, kwargs))


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.mt5 = FakeMT5()
        self.bus = FakeBus()
        self.notifier = FakeNotifier()
        self.engine = ReconciliationEngine(
            self.repository,  # type: ignore[arg-type]
            self.mt5,  # type: ignore[arg-type]
            self.bus,  # type: ignore[arg-type]
            self.notifier,  # type: ignore[arg-type]
            poll_seconds=2,
            manual_token=API_TOKEN,
        )

    async def test_unknown_position_persists_safe_mode_and_sends_critical_alert(self):
        position = MT5Position(
            ticket=123,
            identifier=456,
            symbol="EURUSD",
            side="buy",
            volume=Decimal("0.1"),
            price_open=Decimal("1.1"),
            stop_loss=Decimal("1.09"),
            take_profit=Decimal("1.12"),
            profit=Decimal("0"),
            swap=Decimal("0"),
            opened_at=NOW,
        )
        self.mt5.value = snapshot(position)
        self.repository.outcome = ReconciliationOutcome([position], [], 0)

        await self.engine.initialize()
        report = await self.engine.run_once("startup")

        self.assertTrue(report.safe_mode)
        self.assertEqual(report.unknown_position_tickets, [456])
        self.assertTrue(self.engine.state.manual_clear_required)
        self.assertTrue(self.bus.state["enabled"])
        self.assertIn(SAFE_MODE_CHANNEL, [event[0] for event in self.bus.events])
        self.assertIn("system.alerts", [event[0] for event in self.bus.events])
        self.assertIn("Unknown open position", self.notifier.alerts[0][0])
        with self.assertRaises(RuntimeError):
            await self.engine.clear_safe_mode()

    async def test_operator_safe_mode_activation_is_persisted_and_alerted(self):
        state = await self.engine.activate_safe_mode("Telegram emergency control.")

        self.assertTrue(state.enabled)
        self.assertTrue(state.manual_clear_required)
        self.assertEqual(self.repository.state["reason"], "Telegram emergency control.")
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertIn(SAFE_MODE_CHANNEL, [event[0] for event in self.bus.events])

    async def test_successful_startup_reconciliation_clears_temporary_safe_mode(self):
        await self.engine.initialize()
        report = await self.engine.run_once("startup")

        self.assertFalse(report.safe_mode)
        self.assertFalse(self.engine.state.enabled)
        self.assertFalse(self.repository.state["enabled"])

    async def test_initialize_restores_persisted_safe_mode_state(self):
        self.repository.state = SafeModeState(
            enabled=True,
            reason="unknown broker position",
            manual_clear_required=True,
            updated_at=NOW,
        ).model_dump(mode="json")

        await self.engine.initialize()

        self.assertTrue(self.engine.state.enabled)
        self.assertTrue(self.engine.state.manual_clear_required)
        self.assertTrue(self.bus.state["enabled"])
        self.assertIn(SAFE_MODE_CHANNEL, [event[0] for event in self.bus.events])

    async def test_mt5_disconnect_enters_safe_mode_and_auth_is_constant_time_checked(self):
        await self.engine.initialize()
        await self.engine.run_once("startup")
        self.mt5.error = MT5AdapterUnavailable("unavailable")

        with self.assertRaises(MT5AdapterUnavailable):
            await self.engine.run_once("startup")

        self.assertTrue(self.engine.state.enabled)
        with self.assertRaises(RuntimeError):
            await self.engine.clear_safe_mode()
        self.assertFalse(self.engine.authenticate_manual_request("wrong"))
        self.assertTrue(self.engine.authenticate_manual_request(API_TOKEN))

    async def test_unknown_position_safe_mode_requires_clean_reconcile_then_operator_clear(self):
        position = MT5Position(
            ticket=123,
            identifier=456,
            symbol="EURUSD",
            side="buy",
            volume=Decimal("0.1"),
            price_open=Decimal("1.1"),
            stop_loss=Decimal("1.09"),
            take_profit=Decimal("1.12"),
            profit=Decimal("0"),
            swap=Decimal("0"),
            opened_at=NOW,
        )
        await self.engine.initialize()
        self.repository.outcome = ReconciliationOutcome([position], [], 0)
        await self.engine.run_once("startup")
        self.repository.outcome = ReconciliationOutcome([], [], 0)
        await self.engine.run_once("poll")

        state = await self.engine.clear_safe_mode()
        self.assertFalse(state.enabled)
        self.assertFalse(self.repository.state["enabled"])


class LedgerMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_expected_ledger_tables_and_trade_fields_exist(self):
        self.assertTrue(
            {"accounts", "trades", "signals", "audit_logs", "system_state"}
            <= set(Base.metadata.tables)
        )
        columns = set(Base.metadata.tables["trades"].columns.keys())
        self.assertTrue(
            {
                "ticket",
                "symbol",
                "lot",
                "initial_lot",
                "entry_price",
                "exit_price",
                "stop_loss",
                "take_profit",
                "pnl",
                "status",
                "signal_id",
                "client_order_id",
                "closed_volume",
                "mt5_deal_ids",
            }
            <= columns
        )

    def test_host_adapter_rejects_short_shared_secret(self):
        with self.assertRaises(ValueError):
            MT5AdapterClient(base_url="http://localhost", token="short")
        with self.assertRaises(ValueError):
            MT5AdapterClient(
                base_url="http://public-adapter.example",
                token=TOKEN,
            )

    async def test_mt5_adapter_client_parses_broker_snapshot(self):
        response_body = snapshot().model_dump_json()

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")
            self.assertEqual(request.url.path, "/v1/state")
            return httpx.Response(200, content=response_body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        mt5_client = MT5AdapterClient(
            base_url="https://mt5-host:8765",
            token=TOKEN,
            client=client,
        )

        actual = await mt5_client.get_snapshot(NOW)

        self.assertEqual(actual.account.login, "123456")
        self.assertEqual(actual.account.equity, Decimal("10000"))
        self.assertEqual(actual.fetched_at, NOW)

    async def test_mt5_adapter_client_parses_lightweight_account_status(self):
        response_body = {
            "account": snapshot().account.model_dump(mode="json"),
            "host_cpu_percent": "12.5",
            "host_memory_percent": "63.0",
            "fetched_at": NOW.isoformat(),
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/account")
            return httpx.Response(200, json=response_body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        mt5_client = MT5AdapterClient(
            base_url="https://mt5-host:8765",
            token=TOKEN,
            client=client,
        )

        actual = await mt5_client.get_account_status()

        self.assertEqual(actual.account.login, "123456")
        self.assertEqual(actual.host_cpu_percent, Decimal("12.5"))
        self.assertEqual(actual.host_memory_percent, Decimal("63.0"))

    async def test_mt5_adapter_http_failure_is_treated_as_disconnected(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, request=request)
            )
        )
        self.addAsyncCleanup(client.aclose)
        mt5_client = MT5AdapterClient(
            base_url="https://mt5-host:8765",
            token=TOKEN,
            client=client,
        )
        with self.assertRaises(MT5AdapterUnavailable):
            await mt5_client.get_snapshot()

    def test_closed_deals_update_trade_once_and_accumulate_partial_fills(self):
        trade = Trade(
            ticket=456,
            lot=Decimal("0.2"),
            initial_lot=Decimal("0.2"),
            closed_volume=Decimal("0"),
            mt5_deal_ids=[],
            commission=Decimal("0"),
            swap=Decimal("0"),
            fees=Decimal("0"),
            pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            exit_price=None,
            closed_at=None,
            status="OPEN",
        )
        first_deal = MT5ClosedDeal(
            ticket=8001,
            position_id=456,
            symbol="EURUSD",
            timestamp=NOW,
            price=Decimal("1.2"),
            volume=Decimal("0.1"),
            profit=Decimal("10"),
            commission=Decimal("-1"),
            swap=Decimal("0"),
            fee=Decimal("0"),
        )
        partial_snapshot = snapshot().model_copy(
            update={"closed_deals": [first_deal]}
        )

        self.assertFalse(
            LedgerRepository._reconcile_closed_trade(trade, partial_snapshot, 456)
        )
        self.assertEqual(trade.status, "PARTIALLY_CLOSED")
        self.assertEqual(trade.closed_volume, Decimal("0.1"))
        self.assertEqual(trade.pnl, Decimal("10"))
        self.assertFalse(
            LedgerRepository._reconcile_closed_trade(trade, partial_snapshot, 456)
        )
        second_deal = first_deal.model_copy(
            update={
                "ticket": 8002,
                "timestamp": NOW.replace(second=1),
                "price": Decimal("1.3"),
            }
        )
        full_snapshot = snapshot().model_copy(
            update={"closed_deals": [first_deal, second_deal]}
        )

        self.assertTrue(
            LedgerRepository._reconcile_closed_trade(trade, full_snapshot, 456)
        )
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.closed_volume, Decimal("0.2"))
        self.assertEqual(trade.exit_price, Decimal("1.25"))
        self.assertEqual(trade.pnl, Decimal("20"))
