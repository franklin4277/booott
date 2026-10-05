# MT5 Automated Trading System

Windows 11 / Windows Server VPS-friendly monorepo scaffold for an MT5-connected,
local-process trading platform.

## Repository layout

```text
.
├── mt5/
│   ├── Experts/
│   ├── Include/
│   └── README.md
├── scripts/
│   ├── mt5_host_adapter.py
│   ├── start-all.ps1
│   ├── stop-all.ps1
│   ├── run-tests.ps1
│   └── health-check.ps1
├── schemas/
├── event_bus/
├── utils/
├── services/
│   ├── api_gateway/app/
│   ├── risk_engine/app/
│   ├── market_engine/app/
│   ├── market_data/app/
│   ├── pattern_engine/app/
│   ├── ai_engine/app/
│   ├── reconciliation/app/
│   ├── execution_engine/app/
│   ├── telegram_bot/app/
│   └── trade_monitor/app/
├── database/
├── migrations/
├── .env.example
└── requirements.txt
```

Every Python service exposes `/health` and `/metrics`. `market-data` implements
tick/bar ingress, SQLite persistence, and rolling indicators;
`pattern-engine` implements a configurable multi-timeframe candidate strategy.
The Windows MT5 host adapter publishes live ticks and closed-bar history,
serves broker state for reconciliation, and accepts signed order approvals.
Execution remains fail-closed by default; validate broker execution and
strategy suitability on a demo account before any live use.

`market-data` accepts JSON tick/bar messages through Redis or ZeroMQ PULL
sockets, persists them to SQLite, and computes rolling indicators.
`pattern-engine` consumes normalized bars and spread metrics and publishes
qualified setups to `signals.pattern`. See `services/market_data/README.md` for
the input message shape and port mapping.

`ai-engine` analyzes pattern signals through Instructor with OpenAI or
Anthropic, applies a token bucket and circuit breaker, and publishes results
to `signals.ai` and health state to `ai.state`. AI failure is fail-closed to
`NO_TRADE` unless the quantitative override is enabled in deployment config
and explicitly requested with a complete deterministic override in the input.

