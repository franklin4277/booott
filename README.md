# MT5 Automated Trading System

Windows 11 / Windows Server VPS-friendly monorepo scaffold for an MT5-connected,
containerized trading platform.

## Repository layout

```text
.
├── config/
│   ├── grafana/provisioning/datasources/
│   ├── grafana/provisioning/dashboards/
│   ├── loki/
│   └── prometheus/
├── mt5/
│   ├── Experts/
│   ├── Include/
│   └── README.md
├── scripts/
│   └── dev/
├── schemas/
├── event_bus/
├── utils/
├── services/
│   ├── common/
│   ├── api_gateway/app/
│   ├── risk_engine/app/
│   ├── market_engine/app/
│   ├── market_data/app/
│   ├── pattern_engine/app/
│   ├── ai_engine/app/
│   ├── reconciliation/app/
│   ├── telegram_bot/app/
│   └── trade_monitor/app/
├── database/
├── migrations/
├── grafana/dashboards/
├── .dockerignore
├── .env.example
├── Dockerfile
├── docker-compose.dev.yml
├── docker-compose.prod.yml
└── requirements.txt
```

Every Python service exposes `/health` and `/metrics`. `market-data` implements
tick/bar ingress, TimescaleDB persistence, and rolling indicators;
`pattern-engine` implements a configurable multi-timeframe candidate strategy.
Broker order routing, account-specific execution controls, historical data
backfill, and production strategy validation remain outside this scaffold.

`market-data` accepts JSON tick/bar messages through Redis or ZeroMQ PULL
sockets, persists them to TimescaleDB, and computes rolling indicators.
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
economic calendar with `CALENDAR_API_URL`; until it is reachable, the engine
rejects order approvals.

`reconciliation` applies the Alembic ledger migration at startup, records
bar/signal/AI/order/execution event lineage, and reconciles open positions,
pending orders, closures, and account equity against a Windows-host MT5 adapter.
An unknown broker position or unavailable broker-state adapter places the
system in persistent `SAFE_MODE`; unknown positions trigger configured
PagerDuty/Telegram alerts. Reconciliation is also available through an
authenticated manual API request.

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
notification chat IDs in the untracked `.env`. Without a token, monitoring
remains available but Telegram control/notification delivery is disabled.

Grafana provisions `grafana/dashboards/trading_system.json` automatically.
The authenticated MT5 host-state bridge supplies Windows CPU/RAM samples to
the Telegram service, which exports them alongside service metrics to
Prometheus. AI token counts are explicitly estimates
(serialized characters divided by four), because the current Instructor
provider interface does not expose provider usage metadata. Account equity is
sampled from the live MT5 bridge and stored as a Prometheus time series only
while the telemetry service is running.

Shared Pydantic contracts live in `schemas/`, Redis Pub/Sub transport in
`event_bus/`, and order HMAC signing in `utils/security.py`. Redis Pub/Sub is
best-effort and does not retain messages for disconnected subscribers; use
event IDs for consumer deduplication and Redis Streams/a durable broker if
delivery guarantees are required.

## Development on Windows

1. Install Docker Desktop with the WSL 2 backend and start it.
2. Copy `.env.example` to `.env` and replace all development credentials and
   API-key placeholders.
3. On the Windows host with the MT5 terminal logged in, install the host bridge
   dependencies with `py -m pip install -r requirements-mt5-host.txt`, set
   `MT5_ADAPTER_TOKEN` to the same secret as `.env`, and start
   `.\scripts\run_mt5_host_adapter.ps1`. Restrict inbound TCP 8765 in Windows
   Firewall to the Docker host/network; keep the bridge token private.
4. Set the Telegram bot token and authorized numeric user IDs in `.env` before
   relying on Telegram control. Bot commands are accepted in private chats
   only; configure notification destinations separately if desired.
5. Run `.\scripts\dev\up.ps1` (or
   `docker compose --env-file .env -f docker-compose.dev.yml up --build -d`).
6. Check `http://localhost:8000/health`, Grafana at `http://localhost:3000`,
   and Prometheus at `http://localhost:9090`.

## Windows VPS production autostart

