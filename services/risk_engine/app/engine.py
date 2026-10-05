import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from prometheus_client import Gauge
from redis.exceptions import RedisError

from event_bus.redis_bus import RedisEventBus, RedisEventBusError
from schemas.messages import (
    AIAnalysisResult,
    AIRecommendation,
    OrderType,
    PatternSetupSignal,
    SignedOrderPayload,
)
from services.risk_engine.app.calendar import (
    CalendarUnavailableError,
    EconomicCalendarClient,
)
from services.risk_engine.app.models import (
    PortfolioSnapshot,
    RiskApproval,
    RiskRejection,
    RiskRejectionCode,
)
from utils.security import sign_payload

logger = logging.getLogger(__name__)
OPEN_RISK_PERCENT = Gauge(
    "trading_open_risk_percent",
    "Open portfolio stop-loss risk as a percentage of current equity.",
)
ORDERS_CHANNEL = "orders.approved"
REJECTIONS_CHANNEL = "risk.rejections"
ALERTS_CHANNEL = "system.alerts"


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss: Decimal
    max_daily_drawdown_pct: Decimal
    max_portfolio_heat_pct: Decimal
    max_correlated_exposure_pct: Decimal
    max_position_size: Decimal
    contract_units: Decimal
    signal_ttl_seconds: int
    max_signal_age_seconds: int
    account_state_max_age_seconds: int
    news_embargo_minutes: int
    correlation_threshold: Decimal
    correlation_matrix: dict[frozenset[str], Decimal]
    min_ai_confidence: Decimal = Decimal("0.65")

    @classmethod
    def from_environment(cls) -> "RiskLimits":
        correlations_json = os.environ.get(
            "RISK_CORRELATION_MATRIX",
            '{"XAUUSD|EURUSD":0.75}',
        )
        try:
            if not correlations_json.strip():
                correlations_json = '{"XAUUSD|EURUSD":0.75}'
            raw_correlations = json.loads(correlations_json)
            if not isinstance(raw_correlations, dict):
                raise TypeError("correlation matrix must be a JSON object")
            correlations: dict[frozenset[str], Decimal] = {}
            for pair, value in raw_correlations.items():
                if not isinstance(pair, str):
                    raise TypeError("correlation keys must be strings")
                symbols = [symbol.strip().upper() for symbol in pair.split("|")]
                if len(symbols) != 2 or not all(symbols) or symbols[0] == symbols[1]:
                    raise ValueError(
                        "each correlation key must contain two distinct symbols separated by |"
                    )
                correlations[frozenset(symbols)] = Decimal(str(value))
            if any(value < -1 or value > 1 for value in correlations.values()):
                raise ValueError("correlation values must be between -1 and 1")
            limits = cls(
                max_daily_loss=Decimal(os.environ.get("RISK_MAX_DAILY_LOSS", "100")),
                max_daily_drawdown_pct=Decimal(
                    os.environ.get("RISK_MAX_DRAWDOWN_PCT", "5")
                ),
                max_portfolio_heat_pct=Decimal(
                    os.environ.get("RISK_MAX_PORTFOLIO_HEAT_PCT", "2")
                ),
                max_correlated_exposure_pct=Decimal(
                    os.environ.get("RISK_MAX_CORRELATED_EXPOSURE_PCT", "1")
                ),
                max_position_size=Decimal(
                    os.environ.get("RISK_MAX_POSITION_SIZE", "0.01")
                ),
                contract_units=Decimal(
                    os.environ.get("RISK_CONTRACT_UNITS", "100000")
                ),
                signal_ttl_seconds=int(os.environ.get("RISK_ORDER_TTL_SECONDS", "5")),
                max_signal_age_seconds=int(
                    os.environ.get("RISK_MAX_SIGNAL_AGE_SECONDS", "60")
                ),
                account_state_max_age_seconds=int(
                    os.environ.get("RISK_ACCOUNT_STATE_MAX_AGE_SECONDS", "15")
                ),
                news_embargo_minutes=int(
                    os.environ.get("RISK_NEWS_EMBARGO_MINUTES", "15")
                ),
                correlation_threshold=Decimal(
                    os.environ.get("RISK_CORRELATION_THRESHOLD", "0.7")
                ),
                correlation_matrix=correlations,
                min_ai_confidence=Decimal(
                    os.environ.get("RISK_MIN_AI_CONFIDENCE", "0.65")
                ),
            )
            limits.validate()
            return limits
        except (json.JSONDecodeError, InvalidOperation, TypeError) as exc:
            raise ValueError("invalid risk limit configuration") from exc

    def validate(self) -> None:
        if (
            self.max_daily_loss <= 0
            or self.max_daily_drawdown_pct <= 0
            or self.max_daily_drawdown_pct > 100
            or self.max_portfolio_heat_pct <= 0
            or self.max_correlated_exposure_pct <= 0
            or self.max_correlated_exposure_pct > self.max_portfolio_heat_pct
            or self.max_position_size <= 0
            or self.contract_units <= 0
            or self.signal_ttl_seconds != 5
            or self.max_signal_age_seconds <= 0
            or self.account_state_max_age_seconds <= 0
            or self.news_embargo_minutes < 1
            or self.correlation_threshold < 0
            or self.correlation_threshold > 1
            or self.min_ai_confidence < 0
            or self.min_ai_confidence > 1
        ):
            raise ValueError("risk limits are invalid; order TTL must be exactly 5 seconds")


