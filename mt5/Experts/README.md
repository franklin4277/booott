# Expert Advisors

Store MetaTrader 5 Expert Advisor source files (`.mq5`) in this directory.

`IndependentEquityGuard.mq5` is a standalone account-level equity and daily
drawdown guard. Copy `../Files/equity_guard_config.json` into the terminal's
`MQL5/Files/` directory, configure the account-currency limits, compile the EA,
and attach it to a dedicated chart.

The daily baseline is the account equity at the first EA startup/tick of the
server date, then persisted in terminal Global Variables for that date. On a
first install or if that stored value is removed, starting the guard mid-day
uses the current equity as the day's baseline.

On a trigger, the guard repeatedly attempts to close positions and delete
pending orders, including retries on its timer. MQL5 does not provide an API for
an EA to toggle the terminal-wide Algo Trading control; the EA raises an alert
and writes this required manual action to its critical log. Keep terminal
trading permissions enabled if the guard must be able to liquidate positions.
The alarm uses the terminal's `alert.wav` sound.
