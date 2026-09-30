# Algorithmic Pattern Engine

The engine consumes `BarData` events from `market.bars` and rolling tick-spread
metrics from `market.indicators`. It publishes validated `PatternSetupSignal`
events to `signals.pattern`.

A candidate is eligible when:

- the configured base-timeframe bar has a bullish/bearish engulfing pattern or
  a close beyond the prior structure lookback high/low;
- all configured confirmation timeframes have a latest candle in the same
  direction;
- base-timeframe ATR exceeds its preceding ATR baseline by the configured
  expansion ratio (default `1.2`);
- a warmed 20-sample spread ratio is available and does not exceed `2.5`; and
- the bar is outside the New York local-time rollover interval, 16:55 inclusive
  to 17:15 exclusive. The IANA timezone applies the appropriate EST/EDT offset.

The defaults are configurable with `PATTERN_*` variables in `.env.example`.
The service keeps rolling candle history in memory and does not preload it from
TimescaleDB, so it must warm up after startup before it can emit signals. These
signals are evidence candidates, not execution approvals or financial advice.