class RiskEngine:
    def __init__(
        self,
        bus: RedisEventBus,
        *,
        calendar: EconomicCalendarClient | None = None,
        limits: RiskLimits | None = None,
        signing_secret: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bus = bus
        self.calendar = calendar or EconomicCalendarClient()
        self.limits = limits or RiskLimits.from_environment()
        self.limits.validate()
        self.signing_secret = signing_secret
        configured_secret = signing_secret or os.environ.get(
            "ORDER_SIGNING_SECRET"
        ) or os.environ.get("SECRET_KEY")
        if configured_secret is None or len(configured_secret.encode("utf-8")) < 32:
            raise ValueError(
                "ORDER_SIGNING_SECRET or SECRET_KEY must contain at least 32 bytes"
            )
        self.signing_secret = configured_secret
        self.clock = clock or (lambda: datetime.now(UTC))
        self.portfolio: PortfolioSnapshot | None = None
        self.pending_signals: dict[str, PatternSetupSignal] = {}
        self.pending_ai_results: dict[str, AIAnalysisResult] = {}
        self.risk_reservations: dict[str, tuple[str, Decimal]] = {}
        self._evaluation_lock = asyncio.Lock()
        self.safe_mode = False
        self.no_new_trades = False
        self.ai_enabled = os.environ.get("FEATURE_AI_SIGNALS", "false").lower() in {
            "1",
            "true",
            "yes",
        }

    async def handle_portfolio(self, snapshot: PortfolioSnapshot) -> None:
        self.portfolio = snapshot
        total_risk = sum(
            (position.risk_amount for position in snapshot.positions), Decimal(0)
        )
        OPEN_RISK_PERCENT.set(
            float(total_risk / snapshot.account.equity * Decimal(100))
            if snapshot.account.equity > 0
            else 0
        )
        reflected_order_ids = {
            str(position.source_order_id)
            for position in snapshot.positions
            if position.source_order_id is not None
        }
        for order_id in reflected_order_ids:
            self.risk_reservations.pop(order_id, None)

    async def handle_execution_status(self, order_id: str, status: str) -> None:
        if status.casefold() in {"rejected", "cancelled", "failed"}:
            self.risk_reservations.pop(order_id, None)

    def handle_safe_mode(self, enabled: bool) -> None:
        self.safe_mode = enabled

    def handle_kill_switch(self, mode: str) -> None:
        self.no_new_trades = mode == "NO_NEW_TRADES"

    async def handle_pattern_signal(self, signal: PatternSetupSignal) -> None:
        self._prune_pending_signals()
        if len(self.pending_signals) >= 10_000:
            await self._reject(
                signal,
                RiskRejectionCode.INVALID_SIGNAL,
                "Risk engine pending-signal capacity is exhausted.",
                signal.trace_id,
            )
            return
        signal_key = str(signal.signal_id)
        pending_ai = self.pending_ai_results.pop(signal_key, None)
        if pending_ai is not None:
            await self._evaluate(signal, ai_result=pending_ai)
            return
        self.pending_signals[str(signal.signal_id)] = signal
        if not self.ai_enabled:
            self.pending_signals.pop(str(signal.signal_id), None)
            await self._evaluate(signal, ai_result=None)

    async def handle_ai_result(self, result: AIAnalysisResult) -> None:
        if not self.ai_enabled:
            logger.info("Ignoring AI analysis because FEATURE_AI_SIGNALS is disabled")
            return
        if result.signal_id is None:
            await self._publish_rejection(
                signal_id=uuid4(),
                symbol="unknown",
                code=RiskRejectionCode.INVALID_SIGNAL,
                reason="AI analysis is missing its originating signal_id.",
                trace_id=result.trace_id,
            )
            return
        signal = self.pending_signals.pop(str(result.signal_id), None)
        if signal is None:
            self._prune_pending_signals()
            self.pending_ai_results[str(result.signal_id)] = result
            return
        await self._evaluate(signal, ai_result=result)

    async def _evaluate(
        self,
        signal: PatternSetupSignal,
        ai_result: AIAnalysisResult | None,
    ) -> None:
        async with self._evaluation_lock:
            await self._evaluate_serialized(signal, ai_result)

    async def _evaluate_serialized(
        self,
        signal: PatternSetupSignal,
        ai_result: AIAnalysisResult | None,
    ) -> None:
        now = self.clock().astimezone(UTC)
        trace_id = signal.trace_id
        try:
            shared_safe_mode = await self.bus.get_state("system:safe_mode")
        except (RedisError, RedisEventBusError):
            self.safe_mode = True
            shared_safe_mode = None
        if shared_safe_mode is None:
            self.safe_mode = True
        else:
            self.safe_mode = bool(shared_safe_mode.get("enabled", True))
        if self.safe_mode:
            await self._reject(
                signal,
                RiskRejectionCode.SAFE_MODE,
                "System SAFE_MODE is enabled; new orders are blocked.",
                trace_id,
            )
            return
        try:
            kill_switch = await self.bus.get_state("system:kill_switch")
        except (RedisError, RedisEventBusError):
            kill_switch = None
            await self._reject(
                signal,
                RiskRejectionCode.SAFE_MODE,
                "Kill switch state is unavailable; new orders are blocked.",
                trace_id,
            )
            return
        kill_mode = (
            str(kill_switch.get("mode", "RUNNING"))
            if kill_switch is not None
            else "RUNNING"
        )
        self.handle_kill_switch(kill_mode)
        if kill_mode != "RUNNING":
            await self._reject(
                signal,
                RiskRejectionCode.SAFE_MODE,
                f"Global kill switch is active ({kill_mode}); new orders are blocked.",
                trace_id,
            )
            return
        rejection = self._validate_signal_age(signal, now)
        if rejection is not None:
            await self._reject(
                signal,
                rejection,
                "Signal is expired, future-dated, or exceeds the configured age.",
                trace_id,
            )
            return
        if (
            signal.side.value == "buy"
            and not (
                signal.stop_loss < signal.entry_price < signal.take_profit
            )
        ) or (
            signal.side.value == "sell"
            and not (
                signal.take_profit < signal.entry_price < signal.stop_loss
            )
        ):
            await self._reject(
                signal,
                RiskRejectionCode.INVALID_RISK_PARAMETERS,
                "Stop loss and take profit are inconsistent with the trade direction.",
                trace_id,
            )
            return

        if self.ai_enabled and ai_result is None:
            await self._reject(
                signal,
                RiskRejectionCode.MISSING_AI_ANALYSIS,
                "AI analysis is required by FEATURE_AI_SIGNALS.",
                trace_id,
            )
            return

        risk_multiplier = Decimal(1)
        if ai_result is not None:
            ai_age_seconds = (
                now - ai_result.created_at.astimezone(UTC)
            ).total_seconds()
            if ai_age_seconds < -1 or ai_age_seconds > self.limits.max_signal_age_seconds:
                await self._reject(
                    signal,
                    RiskRejectionCode.AI_ANALYSIS_STALE,
                    "AI analysis is future-dated or exceeds the configured age.",
                    trace_id,
                )
                return
            if ai_result.decision == AIRecommendation.NO_TRADE:
                await self._reject(
                    signal,
                    RiskRejectionCode.AI_NO_TRADE,
                    "AI analysis decision is NO_TRADE.",
                    trace_id,
                )
                return
            if ai_result.confidence_score < self.limits.min_ai_confidence:
                await self._reject(
                    signal,
                    RiskRejectionCode.AI_CONFIDENCE_TOO_LOW,
                    (
                        "AI confidence "
                        f"{ai_result.confidence_score} is below "
                        f"{self.limits.min_ai_confidence}."
                    ),
                    trace_id,
                )
                return
            expected_decision = (
                AIRecommendation.BUY
                if signal.side.value == "buy"
                else AIRecommendation.SELL
            )
            if ai_result.decision != expected_decision:
                await self._reject(
                    signal,
                    RiskRejectionCode.AI_SIDE_MISMATCH,
                    "AI decision conflicts with the pattern signal direction.",
                    trace_id,
                )
                return
            risk_multiplier = ai_result.risk_multiplier

        snapshot = self.portfolio
        if snapshot is None:
            await self._reject(
                signal,
                RiskRejectionCode.ACCOUNT_STATE_UNAVAILABLE,
                "No account and portfolio risk snapshot is available.",
                trace_id,
            )
            return

        account_age = (
            now - snapshot.account.timestamp.astimezone(UTC)
        ).total_seconds()
        if account_age < -1 or account_age > self.limits.account_state_max_age_seconds:
            await self._reject(
                signal,
                RiskRejectionCode.ACCOUNT_STATE_STALE,
                "Account and portfolio snapshot is stale or future-dated.",
                trace_id,
            )
            return

        start_equity = snapshot.daily_starting_equity
        if start_equity <= 0:
            await self._reject(
                signal,
                RiskRejectionCode.DAILY_EQUITY_BASELINE_UNAVAILABLE,
                "Daily starting equity must be positive.",
                trace_id,
            )
            return

        daily_loss = max(Decimal(0), start_equity - snapshot.account.equity)
        daily_drawdown_pct = daily_loss / start_equity * Decimal(100)
        if (
            daily_loss >= self.limits.max_daily_loss
            or daily_drawdown_pct >= self.limits.max_daily_drawdown_pct
        ):
            await self._reject(
                signal,
                RiskRejectionCode.DAILY_DRAWDOWN_LIMIT,
                "Cumulative daily loss has reached a configured loss or drawdown limit.",
                trace_id,
            )
            return

        try:
            embargo_event = await self.calendar.has_high_impact_event(
                self._symbol_currencies(signal.symbol),
                now,
                embargo_minutes=self.limits.news_embargo_minutes,
            )
        except CalendarUnavailableError:
            await self._reject(
                signal,
                RiskRejectionCode.NEWS_CALENDAR_UNAVAILABLE,
                "Economic calendar is unavailable; risk approval fails closed.",
                trace_id,
            )
            return
        if embargo_event is not None:
            await self._reject(
                signal,
                RiskRejectionCode.NEWS_EMBARGO,
                f"High-impact {embargo_event.currency} event embargo: {embargo_event.title}.",
                trace_id,
            )
            return

        now = self.clock().astimezone(UTC)
        rejection = self._validate_signal_age(signal, now)
        if rejection is not None:
            await self._reject(
                signal,
                rejection,
                "Signal expired or became stale during risk validation.",
                trace_id,
            )
            return
        if ai_result is not None:
            ai_age_seconds = (
                now - ai_result.created_at.astimezone(UTC)
            ).total_seconds()
            if ai_age_seconds < -1 or ai_age_seconds > self.limits.max_signal_age_seconds:
                await self._reject(
                    signal,
                    RiskRejectionCode.AI_ANALYSIS_STALE,
                    "AI analysis expired or became stale during risk validation.",
                    trace_id,
                )
                return

        if risk_multiplier <= 0:
            await self._reject(
                signal,
                RiskRejectionCode.INVALID_RISK_PARAMETERS,
                "AI risk multiplier must be greater than zero for an order.",
                trace_id,
            )
            return

        snapshot = self.portfolio
        if snapshot is None:
            await self._reject(
                signal,
                RiskRejectionCode.ACCOUNT_STATE_UNAVAILABLE,
                "No account and portfolio risk snapshot is available.",
                trace_id,
            )
            return
        account_age = (
            now - snapshot.account.timestamp.astimezone(UTC)
        ).total_seconds()
        if account_age < -1 or account_age > self.limits.account_state_max_age_seconds:
            await self._reject(
                signal,
                RiskRejectionCode.ACCOUNT_STATE_STALE,
                "Account and portfolio snapshot became stale during risk evaluation.",
                trace_id,
            )
            return
        current_daily_loss = max(
            Decimal(0),
            snapshot.daily_starting_equity - snapshot.account.equity,
        )
        if (
            current_daily_loss >= self.limits.max_daily_loss
            or current_daily_loss
            / snapshot.daily_starting_equity
            * Decimal(100)
            >= self.limits.max_daily_drawdown_pct
        ):
            await self._reject(
                signal,
                RiskRejectionCode.DAILY_DRAWDOWN_LIMIT,
                "Cumulative daily loss has reached a configured loss or drawdown limit.",
                trace_id,
            )
            return

        volume = self.limits.max_position_size * risk_multiplier
        proposed_risk = (
            abs(signal.entry_price - signal.stop_loss)
            * volume
            * self.limits.contract_units
        )
        current_risk = sum(
            (position.risk_amount for position in snapshot.positions),
            Decimal(0),
        )
        reflected_order_ids = {
            str(position.source_order_id)
            for position in snapshot.positions
            if position.source_order_id is not None
        }
        current_risk += sum(
            (
                risk_amount
                for order_id, (_, risk_amount) in self.risk_reservations.items()
                if order_id not in reflected_order_ids
            ),
            Decimal(0),
        )
        total_heat = current_risk + proposed_risk
        heat_limit = (
            snapshot.account.equity
            * self.limits.max_portfolio_heat_pct
            / Decimal(100)
        )
        if total_heat > heat_limit:
            await self._reject(
                signal,
                RiskRejectionCode.PORTFOLIO_HEAT_LIMIT,
                "Current plus proposed open-trade risk exceeds the portfolio heat limit.",
                trace_id,
            )
            return

        correlated_risk = proposed_risk
        for position in snapshot.positions:
            correlation = self._correlation(signal.symbol, position.symbol)
            if abs(correlation) >= self.limits.correlation_threshold:
                correlated_risk += position.risk_amount * abs(correlation)
        for order_id, (reserved_symbol, reserved_risk) in self.risk_reservations.items():
            if order_id not in reflected_order_ids:
                correlation = self._correlation(signal.symbol, reserved_symbol)
                if abs(correlation) >= self.limits.correlation_threshold:
                    correlated_risk += reserved_risk * abs(correlation)
        correlated_limit = (
            snapshot.account.equity
            * self.limits.max_correlated_exposure_pct
            / Decimal(100)
        )
        if correlated_risk > correlated_limit:
            await self._reject(
                signal,
                RiskRejectionCode.CORRELATED_EXPOSURE_LIMIT,
                "Correlated open and proposed exposure exceeds the configured threshold.",
                trace_id,
            )
            return

        issued_at = now
        expires_at = issued_at + timedelta(seconds=self.limits.signal_ttl_seconds)
        payload = SignedOrderPayload(
            intent_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            order_type=OrderType.MARKET,
            volume=volume,
            price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            issued_at=issued_at,
            expires_at=expires_at,
            trace_id=trace_id or uuid4().hex,
        )
        signed_payload = sign_payload(payload, self.signing_secret)
        await self.bus.publish(
            ORDERS_CHANNEL,
            signed_payload,
            trace_id=signed_payload.trace_id,
            event_type="SignedOrderPayload",
        )
        self.risk_reservations[str(signed_payload.order_id)] = (
            signal.symbol,
            proposed_risk,
        )
        approval = RiskApproval(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            proposed_risk_amount=proposed_risk,
            portfolio_heat_after=total_heat,
            correlated_risk_after=correlated_risk,
            timestamp=issued_at,
        )
        logger.info("RISK_APPROVAL %s", approval.model_dump_json())

    def _validate_signal_age(
        self,
        signal: PatternSetupSignal,
        now: datetime,
    ) -> RiskRejectionCode | None:
        created_at = signal.created_at.astimezone(UTC)
        expires_at = signal.expires_at.astimezone(UTC)
        if now >= expires_at:
            return RiskRejectionCode.SIGNAL_EXPIRED
        age_seconds = (now - created_at).total_seconds()
        if age_seconds < -1 or age_seconds > self.limits.max_signal_age_seconds:
            return RiskRejectionCode.SIGNAL_TOO_OLD
        return None

    def _prune_pending_signals(self) -> None:
        now = self.clock().astimezone(UTC)
        expired_ids = [
            signal_id
            for signal_id, signal in self.pending_signals.items()
            if signal.expires_at.astimezone(UTC) <= now
        ]
        for signal_id in expired_ids:
            del self.pending_signals[signal_id]
        expired_ai_ids = [
            signal_id
            for signal_id, result in self.pending_ai_results.items()
            if (now - result.created_at.astimezone(UTC)).total_seconds()
            > self.limits.max_signal_age_seconds
        ]
        for signal_id in expired_ai_ids:
            del self.pending_ai_results[signal_id]

    @staticmethod
    def _symbol_currencies(symbol: str) -> set[str]:
        normalized = symbol.upper().replace("/", "").replace("_", "")
        if normalized.startswith("XAU"):
            return {"XAU", normalized[3:6]} if len(normalized) >= 6 else {"XAU", "USD"}
        if len(normalized) >= 6:
            return {normalized[:3], normalized[3:6]}
        return {normalized}

    def _correlation(self, symbol_a: str, symbol_b: str) -> Decimal:
        if symbol_a.upper() == symbol_b.upper():
            return Decimal(1)
        return self.limits.correlation_matrix.get(
            frozenset({symbol_a.upper(), symbol_b.upper()}),
            Decimal(0),
        )

    async def _reject(
        self,
        signal: PatternSetupSignal,
        code: RiskRejectionCode,
        reason: str,
        trace_id: str | None,
    ) -> None:
        await self._publish_rejection(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            code=code,
            reason=reason,
            trace_id=trace_id,
        )

    async def _publish_rejection(
        self,
        *,
        signal_id: UUID,
        symbol: str,
        code: RiskRejectionCode,
        reason: str,
        trace_id: str | None,
    ) -> None:
        rejection = RiskRejection(
            signal_id=signal_id,
            symbol=symbol,
            code=code,
            reason=reason,
            occurred_at=self.clock().astimezone(UTC),
            trace_id=trace_id,
        )
        logger.warning("RISK_REJECTION %s", rejection.model_dump_json())
        results = await asyncio.gather(
            self.bus.publish(
                REJECTIONS_CHANNEL,
                rejection,
                trace_id=trace_id or uuid4().hex,
                event_type="RiskRejection",
            ),
            self.bus.publish(
                ALERTS_CHANNEL,
                rejection,
                trace_id=trace_id or uuid4().hex,
                event_type="RiskAlert",
            ),
            return_exceptions=True,
        )
        if any(isinstance(result, asyncio.CancelledError) for result in results):
            raise asyncio.CancelledError
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("risk rejection publication failed", failures)