`scripts/install-production.ps1` installs boot tasks for MT5, the host-state
adapter, and the MT5/Docker watchdog; configures the Docker service for
automatic startup; applies a Windows Update maintenance policy; validates and
starts the production Compose stack. Before running it in an elevated
PowerShell session:

1. Install a supported Linux Docker Engine/Compose target for this Linux-image
   stack and confirm `docker info --format '{{.OSType}}'` reports `linux`.
   Windows Server's native Windows-container engine cannot run the TimescaleDB,
   Redis, and Python Linux containers in this Compose project. Windows Server
   deployments need a supported Linux VM/host or a Docker context to one; that
   engine's own service must be configured for autostart separately.
2. Compile `mt5/Experts/IndependentEquityGuard.mq5` with MetaEditor and copy
   the resulting `.ex5` into the logged-in MT5 account's data folder under
   `MQL5/Experts`. Configure the account's `Default` profile: attach the guard
   to its clean chart and add any trading EAs to their own charts/templates.
   The startup file selects the `Default` profile and explicitly starts the
   guard EA on EURUSD M5.
3. Replace the broker login, password, and server placeholders in
   `scripts/mt5_startup.ini`. The installer restricts that file's NTFS ACL to
   the MT5 account, SYSTEM, and Administrators. Use an MT5 Windows account
   that has logged on once, so its terminal data directory exists.
4. Configure `.env`, including distinct 32-byte `MT5_ADAPTER_TOKEN` and
   `RECONCILIATION_API_TOKEN` secrets. Install host dependencies with
   `py -m pip install -r requirements-mt5-host.txt`.
5. Run `.\scripts\install-production.ps1 -DockerServiceName docker` as
   Administrator. For Docker Desktop on Windows 11, pass its installed service
   name if different (commonly `com.docker.service`).

The equity guard writes a one-second heartbeat into the MT5 Common Files
directory. The watchdog allows the configured startup grace, then closes a
terminal that stops updating the heartbeat and relaunches it with
`mt5_startup.ini`. It also starts missing Compose services and restarts
unhealthy containers with a cooldown. Run `.\scripts\health-check.ps1` for
RAM/CPU/network, process, heartbeat, Docker, container, and SAFE_MODE checks.
The watchdog keeps a rotating log at `logs\host_watchdog.log`.
Windows active hours support only an 18-hour daily window; the installer uses
the Windows Update notify policy Sunday-Friday and schedules unattended
installation for the Saturday maintenance window. Domain Group Policy may
override local update policy and should be checked by the VPS administrator.

Development publishes PostgreSQL, Redis, Grafana, Prometheus, Loki, and the
Market Data Consumer's configured ZMQ ports on loopback only. MT5 terminal/EA traffic
on the Windows host can use `localhost` and the ports in `.env`.

## Production

Copy and harden `.env` on the VPS, then run:

```powershell
docker compose --env-file .env -f docker-compose.prod.yml up --build -d
```

The production compose file publishes the API Gateway and the MT5 ZMQ adapter
ports on loopback only; put a TLS-terminating reverse proxy or an authenticated
tunnel in front of the API. Database, Redis, and observability ports are not
published to the host. A host-installed MT5 terminal can use `localhost` for
the ZMQ ports. Use Docker network access or an SSH tunnel for Grafana.
Run the authenticated Windows MT5 host adapter alongside the logged-in terminal
and restrict its port to the Docker host using Windows Firewall. The
reconciliation service persists unknown-position SAFE_MODE until an operator
has reconciled the ledger and explicitly clears the mode through the protected
`/safe-mode/clear` endpoint.
Back up the named database, Redis, and observability volumes according to your
retention and recovery requirements.

Both compose files require a local `.env`; Compose does not load `.env.example`
automatically. Do not commit `.env` or use the example credentials in production.

## Notes

- PostgreSQL uses the TimescaleDB image and enables the extension during initial
  database initialization. For an existing database, enable it manually with
  `CREATE EXTENSION IF NOT EXISTS timescaledb;`.
- `services/common` provides shared liveness and Prometheus metrics endpoints.
- Prometheus discovers the seven API services by static Docker DNS targets.
- Loki is provisioned as a log store; add/configure a log shipper before
  expecting container logs to appear in Grafana.
- The ZMQ ports are published for the future Market Engine adapter. This
  baseline HTTP service does not yet bind ZMQ sockets.
