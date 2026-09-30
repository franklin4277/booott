# MT5 State Reconciliation

The reconciliation service applies the Alembic ledger migration at container
startup and consumes `market.bars`, `signals.pattern`, `signals.ai`,
`orders.approved`, `execution.reports`, risk decisions, and system alerts into
`audit_logs`, `signals`, and `trades`. High-frequency ticks remain in the
TimescaleDB market-data hypertable rather than being duplicated in the audit
table. Bar-to-fill trace IDs provide signal lineage. Event IDs are unique for
replay deduplication. Redis Pub/Sub is best-effort; use a durable stream/broker
before relying on the ledger as the sole audit record.

## Windows MT5 host adapter

The official `MetaTrader5` Python package talks to a terminal on Windows and is
not imported into the Linux service container. Install and run the adapter on
the Windows VPS itself:

```powershell
py -m pip install -r requirements-mt5-host.txt
$env:MT5_ADAPTER_TOKEN = "<same random secret as .env>"
# Optional when more than one terminal is installed:
$env:MT5_TERMINAL_PATH = "C:\Program Files\Broker MT5\terminal64.exe"
# Optional; otherwise port 8765 is used:
$env:MT5_ADAPTER_PORT = "8765"
.\scripts\run_mt5_host_adapter.ps1
```

For production, configure and start the terminal adapter and watchdog using
`.\scripts\install-production.ps1` as an elevated Windows PowerShell session;
see the root README for the MT5 profile/EA setup and Docker engine prerequisites.

It listens on port 8765 and serves authenticated `GET /v1/state` snapshots
containing account data, open positions, pending orders, and closing deals
since the supplied `since` timestamp. Keep the terminal logged in and the
adapter running. The host adapter is intentionally separate from the Linux
container and must never be exposed directly to the public Internet. Restrict
port 8765 with Windows Firewall to the Docker host/network and use a unique
32-byte-or-longer bearer token. Compose defaults the URL to
`http://host.docker.internal:8765`.

The bridge reads MT5's actual position/order/deal state; it does not initiate
or close trades. Position correlation uses the MT5 position identifier, MT5
ticket, broker order ticket, or the order UUID if the execution adapter
preserves that UUID in the MT5 order comment. Unknown positions are never
adopted into the ledger automatically.

## Reconciliation and safe mode

Reconciliation runs immediately at startup and every
`RECONCILIATION_INTERVAL_SECONDS`; successful polling after an adapter outage
acts as connection-recovery reconciliation. `POST /reconcile` triggers a manual
run and requires `X-Reconciliation-Token` matching `RECONCILIATION_API_TOKEN`.
`GET /state` and `GET /health` report current health and SAFE_MODE state.
Compose binds the operations API to loopback on port 8010 by default; trigger a
manual run from PowerShell with:

```powershell
$env:RECONCILIATION_API_TOKEN = "<same secret configured in .env>"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/reconcile `
  -Headers @{ "X-Reconciliation-Token" = $env:RECONCILIATION_API_TOKEN }
```

Account balances/equity/margin/peak equity, signals, trades, and append-only
event audit records are stored in PostgreSQL. Missing closing deals are
reconciled using MT5 deal history from the most recent successful query (or
from the oldest currently open ledger trade after a service restart). Deal IDs
are tracked to avoid duplicate PnL updates; partial exits retain cumulative
realized values.

An open position that cannot be matched to a ledger trade persists
`manual_clear_required=true` in PostgreSQL, writes `system:safe_mode` in Redis,
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
