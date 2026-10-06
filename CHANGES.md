# KrakenBot: safer order handling, fee-aware exits, and a backtest that matches live trading

The strategy is the same: 4h candles, buy when RSI < 28 and price > EMA50, +3% target,
trailing stop, 80% of free USD per trade, 15-minute loop. These changes make the bot record
only what actually filled, aim to keep every profit-taking exit above Kraken fees, add
downside protection, and make the dashboard scanner test the same rules the bot trades.

## Changes

1. **Order fill handling.** After each order the bot polls `fetch_order` until the order is
   closed. If it hasn't filled after `ORDER_TIMEOUT_SEC` the bot cancels it and fetches it
   once more to get the final result. Partial fills are handled. State and ledger are updated
   only from the actual filled amount, average price and fee (the fee is estimated from
   `MAKER_FEE`/`TAKER_FEE` when Kraken doesn't report one). An unfilled order never opens or
   closes a position. Each order gets a random `userref` and is saved as `pending_order`
   before it is sent. If `create_order` times out, the bot looks the order up by `userref`
   instead of guessing.
2. **Trailing stop with a fee floor.** Stop = `max(peak*(1-TRAIL_PCT), entry*(1+ROUND_TRIP_FEE+MIN_PROFIT_MARGIN))`.
   It arms once the *peak* reaches entry +1.5%. Profit-taking sells are post-only limits at
   `max(ask, fee floor)`, so they don't fill below breakeven plus margin.
   `MAKER_FEE=0.0025` and `TAKER_FEE=0.0040` are now constants.
3. **Downside protection.** `STOP_LOSS_PCT=0.05` and `MAX_HOLD_HOURS=72`. Set either to `None`
   to turn it off. The code comments note that an exchange-side stop order is a further option
   (it would still work if the PC is off).
4. **Limit pricing.** Markets load once. Buys are priced at the ticker bid and sells at the ask,
   as post-only orders (`params={'postOnly': True}`). Prices and amounts go through
   `price_to_precision`/`amount_to_precision`: buys never round up and sells never round down.
   Kraken's minimum amount and cost are checked before each order. Stop-loss exits use a
   marketable (taker) limit at `bid*(1-STOP_LOSS_MAX_SLIPPAGE_PCT)` with up to
   `STOP_LOSS_RETRIES` attempts.
5. **Position size tracking.** The amount actually bought is stored in state as `amount`.
   Exits sell only that amount, capped by the free balance, so other coins in the account are
   no longer sold.
6. **RSI** now uses Wilder's smoothing: `ewm(alpha=1/period, adjust=False)`.
7. **Closed candles only.** Signals use the last closed candle. The still-forming candle is
   dropped, which is the same as using the old `iloc[-2]`. A signal candle can trigger only one
   entry (`last_entry_signal` in state).
8. **One decision function for live and backtest.** `check_entry_signal()` and `decide_exit()`
   are used by both the live loop and the scanner. Exit order: stop-loss, then target, then the
   fee-floored trailing stop, then the optional RSI exit, then max hold.
   `USE_RSI_OVERBOUGHT_EXIT` defaults to `False`; when on, it only fires above the fee floor.
   The backtest:
   - uses `TIMEFRAME` and fetches `BACKTEST_DAYS=180` of history, paginated
   - enters at the next candle's open
   - walks a price path inside each candle, sized to the live 15-minute check interval
   - charges maker fees (taker fees plus slippage on stop-losses)
   - sizes positions like the live bot (80% of cash)
9. **Dashboard.** Binds to `127.0.0.1` by default (`DASHBOARD_HOST`, `DASHBOARD_PORT`).
   Optional HTTP basic auth via `DASHBOARD_PASSWORD` (user `DASHBOARD_USER`, default `admin`).
   The look is unchanged. Small changes: a "(DRY RUN)" label, the real number of backtest days
   shown, 4-decimal prices under $1, and the coin name in the ledger.
10. **Safer files and startup check.** State and ledger are written to a temp file and then
    swapped in with `os.replace` (with a retry for Windows file locks). On startup the bot
    resolves any `pending_order`, logs open orders on watchlist coins (and cancels them if
    `CANCEL_STRAY_ORDERS_ON_START=True`), and compares the held balance with state. It logs
    any mismatch, uses the exchange balance if it is lower, and moves old-format state over.
    The bot won't open a new trade on a coin that has open orders.
11. **Reliability.** One exchange instance is created and reused (plus a key-less one for the
    scanner thread). Network errors back off exponentially, starting at 30s and capped at the
    check interval. Exchange and authentication errors are caught and logged separately.
    `last_check` and `usd_balance` update on every loop, including while holding. All times
    are timezone-aware UTC.

**DRY_RUN.** `DRY_RUN=true` logs orders instead of sending them. Fills are simulated at the
limit price against a paper wallet (`DRY_RUN_USD_BALANCE`, default 1000). State and ledger go
to `bot_state.dryrun.json` and `trade_ledger.dryrun.json`. No API keys are needed. The default
is still **live** trading, as before.

## New env vars
`DRY_RUN`, `DRY_RUN_USD_BALANCE`, `DASHBOARD_HOST` (default 127.0.0.1), `DASHBOARD_PORT` (5000),
`DASHBOARD_USER` (admin), `DASHBOARD_PASSWORD` (unset = no auth). `KRAKEN_API_KEY` and
`KRAKEN_SECRET_KEY` are unchanged.

## New config constants (top of app.py)
`MAKER_FEE`, `TAKER_FEE`, `ROUND_TRIP_FEE` (= 2×maker), `MIN_PROFIT_MARGIN` (0.002),
`TAKE_PROFIT_PCT` (0.03), `TRAIL_ACTIVATE_PCT` (0.015), `TRAIL_PCT` (0.01), `STOP_LOSS_PCT` (0.05),
`MAX_HOLD_HOURS` (72), `STOP_LOSS_MAX_SLIPPAGE_PCT`, `STOP_LOSS_RETRIES`, `ORDER_TIMEOUT_SEC` (300),
`STOP_LOSS_ORDER_TIMEOUT_SEC` (60), `ORDER_POLL_SEC`, `USE_RSI_OVERBOUGHT_EXIT` (False),
`POSITION_SIZE_PCT` (0.80), `MIN_TRADE_USD` (10), `OHLCV_LIMIT`, `BACKTEST_DAYS` (180),
`BACKTEST_TAKER_SLIPPAGE_PCT`, `CANCEL_STRAY_ORDERS_ON_START` (False), `POSITION_TOLERANCE_PCT`.

## Behaviour changes to know about
- **Trailing stop activation changed.** The old code needed the *current* price to be at least
  entry +1.5% *and* at most 1% below the peak at the same moment, which almost never happens.
  The stop now arms on the peak.
- **History is shorter than requested.** Kraken's OHLC API returns only the latest 720 candles,
  so the scanner covers about 120 days on 4h, not 180.
- **Ledger entries have more fields.** Each entry now also has symbol, fee, order_id, reason,
  partial and pnl. Old entries still display.
- **A corrupt state file stops trading** instead of quietly resetting, so an open position is
  never forgotten.
