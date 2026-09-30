# Market Data Consumer

The service consumes normalized market events from Redis channels
`market.ticks` and `market.bars`, and optionally accepts direct MT5 ingress via
ZeroMQ PULL sockets. The development and production Compose configurations
bind the tick socket to `MT5_ZMQ_PUB_PORT` (default 5555) and the bar socket to
`MT5_ZMQ_SUB_PORT` (default 5556), on loopback only. An EA or adapter running
on the Windows host should connect with PUSH sockets to `tcp://127.0.0.1:5555`
for ticks and `tcp://127.0.0.1:5556` for bars.

Each ZMQ frame is one UTF-8 JSON object using the fields of `TickData` or
`BarData`. An optional `trace_id` field is stripped before schema validation.
Each tick gets a UUID `tick_id` when one is not supplied, so multiple quote
updates sharing a timestamp are preserved. Ticks and bars are stored
idempotently in TimescaleDB hypertables
`market_ticks` and `market_bars`; normalized ZMQ ingress is also published to
the matching Redis channel. Tick processing emits 20-sample spread MA and
spread ratio measurements on `market.indicators`.

EMA, ATR, and RSI are updated from incoming bar closes/OHLC; the spread moving
average is updated on every tick. Indicator state is in-memory and starts
warming from service startup; the raw time series remains persisted in the
hypertables for later backfill or analysis. Bar indicator snapshots are
published on `market.technicals`; tick spread metrics are published on
`market.indicators`.
