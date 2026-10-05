import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from schemas.messages import BarData, PatternSetupSignal, TradeSide

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PatternConfig:
    base_timeframe: str = "M5"
    confirmation_timeframes: tuple[str, ...] = ("M15", "H1")
    history_size: int = 128
    structure_lookback: int = 20
    atr_period: int = 14
    atr_baseline_period: int = 50
    atr_expansion_ratio: Decimal = Decimal("1.2")
    max_spread_ratio: Decimal = Decimal("2.5")
    stop_atr_multiplier: Decimal = Decimal("1.5")
    reward_risk_ratio: Decimal = Decimal("2.0")
    signal_validity_minutes: int = 30
    rollover_start: time = time(16, 55)
    rollover_end: time = time(17, 15)

    @classmethod
    def from_environment(cls) -> "PatternConfig":
        return cls(
            base_timeframe=os.environ.get("PATTERN_BASE_TIMEFRAME", "M5"),
            confirmation_timeframes=tuple(
                item.strip()
                for item in os.environ.get(
                    "PATTERN_CONFIRMATION_TIMEFRAMES",
                    "M15,H1",
                ).split(",")
                if item.strip()
            ),
            history_size=int(os.environ.get("PATTERN_HISTORY_SIZE", "128")),
            structure_lookback=int(os.environ.get("PATTERN_STRUCTURE_LOOKBACK", "20")),
            atr_period=int(os.environ.get("PATTERN_ATR_PERIOD", "14")),
            atr_baseline_period=int(
                os.environ.get("PATTERN_ATR_BASELINE_PERIOD", "50")
            ),
            atr_expansion_ratio=Decimal(
                os.environ.get("PATTERN_ATR_EXPANSION_RATIO", "1.2")
            ),
            max_spread_ratio=Decimal(
                os.environ.get("PATTERN_MAX_SPREAD_RATIO", "2.5")
            ),
            stop_atr_multiplier=Decimal(
                os.environ.get("PATTERN_STOP_ATR_MULTIPLIER", "1.5")
            ),
            reward_risk_ratio=Decimal(
                os.environ.get("PATTERN_REWARD_RISK_RATIO", "2.0")
            ),
            signal_validity_minutes=int(
                os.environ.get("PATTERN_SIGNAL_VALIDITY_MINUTES", "30")
            ),
        )

    def __post_init__(self) -> None:
        if self.history_size < max(
            self.structure_lookback + 1,
            self.atr_period + self.atr_baseline_period + 1,
        ):
            raise ValueError("history_size is too small for configured lookbacks")
        if (
            self.structure_lookback < 2
            or self.atr_period < 1
            or self.atr_baseline_period < 1
            or self.signal_validity_minutes < 1
        ):
            raise ValueError("pattern lookbacks must be positive")
        if self.atr_expansion_ratio <= 0 or self.max_spread_ratio <= 1:
            raise ValueError("volatility/spread ratios must be positive")
        if self.stop_atr_multiplier <= 0 or self.reward_risk_ratio <= 0:
            raise ValueError("stop and reward/risk multipliers must be positive")
        if not self.confirmation_timeframes:
            raise ValueError("at least one confirmation timeframe is required")
        if self.rollover_start >= self.rollover_end:
            raise ValueError("rollover window must not cross midnight")


