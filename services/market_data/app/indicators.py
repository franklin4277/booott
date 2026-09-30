from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal

from schemas.messages import BarData, TickData


@dataclass(frozen=True)
class IndicatorSnapshot:
    ema: Decimal | None
    atr: Decimal | None
    rsi: Decimal | None
    spread_ma: Decimal | None
    spread_ratio: Decimal | None
    spread_sample_count: int


@dataclass
class IndicatorState:
    ema_period: int = 20
    atr_period: int = 14
    rsi_period: int = 14
    spread_period: int = 20
    _ema: Decimal | None = field(default=None, init=False)
    _previous_close: Decimal | None = field(default=None, init=False)
    _true_ranges: deque[Decimal] = field(default_factory=deque, init=False)
    _gains: deque[Decimal] = field(default_factory=deque, init=False)
    _losses: deque[Decimal] = field(default_factory=deque, init=False)
    _spreads: deque[Decimal] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        if min(self.ema_period, self.atr_period, self.rsi_period, self.spread_period) < 1:
            raise ValueError("indicator periods must be positive")
        self._true_ranges = deque(maxlen=self.atr_period)
        self._gains = deque(maxlen=self.rsi_period)
        self._losses = deque(maxlen=self.rsi_period)
        self._spreads = deque(maxlen=self.spread_period)

    def update_tick(self, tick: TickData) -> IndicatorSnapshot:
        current_spread = tick.ask - tick.bid
        reference_average = (
            sum(self._spreads, Decimal(0)) / len(self._spreads)
            if len(self._spreads) == self.spread_period
            else None
        )
        self._spreads.append(current_spread)
        snapshot = self.snapshot()
        if reference_average is not None and reference_average > 0:
            snapshot = replace(
                snapshot,
                spread_ratio=current_spread / reference_average,
            )
        return snapshot

    def update_bar(self, bar: BarData) -> IndicatorSnapshot:
        close = bar.close
        if self._ema is None:
            self._ema = close
        else:
            multiplier = Decimal(2) / Decimal(self.ema_period + 1)
            self._ema = (close - self._ema) * multiplier + self._ema

        if self._previous_close is not None:
            change = close - self._previous_close
            self._gains.append(max(change, Decimal(0)))
            self._losses.append(max(-change, Decimal(0)))

        true_range = bar.high - bar.low
        if self._previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - self._previous_close),
                abs(bar.low - self._previous_close),
            )
        self._true_ranges.append(true_range)
        self._previous_close = close

        if bar.spread is not None:
            self._spreads.append(Decimal(bar.spread))

        return self.snapshot()

    def snapshot(self) -> IndicatorSnapshot:
        atr = (
            sum(self._true_ranges, Decimal(0)) / len(self._true_ranges)
            if self._true_ranges
            else None
        )

        rsi = None
        if self._gains:
            average_gain = sum(self._gains, Decimal(0)) / len(self._gains)
            average_loss = sum(self._losses, Decimal(0)) / len(self._losses)
            if average_loss == 0:
                rsi = Decimal(100) if average_gain > 0 else Decimal(50)
            else:
                relative_strength = average_gain / average_loss
                rsi = Decimal(100) - (
                    Decimal(100) / (Decimal(1) + relative_strength)
                )

        spread_ma = (
            sum(self._spreads, Decimal(0)) / len(self._spreads)
            if self._spreads
            else None
        )
        latest_spread = self._spreads[-1] if self._spreads else None
        spread_ratio = (
            latest_spread / spread_ma
            if (
                latest_spread is not None
                and spread_ma is not None
                and spread_ma > 0
                and len(self._spreads) == self.spread_period
            )
            else None
        )
        return IndicatorSnapshot(
            ema=self._ema,
            atr=atr,
            rsi=rsi,
            spread_ma=spread_ma,
            spread_ratio=spread_ratio,
            spread_sample_count=len(self._spreads),
        )
