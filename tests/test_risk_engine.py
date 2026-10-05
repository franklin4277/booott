import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from schemas.events import EventEnvelope
from schemas.messages import (
    AccountState,
    AIAnalysisResult,
    AIRecommendation,
    ExecutionReport,
    ExecutionStatus,
    PatternSetupSignal,
    SignedOrderPayload,
    TradeSide,
)
from services.risk_engine.app.calendar import (
    CalendarUnavailableError,
    EconomicCalendarClient,
)
from services.risk_engine.app.consumer import RiskEventConsumer
from services.risk_engine.app.engine import RiskEngine, RiskLimits
from services.risk_engine.app.models import (
    EconomicCalendarEvent,
    PortfolioSnapshot,
    PositionExposure,
    RiskRejectionCode,
)
from utils.security import verify_payload

SECRET = "unit-test-risk-signing-secret-32-bytes-min"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakeCalendar:
    def __init__(self, event=None, error=None) -> None:
        self.event = event
        self.error = error
        self.calls = []

    async def has_high_impact_event(self, currencies, timestamp, *, embargo_minutes):
        self.calls.append((currencies, timestamp, embargo_minutes))
        if self.error is not None:
            raise self.error
        return self.event


class FakeBus:
    def __init__(self) -> None:
        self.published = []
        self.state = {"enabled": False}
        self.kill_switch = None

    async def publish(self, channel, payload, **kwargs):
        self.published.append((channel, payload, kwargs))
        return 1

    async def get_state(self, key):
        if key == "system:safe_mode":
            return self.state
        if key == "system:kill_switch":
            return self.kill_switch
        return None


def limits(
    *,
    daily_loss: str = "100",
    heat_pct: str = "2",
    correlated_pct: str = "1",
) -> RiskLimits:
    return RiskLimits(
        max_daily_loss=Decimal(daily_loss),
        max_daily_drawdown_pct=Decimal("5"),
        max_portfolio_heat_pct=Decimal(heat_pct),
        max_correlated_exposure_pct=Decimal(correlated_pct),
        max_position_size=Decimal("0.01"),
        contract_units=Decimal("100000"),
        signal_ttl_seconds=5,
        max_signal_age_seconds=60,
        account_state_max_age_seconds=15,
        news_embargo_minutes=15,
        correlation_threshold=Decimal("0.7"),
        correlation_matrix={
            frozenset({"XAUUSD", "EURUSD"}): Decimal("0.75")
        },
    )


def make_signal(clock: FakeClock, **changes) -> PatternSetupSignal:
    fields = {
        "signal_id": uuid4(),
        "strategy_id": "test-pattern",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "side": TradeSide.BUY,
        "entry_price": Decimal("1.1000"),
        "stop_loss": Decimal("1.0990"),
        "take_profit": Decimal("1.1020"),
        "confidence": Decimal("0.8"),
        "created_at": clock.now,
        "expires_at": clock.now + timedelta(minutes=1),
        "trace_id": "risk-test-trace",
    }
    fields.update(changes)
    return PatternSetupSignal(**fields)


def make_ai_result(signal: PatternSetupSignal, **changes) -> AIAnalysisResult:
    fields = {
        "signal_id": signal.signal_id,
        "provider": "test",
        "model": "test",
        "decision": AIRecommendation.BUY,
        "confidence_score": Decimal("0.9"),
        "risk_multiplier": Decimal("0.5"),
        "reasoning": "Pattern and quantitative conditions align.",
        "invalidated_by": [],
        "created_at": signal.created_at,
        "trace_id": signal.trace_id,
    }
    fields.update(changes)
    return AIAnalysisResult(**fields)


def make_portfolio(
    clock: FakeClock,
    *,
    equity: str = "10000",
    starting_equity: str = "10000",
    positions: list[PositionExposure] | None = None,
    age_seconds: int = 0,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account=AccountState(
            account_id="test-account",
            currency="USD",
            balance=Decimal(equity),
            equity=Decimal(equity),
            margin=Decimal("0"),
            free_margin=Decimal(equity),
            open_positions=len(positions or []),
            timestamp=clock.now - timedelta(seconds=age_seconds),
        ),
        daily_starting_equity=Decimal(starting_equity),
        positions=positions or [],
    )


class CalendarWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_impact_event_is_matched_only_inside_embargo_window(self) -> None:
        class InMemoryCalendar(EconomicCalendarClient):
            def __init__(self, event):
                super().__init__(file_path="unused-calendar-snapshot.tsv")
                self.event = event
                self.requested_window = None

            async def events_between(self, start, end):
                self.requested_window = (start, end)
                return [self.event]

        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        in_window = EconomicCalendarEvent(
            event_id="rate-decision",
            title="Policy announcement",
            timestamp=now + timedelta(minutes=15),
            impact="HIGH",
            currency="usd",
        )
        calendar = InMemoryCalendar(in_window)

        found = await calendar.has_high_impact_event(
            {"USD"},
            now,
            embargo_minutes=15,
        )

        self.assertEqual(found, in_window)
        self.assertEqual(
            calendar.requested_window,
            (now - timedelta(minutes=15), now + timedelta(minutes=15)),
        )

        outside_window = in_window.model_copy(
            update={"timestamp": now + timedelta(minutes=16)}
        )
        calendar.event = outside_window
        self.assertIsNone(
            await calendar.has_high_impact_event({"USD"}, now, embargo_minutes=15)
        )


class MT5CalendarSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_and_filters_fresh_mt5_snapshot(self) -> None:
        now = datetime.now(UTC)
        event_time = now + timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "calendar.tsv"
            snapshot.write_text(
                f"generated_at_utc={int(now.timestamp())}\n"
                f"123\tUSD\tHIGH\t{int(event_time.timestamp())}\tRate decision\n"
                f"124\tEUR\tMEDIUM\t{int(event_time.timestamp())}\tSurvey\n",
                encoding="utf-16",
            )
            calendar = EconomicCalendarClient(snapshot, max_age_seconds=60)

            events = await calendar.events_between(
                now,
                now + timedelta(minutes=15),
            )
            embargo = await calendar.has_high_impact_event(
                {"USD"},
                now,
                embargo_minutes=15,
            )

        self.assertEqual(len(events), 2)
        self.assertEqual(embargo.event_id, "123")

    async def test_missing_stale_or_invalid_mt5_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "calendar.tsv"
            calendar = EconomicCalendarClient(snapshot, max_age_seconds=60)
            with self.assertRaises(CalendarUnavailableError):
                await calendar.events_between(
                    datetime.now(UTC),
                    datetime.now(UTC) + timedelta(minutes=15),
                )

            old_time = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
            snapshot.write_text(f"generated_at_utc={old_time}\n", encoding="utf-16")
            with self.assertRaises(CalendarUnavailableError):
                await calendar.events_between(
                    datetime.now(UTC),
                    datetime.now(UTC) + timedelta(minutes=15),
                )

            snapshot.write_text(
                f"generated_at_utc={int(datetime.now(UTC).timestamp())}\ninvalid\n",
                encoding="utf-16",
            )
            with self.assertRaises(CalendarUnavailableError):
                await calendar.events_between(
                    datetime.now(UTC),
                    datetime.now(UTC) + timedelta(minutes=15),
                )


class RiskEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.bus = FakeBus()
        self.calendar = FakeCalendar()
        self.engine = RiskEngine(
            self.bus,  # type: ignore[arg-type]
            calendar=self.calendar,  # type: ignore[arg-type]
            limits=limits(),
            signing_secret=SECRET,
            clock=self.clock,
        )

    async def prepare(self, **portfolio_options) -> PatternSetupSignal:
        await self.engine.handle_portfolio(
            make_portfolio(self.clock, **portfolio_options)
        )
        signal = make_signal(self.clock)
        await self.engine.handle_pattern_signal(signal)
        return signal

    def rejections(self):
        return [
            payload
            for channel, payload, _ in self.bus.published
            if channel == "risk.rejections"
        ]

    async def test_approves_signs_and_sets_exact_five_second_expiry(self) -> None:
        signal = await self.prepare()

        channel, payload, options = self.bus.published[0]

        self.assertEqual(channel, "orders.approved")
        self.assertIsInstance(payload, SignedOrderPayload)
        self.assertEqual(payload.intent_id, signal.signal_id)
        self.assertEqual(payload.expires_at - payload.issued_at, timedelta(seconds=5))
        self.assertTrue(verify_payload(payload, SECRET))
        self.assertEqual(options["event_type"], "SignedOrderPayload")
        self.assertEqual(self.calendar.calls[0][2], 15)

    async def test_global_no_new_trades_switch_rejects_new_orders(self) -> None:
        self.bus.kill_switch = {"mode": "NO_NEW_TRADES", "enabled": True}
        await self.prepare()

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.SAFE_MODE)
        self.assertFalse(
            any(channel == "orders.approved" for channel, _, _ in self.bus.published)
        )

    async def test_rejects_daily_absolute_and_percentage_drawdown(self) -> None:
        await self.engine.handle_portfolio(
            make_portfolio(self.clock, equity="9890", starting_equity="10000")
        )
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.DAILY_DRAWDOWN_LIMIT)
        self.assertEqual(
            [item[0] for item in self.bus.published],
            ["risk.rejections", "system.alerts"],
        )

    async def test_rejects_stale_account_snapshot(self) -> None:
        await self.engine.handle_portfolio(
            make_portfolio(self.clock, age_seconds=16)
        )
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.ACCOUNT_STATE_STALE)

    async def test_rejects_portfolio_heat_limit(self) -> None:
        position = PositionExposure(
            position_id="open-position",
            symbol="GBPUSD",
            side=TradeSide.BUY,
            risk_amount=Decimal("250"),
        )
        await self.engine.handle_portfolio(
            make_portfolio(self.clock, positions=[position])
        )
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.PORTFOLIO_HEAT_LIMIT)

    async def test_rejects_correlated_exposure(self) -> None:
        position = PositionExposure(
            position_id="gold-position",
            symbol="XAUUSD",
            side=TradeSide.BUY,
            risk_amount=Decimal("150"),
        )
        await self.engine.handle_portfolio(
            make_portfolio(
                self.clock,
                positions=[position],
            )
        )
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(
            self.rejections()[0].code,
            RiskRejectionCode.CORRELATED_EXPOSURE_LIMIT,
        )

    async def test_rejects_news_embargo(self) -> None:
        self.calendar.event = EconomicCalendarEvent(
            event_id="cpi-1",
            title="Inflation release",
            timestamp=self.clock.now,
            impact="high",
            currency="USD",
        )
        await self.engine.handle_portfolio(make_portfolio(self.clock))
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.NEWS_EMBARGO)

    async def test_calendar_failure_fails_closed(self) -> None:
        self.calendar.error = CalendarUnavailableError("test outage")
        await self.engine.handle_portfolio(make_portfolio(self.clock))
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(
            self.rejections()[0].code,
            RiskRejectionCode.NEWS_CALENDAR_UNAVAILABLE,
        )

    async def test_rejects_expired_or_old_signals(self) -> None:
        await self.engine.handle_portfolio(make_portfolio(self.clock))
        expired = make_signal(
            self.clock,
            created_at=self.clock.now - timedelta(minutes=2),
            expires_at=self.clock.now - timedelta(seconds=1),
        )
        await self.engine.handle_pattern_signal(expired)
        old = make_signal(
            self.clock,
            created_at=self.clock.now - timedelta(seconds=61),
            expires_at=self.clock.now + timedelta(seconds=1),
        )
        await self.engine.handle_pattern_signal(old)

        self.assertEqual(
            [item.code for item in self.rejections()],
            [RiskRejectionCode.SIGNAL_EXPIRED, RiskRejectionCode.SIGNAL_TOO_OLD],
        )

    async def test_rejects_invalid_stop_and_target_direction(self) -> None:
        await self.engine.handle_portfolio(make_portfolio(self.clock))
        invalid = make_signal(
            self.clock,
            stop_loss=Decimal("1.1010"),
            take_profit=Decimal("1.1020"),
        )
        await self.engine.handle_pattern_signal(invalid)

        self.assertEqual(
            self.rejections()[0].code,
            RiskRejectionCode.INVALID_RISK_PARAMETERS,
        )

    async def test_safe_mode_blocks_order_approval(self) -> None:
        self.bus.state = {"enabled": True}
        await self.engine.handle_portfolio(make_portfolio(self.clock))
        await self.engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(self.rejections()[0].code, RiskRejectionCode.SAFE_MODE)

    async def test_ai_gate_requires_matching_trade_direction_and_approval(self) -> None:
        with patch.dict(os.environ, {"FEATURE_AI_SIGNALS": "true"}):
            engine = RiskEngine(
                self.bus,  # type: ignore[arg-type]
                calendar=self.calendar,  # type: ignore[arg-type]
                limits=limits(),
                signing_secret=SECRET,
                clock=self.clock,
            )
        await engine.handle_portfolio(make_portfolio(self.clock))
        signal = make_signal(self.clock)
        await engine.handle_pattern_signal(signal)
        self.assertEqual(self.bus.published, [])
        await engine.handle_ai_result(
            make_ai_result(signal, decision=AIRecommendation.SELL)
        )
        self.assertEqual(self.bus.published[0][1].code, RiskRejectionCode.AI_SIDE_MISMATCH)

    async def test_ai_result_can_arrive_before_pattern_signal(self) -> None:
        with patch.dict(os.environ, {"FEATURE_AI_SIGNALS": "true"}):
            engine = RiskEngine(
                self.bus,  # type: ignore[arg-type]
                calendar=self.calendar,  # type: ignore[arg-type]
                limits=limits(),
                signing_secret=SECRET,
                clock=self.clock,
            )
        await engine.handle_portfolio(make_portfolio(self.clock))
        signal = make_signal(self.clock)
        await engine.handle_ai_result(make_ai_result(signal))
        self.assertEqual(self.bus.published, [])
        await engine.handle_pattern_signal(signal)

        self.assertEqual(self.bus.published[0][0], "orders.approved")

    async def test_pending_approved_orders_are_reserved_against_portfolio_heat(self) -> None:
        tight_limits = RiskLimits(
            max_daily_loss=Decimal("100"),
            max_daily_drawdown_pct=Decimal("5"),
            max_portfolio_heat_pct=Decimal("0.002"),
            max_correlated_exposure_pct=Decimal("0.002"),
            max_position_size=Decimal("1"),
            contract_units=Decimal("100"),
            signal_ttl_seconds=5,
            max_signal_age_seconds=60,
            account_state_max_age_seconds=15,
            news_embargo_minutes=15,
            correlation_threshold=Decimal("0.7"),
            correlation_matrix={},
        )
        engine = RiskEngine(
            self.bus,  # type: ignore[arg-type]
            calendar=self.calendar,  # type: ignore[arg-type]
            limits=tight_limits,
            signing_secret=SECRET,
            clock=self.clock,
        )
        await engine.handle_portfolio(make_portfolio(self.clock))

        await engine.handle_pattern_signal(make_signal(self.clock))
        await engine.handle_pattern_signal(make_signal(self.clock))
        await engine.handle_pattern_signal(make_signal(self.clock))

        self.assertEqual(len(engine.risk_reservations), 2)
        self.assertEqual(
            self.bus.published[-2][1].code,
            RiskRejectionCode.PORTFOLIO_HEAT_LIMIT,
        )
        order_id = next(iter(engine.risk_reservations))
        await engine.handle_execution_status(order_id, "cancelled")
        self.assertEqual(len(engine.risk_reservations), 1)

    async def test_execution_report_releases_failed_order_reservation(self) -> None:
        await self.prepare()
        signed = self.bus.published[0][1]
        self.assertIn(str(signed.order_id), self.engine.risk_reservations)
        report = ExecutionReport(
            order_id=signed.order_id,
            status=ExecutionStatus.REJECTED,
            executed_volume=Decimal("0"),
            timestamp=self.clock.now,
        )
        consumer = RiskEventConsumer(self.bus, self.engine)  # type: ignore[arg-type]
        await consumer.process_event(
            EventEnvelope(
                event_type="ExecutionReport",
                trace_id="execution-trace",
                payload=report.model_dump(mode="json"),
            )
        )

        self.assertNotIn(str(signed.order_id), self.engine.risk_reservations)

    async def test_portfolio_snapshot_reconciles_reservation_by_source_order(self) -> None:
        await self.prepare()
        signed = self.bus.published[0][1]
        position = PositionExposure(
            position_id="broker-position-123",
            source_order_id=signed.order_id,
            symbol="EURUSD",
            side=TradeSide.BUY,
            risk_amount=Decimal("10"),
        )

        await self.engine.handle_portfolio(
            make_portfolio(self.clock, positions=[position])
        )

        self.assertNotIn(str(signed.order_id), self.engine.risk_reservations)

    async def test_consumer_intercepts_pattern_ai_and_portfolio_events(self) -> None:
        consumer = RiskEventConsumer(self.bus, self.engine)  # type: ignore[arg-type]
        snapshot = make_portfolio(self.clock)
        portfolio_event = EventEnvelope(
            event_type="PortfolioSnapshot",
            trace_id="portfolio-trace",
            payload=snapshot.model_dump(mode="json"),
        )
        await consumer.process_event(portfolio_event)
        self.assertEqual(self.engine.portfolio, snapshot)

    async def test_consumer_applies_persisted_safe_mode_events(self) -> None:
        consumer = RiskEventConsumer(self.bus, self.engine)  # type: ignore[arg-type]
        event = EventEnvelope(
            event_type="SafeModeState",
            trace_id="safe-mode-trace",
            payload={
                "enabled": True,
                "reason": "unknown position",
                "manual_clear_required": True,
            },
        )
        await consumer.process_event(event)

        self.assertTrue(self.engine.safe_mode)


if __name__ == "__main__":
    unittest.main()