class PatternEngine:
    def __init__(self, config: PatternConfig | None = None) -> None:
        self.config = config or PatternConfig.from_environment()
        self._bars: dict[tuple[str, str], deque[BarData]] = defaultdict(
            lambda: deque(maxlen=self.config.history_size)
        )
        self._last_processed_base_bar: dict[str, datetime] = {}
        self._latest_spread_ratio: dict[str, Decimal] = {}

    def record_spread_ratio(self, symbol: str, ratio: Decimal) -> None:
        self._latest_spread_ratio[symbol] = ratio

    def on_bar(self, bar: BarData) -> PatternSetupSignal | None:
        history = self._bars[(bar.symbol, bar.timeframe)]
        if history and bar.timestamp <= history[-1].timestamp:
            if bar.timestamp == history[-1].timestamp:
                history[-1] = bar
            return None

        history.append(bar)
        if bar.timeframe != self.config.base_timeframe:
            return None
        if self._last_processed_base_bar.get(bar.symbol) == bar.timestamp:
            return None
        self._last_processed_base_bar[bar.symbol] = bar.timestamp

        if self.is_rollover(bar.timestamp):
            return None

        bars = list(history)
        minimum_history = max(
            self.config.structure_lookback + 2,
            self.config.atr_period + self.config.atr_baseline_period + 1,
        )
        if len(bars) < minimum_history:
            return None

        spread_ratio = self._spread_ratio(bar)
        if spread_ratio is None or spread_ratio > self.config.max_spread_ratio:
            return None

        direction, evidence = self._base_evidence(bars)
        if direction is None:
            return None
        confirmation = self._higher_timeframe_confirmation(bar.symbol)
        if confirmation != direction:
            return None

        atr, baseline_atr = self._atr_values(bars)
        if atr <= 0 or baseline_atr <= 0:
            return None
        volatility_ratio = atr / baseline_atr
        if volatility_ratio < self.config.atr_expansion_ratio:
            return None

        side = TradeSide.BUY if direction > 0 else TradeSide.SELL
        stop_distance = atr * self.config.stop_atr_multiplier
        entry = bar.close
        stop = entry - stop_distance if direction > 0 else entry + stop_distance
        target_distance = stop_distance * self.config.reward_risk_ratio
        target = entry + target_distance if direction > 0 else entry - target_distance
        if min(entry, stop, target) <= 0:
            return None

        confidence = min(
            Decimal(1),
            Decimal("0.5")
            + min(volatility_ratio - Decimal(1), Decimal(1)) * Decimal("0.2")
            + Decimal("0.1") * len(evidence),
        )
        created_at = datetime.now(UTC)
        return PatternSetupSignal(
            strategy_id="multi-timeframe-pattern-v1",
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            side=side,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            confidence=confidence,
            created_at=created_at,
            expires_at=created_at
            + timedelta(minutes=self.config.signal_validity_minutes),
            attributes={
                "evidence": evidence,
                "confirmation_timeframes": list(self.config.confirmation_timeframes),
                "atr": str(atr),
                "atr_baseline": str(baseline_atr),
                "volatility_ratio": str(volatility_ratio),
                "spread_ratio": str(spread_ratio) if spread_ratio is not None else None,
            },
        )

    def is_rollover(self, timestamp: datetime) -> bool:
        local_time = timestamp.astimezone(NEW_YORK).time().replace(tzinfo=None)
        return self.config.rollover_start <= local_time < self.config.rollover_end

    def _spread_ratio(self, bar: BarData) -> Decimal | None:
        rolling_ratio = self._latest_spread_ratio.get(bar.symbol)
        if rolling_ratio is not None:
            return rolling_ratio

        history = list(self._bars[(bar.symbol, bar.timeframe)])
        if bar.spread is None:
            return None
        spreads = [
            item.spread
            for item in history[:-1]
            if item.spread is not None
        ][-20:]
        if len(spreads) < 20:
            return None
        average = sum(spreads, 0) / len(spreads)
        if average <= 0:
            return None
        return Decimal(bar.spread or 0) / Decimal(average)

    def _base_evidence(self, bars: list[BarData]) -> tuple[int | None, list[str]]:
        previous, current = bars[-2], bars[-1]
        evidence: list[str] = []
        direction: int | None = None

        previous_bullish = previous.close > previous.open
        previous_bearish = previous.close < previous.open
        current_bullish = current.close > current.open
        current_bearish = current.close < current.open

        if (
            previous_bearish
            and current_bullish
            and current.open <= previous.close
            and current.close >= previous.open
        ):
            direction = 1
            evidence.append("bullish_engulfing")
        elif (
            previous_bullish
            and current_bearish
            and current.open >= previous.close
            and current.close <= previous.open
        ):
            direction = -1
            evidence.append("bearish_engulfing")

        prior_structure = bars[-(self.config.structure_lookback + 1) : -1]
        prior_high = max(item.high for item in prior_structure)
        prior_low = min(item.low for item in prior_structure)
        if current.close > prior_high:
            if direction not in (None, 1):
                return None, []
            direction = 1
            evidence.append("bullish_structure_break")
        elif current.close < prior_low:
            if direction not in (None, -1):
                return None, []
            direction = -1
            evidence.append("bearish_structure_break")

        return direction, evidence

    def _higher_timeframe_confirmation(self, symbol: str) -> int | None:
        directions: list[int] = []
        for timeframe in self.config.confirmation_timeframes:
            history = self._bars[(symbol, timeframe)]
            if not history:
                return None
            latest = history[-1]
            if latest.close > latest.open:
                directions.append(1)
            elif latest.close < latest.open:
                directions.append(-1)
            else:
                return None
        if directions and all(direction == directions[0] for direction in directions):
            return directions[0]
        return None

    def _atr_values(self, bars: list[BarData]) -> tuple[Decimal, Decimal]:
        ranges: list[Decimal] = []
        previous_close: Decimal | None = None
        for bar in bars:
            true_range = bar.high - bar.low
            if previous_close is not None:
                true_range = max(
                    true_range,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            ranges.append(true_range)
            previous_close = bar.close

        atr = sum(ranges[-self.config.atr_period :], Decimal(0)) / Decimal(
            self.config.atr_period
        )
        baseline_start = -(
            self.config.atr_period + self.config.atr_baseline_period
        )
        baseline_ranges = ranges[baseline_start : -self.config.atr_period]
        baseline_atr = sum(baseline_ranges, Decimal(0)) / Decimal(
            len(baseline_ranges)
        )
        return atr, baseline_atr
