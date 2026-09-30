# MetaTrader 5 files

Place Expert Advisors in `Experts/` and shared `.mqh` includes in `Include/`.
`Experts/IndependentEquityGuard.mq5` is the independent emergency equity guard.
Copy its sample configuration from `Files/` into the terminal's `MQL5/Files/`
directory before attaching the EA.

The Market Data Consumer's ZMQ PULL ports are configured by
`MT5_ZMQ_PUB_PORT` (ticks) and `MT5_ZMQ_SUB_PORT` (bars) in the root `.env`
file. An EA/adapter should connect with ZMQ PUSH sockets to these localhost
ports. The development and production Compose files bind these ports to
loopback on the host; do not expose them remotely without authentication and
network controls.
