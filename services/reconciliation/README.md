# MT5 State Reconciliation

The reconciliation service consumes `market.bars`, `signals.pattern`, `signals.ai`,
`orders.approved`, `execution.reports`, risk decisions, and system alerts into
`audit_logs`, `signals`, and `trades`. Bar-to-fill trace IDs provide signal lineage.
Event IDs are unique for replay deduplication. Redis Pub/Sub is best-effort; use a
durable stream/broker before relying on the ledger as the sole audit record.

## Windows MT5 host adapter

The official `MetaTrader5` Python package talks to a terminal on Windows and is
not imported into the Python service process. Install and run the adapter on
the Windows VPS itself:

```powershell
py -m pip install -r requirements-mt5-host.txt
$env:MT5_ADAPTER_TOKEN = "<same random secret as .env.local>"
# Optional when more than one terminal is installed:
$env:MT5_TERMINAL_PATH = "C:\Program Files\Broker MT5\terminal64.exe"
# Optional; otherwise port 8765 is used:
$env:MT5_ADAPTER_PORT = "8765"
.\scripts\run_mt5_host_adapter.ps1
```

For production, start the terminal adapter and watchdog using the root README
procedures.

It listens on port 8765 and serves authenticated `GET /v1/state` snapshots
containing account data, open positions, pending orders, and closing deals
since the supplied `since` timestamp. Keep the terminal logged in and the
adapter running. The host adapter is intentionally separate from the trading
service process and must never be exposed directly to the public Internet.
Restrict port 8765 with Windows Firewall to localhost only and use a unique
32-byte-or-longer bearer token. The default URL is `http://localhost:8765`.

The bridge reads MT5's actual position/order/deal state and accepts only
unexpired, HMAC-signed market orders at `POST /v1/orders`. Broker routing is
disabled unless `MT5_LIVE_TRADING_ENABLED=true` on the Windows host and the
execution worker has both `FEATURE_PAPER_TRADING=false` and
`MT5_LIVE_TRADING_ENABLED=true`. Before enabling these settings, validate the
complete execution path on a demo account. The adapter records a pending
submission before calling the broker; ambiguous submissions are not retried
automatically and must be resolved through reconciliation.

The same host adapter can publish ticks and closed bars to the authenticated
market-data ingestion endpoints. Configure `MT5_MARKET_SYMBOLS`,
`MT5_MARKET_TIMEFRAMES`, and `MT5_MARKET_DATA_ENABLED` in `.env.local`.
Position correlation uses the MT5 position identifier, MT5 ticket, broker order
ticket, or client order UUID. Unknown positions are never adopted into the
ledger automatically.

## Reconciliation and safe mode

Reconciliation runs immediately at startup and every
`RECONCILIATION_INTERVAL_SECONDS`; successful polling after an adapter outage
acts as connection-recovery reconciliation. `POST /reconcile` triggers a manual
run and requires `X-Reconciliation-Token` matching `RECONCILIATION_API_TOKEN`.
`GET /state` and `GET /health` report current health and SAFE_MODE state.
The operations API listens on port 8010 by default; trigger a manual run from
PowerShell with:

```powershell
$env:RECONCILIATION_API_TOKEN = "<same secret configured in .env.local>"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/reconcile `
  -Headers @{ "X-Reconciliation-Token" = $env:RECONCILIATION_API_TOKEN }
```

Account balances/equity/margin/peak equity, signals, trades, and append-only
event audit records are stored in SQLite by default. Missing closing deals are
reconciled using MT5 deal history from the most recent successful query (or
from the oldest currently open ledger trade after a service restart). Deal IDs
are tracked to avoid duplicate PnL updates; partial exits retain cumulative
realized values.

An open position that cannot be matched to a ledger trade persists
`manual_clear_required=true` in the database, writes `system:safe_mode` in Redis,
publishes `system.safe_mode`, and raises `system.alerts`. The Risk Engine reads
the persistent Redis state before approving each order and rejects approvals
while SAFE_MODE is active. PagerDuty and Telegram critical notifications are
sent when their credentials are configured. Resolve the position/ledger
discrepancy first, then run a clean reconciliation and use the authenticated
`POST /safe-mode/clear` endpoint to clear the latched mode:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/safe-mode/clear `
  -Headers @{ "X-Reconciliation-Token" = $env:RECONCILIATION_API_TOKEN }
```

Configure `PAGERDUTY_ROUTING_KEY`, or both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`, in the deployment environment. Critical alerts are also
logged and published internally regardless of external notification setup.