`risk-engine` consumes `signals.pattern`, `signals.ai`, and `risk.portfolio`.
Publish a current `PortfolioSnapshot` (account equity, daily starting equity,
and each open position's risk amount) to `risk.portfolio`. Approved orders
are signed and published to `orders.approved`; rejections publish structured
events to `risk.rejections` and `system.alerts`. Configure a trusted HTTPS
MT5 calendar bridge by compiling and attaching
`mt5/Experts/EconomicCalendarBridge.mq5` in the logged-in terminal. The EA
publishes MT5 built-in high-impact calendar events to the shared Common Files
folder; until a fresh snapshot is available, the Risk Engine rejects order
approvals.

`reconciliation` records bar/signal/AI/order/execution event lineage, and
reconciles open positions, pending orders, closures, and account equity against
a Windows-host MT5 adapter. An unknown broker position or unavailable broker-
state adapter places the system in persistent `SAFE_MODE`; unknown positions
trigger configured PagerDuty/Telegram alerts. Reconciliation is also available
through an authenticated manual API request.

`telegram-bot` consumes execution, risk-rejection, SAFE_MODE, kill-switch,
AI-state, and system-alert events for out-of-band Telegram notification. Its
private-chat controls are restricted to numeric IDs in
`TELEGRAM_ALLOWED_USER_IDS`: `/status`, `/kill NO_NEW_TRADES`,
`/kill SAFE_MODE`, `/kill EMERGENCY_FLAT`, and `/resume`. The bot persists
`NO_NEW_TRADES` in Redis; `/resume` releases that latch, or clears the
SAFE_MODE/EMERGENCY_FLAT mirror only after the reconciliation service confirms
SAFE_MODE is already clear. SAFE_MODE and EMERGENCY_FLAT require a successful
reconciliation and the existing authenticated manual SAFE_MODE clear
procedure before trading can resume. EMERGENCY_FLAT first persists SAFE_MODE, then calls the
authenticated Windows MT5 host adapter to close positions and cancel pending
orders; an incomplete broker response is reported as critical and leaves
SAFE_MODE latched. Configure `TELEGRAM_BOT_TOKEN`, authorized user IDs, and
notification chat IDs in the untracked `.env.local`. Without a token, monitoring
remains available but Telegram control/notification delivery is disabled.

`api-gateway` is the authenticated reverse-proxy entry point. It proxies
`POST /v1/ingest/tick` and `POST /v1/ingest/bar` to `market-data`,
proxies reconciliation admin routes to `reconciliation`, and aggregates
`/health` from all downstream services. Require `X-Api-Key` matching
`API_GATEWAY_KEY` for every proxied request; the gateway forwards
`MT5_ADAPTER_TOKEN` for market-ingest calls so the host adapter can publish
without exposing the bridge token to external clients.

`market-engine` is the market-data lifecycle and backfill orchestrator.
On startup it checks `MT5_ADAPTER_URL/v1/state` for adapter health, then
periodically backfills historical bars into `market-data` to warm indicators
after restarts. It publishes `market.engine.status` events and exposes
`POST /v1/backfill` for on-demand re-population of a single symbol/timeframe.

`trade-monitor` is the open-trade surveillance service. It consumes
`execution.reports`, maintains an in-memory open-trade ledger, polls
`MT5_ADAPTER_URL/v1/account` for equity snapshots, and publishes
`trade.status` alerts to `system.alerts` for:
- Unrealized drawdown exceeding `TELEGRAM_DRAWDOWN_WARNING_PCT`
- Trade age exceeding `TRADE_MAX_AGE_HOURS` (requires two consecutive checks)
- Sudden adverse move exceeding `TRADE_ADVERSE_MOVE_PCT` (requires two consecutive checks)
It exposes `GET /v1/open-trades` for dashboard queries.

Shared Pydantic contracts live in `schemas/`, Redis Pub/Sub transport in
`event_bus/`, and order HMAC signing in `utils/security.py`. Redis Pub/Sub is
best-effort and does not retain messages for disconnected subscribers; use
event IDs for consumer deduplication and Redis Streams/a durable broker if
delivery guarantees are required.

## Development on Windows

1. Copy `.env.example` to `.env.local` (gitignored) and replace all development
   credentials and API-key placeholders.
2. On the Windows host with the MT5 terminal logged in, install the host bridge
   dependencies with `py -m pip install -r requirements-mt5-host.txt`, set
   `MT5_ADAPTER_TOKEN` to the same secret as `.env.local`, and start
   `.\scripts\run_mt5_host_adapter.ps1`. Restrict inbound TCP 8765 in Windows
   Firewall to localhost only; keep the bridge token private.
3. Set the Telegram bot token and authorized numeric user IDs in `.env.local`
   before relying on Telegram control. Bot commands are accepted in private chats
   only; configure notification destinations separately if desired.
4. Install local-mode extras (SQLite + fakeredis):
   ```powershell
   py -m pip install -r requirements.txt[local]
   ```
5. Start all services:
   ```powershell
   .\scripts\start-all.ps1
   ```
6. Check `http://localhost:8000/health` (api-gateway) and
   `http://localhost:8020/health` (market-data ingest).

For a browser-based local monitoring view, open
`http://127.0.0.1:8000/dashboard`. The dashboard auto-refreshes service health,
broker account balance, equity, margin, live MT5 positions, and tracked
executions every 10 seconds. Account and position details require the MT5 host
adapter to be connected and are restricted to connections from the local
machine.

Stop all services with:

```powershell
.\scripts\stop-all.ps1
```

## Development without Docker

All ten services run as local `uvicorn` processes with **embedded SQLite and
fakeredis** so no external database or Docker Desktop is required.

### Prerequisites

1. Python 3.12 with `py` launcher available on `PATH`.
2. Copy `.env.example` to `.env.local` (gitignored):
   ```powershell
   Copy-Item .env.example .env.local
   ```
   At minimum, set `API_GATEWAY_KEY`, `MT5_ADAPTER_TOKEN`, and `ORDER_SIGNING_SECRET`
   to distinct random 32-byte values.
3. Install local-mode extras (SQLite + fakeredis):
   ```powershell
   py -m pip install -r requirements.txt[local]
   ```

### Start all services

```powershell
.\scripts\start-all.ps1
```

`start-all.ps1` does the following:

- Loads `.env.local` if it exists, otherwise `.env`.
- Creates the `.venv` if missing and installs `requirements.txt`.
- Creates the `logs/` directory and writes one `<service>.pid` file per process.
- Initializes the SQLite schema automatically when `DATABASE_URL` starts with
  `sqlite`.
- Uses **fakeredis** as the Redis client — no `redis-server` process is needed.
- Starts all ten services on the ports shown in the table below.
- Optionally starts `scripts/mt5_host_adapter.py` when
  `MT5_MARKET_DATA_ENABLED=true` (omit with `-SkipMT5`).

| Service              | Default port |
|----------------------|-------------|
| api-gateway          | 8000        |
| risk-engine          | 8001        |
| pattern-engine       | 8002        |
| ai-engine            | 8003        |
| execution-engine     | 8004        |
| telegram-bot         | 8005        |
| market-engine        | 8006        |
| trade-monitor        | 8007        |
| market-data (ingest) | 8020        |
| reconciliation       | 8010        |
| mt5-host-adapter     | 8765        |

Every port is overridable via the corresponding environment variable
(e.g. `API_PORT=8010`, `MARKET_DATA_INGEST_PORT=9090`).

### Stop all services

```powershell
.\scripts\stop-all.ps1
```

This reads PID files from `logs/` and falls back to killing all `uvicorn` and
`python` processes if the files are missing.

### SQLite notes

- `DATABASE_URL=sqlite+aiosqlite:///./trading_local.db` uses SQLite as the
  backend. The `market-data` service skips the TimescaleDB `create_hypertable`
  call automatically when the dialect is SQLite.
- Alembic migrations are PostgreSQL-specific and are **not run** in SQLite mode;
  `metadata.create_all()` creates the required tables instead.
- TimescaleDB compression and continuous aggregates are unavailable in SQLite
  mode; rolling indicators still function for the current session.

### fakeredis notes

- `REDIS_URL=redis://localhost:6379/0` works unchanged; the `event_bus` module
  detects fakeredis and uses a `FakeRedis` client when the host is `localhost`.
- All Redis Pub/Sub, `SET`/`GET`, and key operations used by the services are
  supported by fakeredis.

### MT5 host adapter

When `MT5_MARKET_DATA_ENABLED=true` and the Windows MT5 terminal is logged in,
`start-all.ps1` also spawns `scripts/mt5_host_adapter.py` on
`MT5_ADAPTER_PORT` (default 8765). Set `MT5_MARKET_DATA_ENABLED=false` or pass
`-SkipMT5` to skip the adapter.

```powershell
# skip adapter launch
.\scripts\start-all.ps1 -SkipMT5
```

## Continuous Integration

Run the local test runner from the repository root:

```powershell
.\scripts\run-tests.ps1 -All
```

This creates a `.venv`, installs dependencies, runs `ruff` lint, and executes
the unit-test suite with an 80 % coverage threshold over
`services/`, `schemas/`, `event_bus/`, `utils/`, and `database/`.

## End-to-end execution safety

The normal path is MT5 market data → market/pattern services → optional AI →
Risk Engine → `orders.approved` → execution engine → Windows MT5 adapter →
`execution.reports` → ledger/reconciliation. Reconciliation publishes the
account's daily-equity baseline and open-position risk to `risk.portfolio`;
missing or unknown position risk is treated conservatively. Pattern signals
are based only on the most recently closed candle, and approved orders expire
after five seconds. Configure a working economic-calendar API before expecting
the Risk Engine to approve orders.

The Windows adapter polls the configured `MT5_MARKET_SYMBOLS` and
`MT5_MARKET_TIMEFRAMES`, then sends ticks and closed bars to the authenticated
market-data ingest endpoint. `MT5_MARKET_DATA_ENABLED` controls publishing;
closed-bar history is loaded when the adapter starts and newer bars are sent
incrementally. Ticks older than `MT5_MARKET_MAX_TICK_AGE_SECONDS` (30 seconds
by default) are discarded by both the adapter and market-data ingress, so a
weekend's last quote is not treated as live data. Historical closed bars remain
available for indicator warm-up; the pattern engine rejects signals from stale
bars. Confirm broker-specific symbol names and keep the adapter and terminal
running on the Windows host.

Paper trading is enabled by default and does not contact the broker. Keep
`FEATURE_PAPER_TRADING=true` and `MT5_LIVE_TRADING_ENABLED=false` until the
data, reconciliation, calendar, kill-switch, and alerting path has been
verified on a demo account. Broker routing requires explicitly disabling paper
trading and enabling live trading in the Windows host. The project does not
certify any strategy or guarantee profitability. Redis Pub/Sub is best-effort;
use a durable broker before relying on it for production order delivery.

## Notes

- SQLite is the default backend. For PostgreSQL, set `DATABASE_URL` to a
  `postgresql+asyncpg://` connection string and install `asyncpg` and `alembic`
  manually.
- `services/common` provides shared liveness and Prometheus metrics endpoints.
- The market-data ZMQ ports remain available for compatible MT5 integrations;
  the Windows host adapter uses authenticated HTTP ingress by default.
