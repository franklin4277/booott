# Risk Engine

The service consumes:

- `signals.pattern` (`PatternSetupSignal`)
- `signals.ai` (`AIAnalysisResult`); required when `FEATURE_AI_SIGNALS=true`
- `risk.portfolio` (`PortfolioSnapshot`) with a fresh `AccountState`,
  `daily_starting_equity`, and each open position's current risk amount.
- `execution.reports` (`ExecutionReport`) to release reservations for failed or
  cancelled orders.
- `system.safe_mode` and the persistent Redis key `system:safe_mode`; missing
  or unreadable state fails closed and blocks order approvals.

Until a portfolio snapshot is available, approval is denied. The snapshot
producer is an internal trusted component and must calculate each position's
remaining loss-at-stop in account currency. Daily loss is computed as
`max(0, daily_starting_equity - current_equity)` and checked against both the
absolute and percentage limits.

Portfolio heat is the sum of current open risk and proposed stop risk. Proposed
risk is estimated as `abs(entry - stop) * volume * RISK_CONTRACT_UNITS`; set
contract units to the instrument contract size appropriate to the account and
price denomination. Correlated open risk uses absolute configured correlations
from `RISK_CORRELATION_MATRIX` (JSON mapping `"SYMBOL_A|SYMBOL_B"` to -1..1).
Correlations are policy inputs, not inferred market facts, and require operator
review.

Approved orders not yet represented in a portfolio snapshot are reserved
against heat and correlated exposure. Reservations are released on rejected,
cancelled, or failed execution reports and reconciled when a portfolio position
includes the originating signed-order ID in `source_order_id`.

News checks use the MT5 terminal's built-in economic calendar through
`mt5/Experts/EconomicCalendarBridge.mq5`, not an external calendar API.
Compile and attach that EA to a chart in the connected MT5 terminal. It writes
high-impact events for the next 48 hours to
`%APPDATA%\MetaQuotes\Terminal\Common\Files\booott_economic_calendar.tsv`
every 30 seconds as a UTF-16 snapshot, publishing complete files from a
temporary file so the risk engine never consumes a partially written snapshot.
The risk engine reads that shared file and checks the
configured ±15-minute window. `MT5_CALENDAR_FILE` can override the file path;
snapshots older than `MT5_CALENDAR_MAX_AGE_SECONDS` (180 seconds by default),
missing files, and malformed snapshots fail closed as
`NEWS_CALENDAR_UNAVAILABLE`.

Approvals are HMAC-SHA256 signed using `ORDER_SIGNING_SECRET` and expire five
seconds after issue time. Rejections are logged as structured
`RISK_REJECTION` messages and published to both `risk.rejections` and
`system.alerts`.
