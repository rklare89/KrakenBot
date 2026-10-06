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
`STRATEGY_PRESET` (conservative | tuned_a | trend; see the tuning and alternative-strategies sections), `DRY_RUN`, `DRY_RUN_USD_BALANCE`, `DASHBOARD_HOST` (default 127.0.0.1), `DASHBOARD_PORT` (5000),
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

## Strategy tuning (Oct 2026): no robust edge found, so the default stays conservative

**Bottom line:** I could not find any parameter set that makes money reliably after fees on
the 5-coin watchlist when tested on data it wasn't tuned on. So the default stays on the
original rules, with a 72h cooldown added after each stop-loss. Those rules almost never
trade. The best set I found is included as an **opt-in** preset (`STRATEGY_PRESET=tuned_a`).
Its edge is small and it carries real drawdown risk.

### The original entry rule can't fire on 4h candles
Across 10 liquid coins from Jan 2023 to Oct 2026, 4h RSI went below 28 between about 200 and
300 times per coin. Every one of those times, price was *below* EMA50 (the highest
close/EMA50 ratio seen was 0.98). So "RSI < 28 and price > EMA50" made **0 trades in 3.75
years** on all 10 coins. That's why the bot appeared safe: it never traded.

### Method (`optimize.py`; rerun with `python optimize.py fetch && python optimize.py tune --extras`)
- **Data.** Kraken's public candle API only returns the latest 720 candles (~120 days on 4h,
  ~30 days on 1h). That's too little to tune on.
  - For history I used Coinbase's public 1h USD candles from Jan 2023, grouped into 4h
    candles. Their closes match Kraken's within a median of 1–4 basis points (0.01–0.04%)
    over the overlapping period.
  - Results were then re-checked on Kraken's own last 120 days.
- **Split.** One calendar cut-off for all coins. *Train* is Jan 2023 – 11 Jun 2025.
  *Test* (out-of-sample) is 12 Jun 2025 – 5 Oct 2026. The test period is also reported in
  two halves.
- **Search.**
  - Stage 1 tried 48 entry rules: RSI threshold 28–45; trend filter EMA50, EMA200, rising
    EMA50, or none; RSI below the threshold or crossing back above it.
  - Stage 2 tried 1,440 exit combinations for the best entry rules: take-profit, trail
    width, trail activation, stop-loss, max hold, and post-stop-loss cooldown (0/24/72h).
- **Selection.** Done on train data only, pooled across all 10 coins. The score is median
  return plus half the mean return, minus a drawdown penalty, minus one point per losing
  coin. Each set's score was averaged with its neighbours in the grid, so I picked a stable
  region rather than a lucky single point. Test results were looked at only after selection.
- **Simulation.** Same code as the live bot (`entry_signals` + `decide_exit`):
  - Entries fill at the next candle's open with the 0.25% maker fee.
  - Profit exits pay the maker fee and only fill at or above the fee floor.
  - Stop-losses pay the 0.40% taker fee plus 0.1% slippage.
  - Each candle is replayed as a price path matching the bot's 15-minute checks.
  - Also reported: an "all-taker" stress test (every fill pays 0.40%), and a portfolio
    simulation that holds one coin at a time, like the live bot.

### Out-of-sample results, 4h (test = 12 Jun 2025 – 5 Oct 2026; Kraken = last ~120 days)
Return is the mean net return per coin over the period. "Win" is the share of trades that
made money. Max DD is the worst drawdown on any single coin; in the portfolio row it is the
portfolio's drawdown.

| Settings | Universe | Period | Trades | Win % | Mean ret % | Coins +/− | Max DD % |
|---|---|---|---|---|---|---|---|
| old (RSI<28 & >EMA50, TP3, SL5, 72h) | watchlist (5) | test | 0 | – | 0.00 | 0/0 | 0.0 |
| default = old + 72h cooldown | watchlist (5) | test | 0 | – | 0.00 | 0/0 | 0.0 |
| tuned_a | watchlist (5) | test | 20 | 85.0 | **−1.43** | 2/3 | 11.8 |
| tuned_a | watchlist (5) | test, all-taker | 20 | 85.0 | −2.32 | 2/3 | 12.3 |
| tuned_a | watchlist (5) | Kraken 120d | 7 | 100 | +2.02 | 3/0 | 1.3 |
| tuned_a | watchlist + 5 majors (10) | test | 37 | 91.9 | +1.92 | 7/3 | 11.8 |
| tuned_a | 10 coins | test half 1 / half 2 | 17 / 20 | 88 / 95 | +0.15 / +1.80 | 7/2, 9/1 | 10.1 / 10.4 |
| tuned_a | 10 coins | test, all-taker | 37 | 91.9 | +1.05 | 7/3 | 12.3 |
| tuned_a | 10 coins | Kraken 120d | 17 | 100 | +2.52 | 8/0 | 3.7 |
| tuned_a, one coin at a time | watchlist (5) | test | 13 | 84.6 | **−6.45** (portfolio) | – | 16.1 |
| tuned_a, one coin at a time | 10 coins | test | 19 | 89.5 | +2.21 (portfolio) | – | 19.7 |

Old and default make 0 trades in every period, in both universes and on Kraken data.

**`tuned_a`** = RSI < 33 with close > EMA200, +2% take-profit, 2% trail armed at +1.5%
(it never goes below the fee floor), 12% stop-loss, 240h max hold, 72h cooldown. Watchlist:
SOL, AVAX, DOGE, NEAR, SUI, BTC, ETH, XRP, LINK, ADA.

- The trail width (1–3%) and the cooldown (0/24/72h) made almost no difference to its
  results.
- Requiring a tighter stop (≤ 8%) and hold (≤ 120h) gave out-of-sample losses: −0.4% mean
  per coin on 10 coins, −5.3% on the watchlist, and −24% for the one-coin-at-a-time watchlist
  portfolio.
- **1h candles** were worse. The best 1h train set lost out of sample: −3.0% mean per coin,
  4/6 coins up/down, −17% portfolio, about 21% drawdown.

### Why `tuned_a` is not the default
- **It loses money on the owner's own watchlist out-of-sample:** −1.4% per coin and −6.5%
  for the one-coin-at-a-time portfolio.
- **It only turns positive with 5 more coins,** and then only slightly: about +2% over 16
  months, against drawdowns of 12–20%.
- **Its high win rate comes from a quick +2% target paired with a wide 12% stop.** A few
  stop-losses can wipe out many small wins. That is not "safe trades".

The extra coins (BTC, ETH, XRP, LINK, ADA) are deep, liquid Kraken USD markets. They give the
dip-buying rule more chances and a calmer mix. That's the only reason they're in the opt-in
preset; the default watchlist is unchanged.

### New in this round
- **Entry rule options:** `RSI_ENTRY_MODE` (`below` | `cross_up`) and `TREND_FILTER`
  (`ema50` | `ema200` | `ema50_slope` | `none`), plus `EMA_LONG_PERIOD` (200) and
  `EMA_SLOPE_LOOKBACK` (6). `OHLCV_LIMIT` rises to 720 so EMA200 has enough history.
- **Cooldown:** `STOPLOSS_COOLDOWN_HOURS` (default 72). After a stop-loss closes a position,
  the bot won't re-enter that coin for 72h. This applies in both the live bot and the
  backtest, and the cooldown is saved in `bot_state.json` under `cooldowns`.
- **Presets:** env var `STRATEGY_PRESET` = `conservative` (default) | `tuned_a`. Try
  `DRY_RUN=true STRATEGY_PRESET=tuned_a` before ever trading it live.
- **Backtest:** now runs on numpy arrays, shares `simulate_exit_in_candle()` with the
  portfolio simulation, and reports max drawdown. The dashboard text describes whichever
  rules are active.
- **New files:**
  - `optimize.py`: `fetch`, `tune [--extras] [--timeframe 1h] [--max-stop X] [--max-hold H]`,
    and `compare`.
  - `tests/test_offline.py`: 67 checks, network blocked. Run with
    `python tests/test_offline.py`.
  - `.gitignore`: excludes the data cache, logs, state files and `.env`.

### Caveats
- **The history is a proxy.** It's Coinbase prices, not Kraken's. Kraken's own data covers
  only the last 120 days, which is a short and fairly favourable stretch.
- **Fills are modelled, not real.** Post-only entries are assumed to fill at the next open.
  In reality, buys in a falling market fill easily, while buys before a bounce may be missed;
  that bias works against the strategy. The all-taker stress test covers part of the fee side.
- **The sample is small:** 20–40 test trades. A few extra stop-losses would flip the result.
- **The comparison isn't fully clean.** I saw test results while trying the tighter-stop
  variant, so treat its comparison as indicative. No setting was moved toward a better test
  number.

## Alternative strategies (Oct 2026): daily trend following holds up out of sample; added as opt-in `trend` preset

I tested two different strategy types with the same method as the RSI tuning. **Daily
Donchian-breakout trend following** made money out of sample while BTC and the coin basket
lost about 21–22%. Its drawdown was under 15%, and its parameter neighbourhood was positive too.
It is now available as an **opt-in** preset, `STRATEGY_PRESET=trend`. **Momentum rotation**
also made money out of sample, but its drawdowns were 27–33%. That is too much for this bot,
so it is documented here but **not implemented**. The default preset is still `conservative`.

### Method (`python optimize.py strategies`; simulator in `portfolio_backtest.py`)
- **Data:**
  - Coinbase public 1h USD candles from Jan 2023, resampled to 4h and daily, used as a proxy
    for Kraken (closes are within a few bps).
  - Cross-check on Kraken's own daily candles (720, covering the whole test window).
- **Split:** tune on **Jan 2023 – 11 Jun 2025**; test once on **12 Jun 2025 – 5 Oct 2026**.
  The test period is also reported in two halves.
- **Universe:** SOL, AVAX, DOGE, NEAR, SUI, BTC, ETH, XRP, LINK, ADA. The original 5
  (SOL, AVAX, DOGE, NEAR, SUI) are reported separately.
- **Account:** a single portfolio, up to 80% invested.
  - Trend: k slots of 80%/k of equity each.
  - Momentum: 80%/top_k per coin.
- **Costs and fills:**
  - Fees are 0.25% maker for limit entries and exits, and 0.40% taker plus 0.1% slippage for
    stop exits.
  - All signals use closed candles and fill at the **next candle's open**.
  - Stops are checked inside each candle and fill at the lower of the open and the stop.
  - Stress test: every fill charged as taker.
- **Selection:** the set picked had the best *neighbour-smoothed* train score
  (return / max(maxDD, 5%), averaged with its grid neighbours). Test results were never used
  to choose.
- **Grids:**
  - Trend: entry 20/30/55 d; MA filter none/100/200 d; ATR trail none/2/3/4/6×; Donchian
    exit none/10/20 d; BTC>200d regime filter on/off; k = 1/3/5/10. That is 1008 sets per
    timeframe, on daily and on 4h.
  - Momentum: lookback 30/60/90 d; top 1/2/3; rebalance every 3/7/14 d; absolute-momentum
    filter on/off. Coins are held only while BTC > its 200-day MA. Rotation is charged maker
    fees on the coins that change.

### Out-of-sample results (portfolio level, $1000 start, 12 Jun 2025 – 5 Oct 2026)

| Strategy | Coins | Trades | Win % | Net return | Max DD |
|---|---|---|---|---|---|
| **Trend, daily** (chosen) | 10 | 28 | 53.6 | **+9.22%** | **13.9%** |
| Trend, daily, all-taker | 10 | 28 | 53.6 | +8.48% | 14.1% |
| Trend, daily, **Kraken data** | 10 | 28 | 50.0 | +9.24% | 14.7% |
| Trend, daily | original 5 | 13 | 53.8 | +7.31% | 9.6% |
| Trend, daily, Kraken data | original 5 | 13 | 53.8 | +8.30% | 9.8% |
| Trend, daily: test half 1 / half 2 | 10 | 13 / 15 | 38 / 67 | −6.78% / +17.17% | 13.6% / 6.7% |
| Trend, 4h (30d entry, no ATR) | 10 | 41 | 41.5 | +11.98% | 20.7% |
| Trend, 4h | original 5 | 20 | 40.0 | +10.35% | 12.4% |
| Momentum (30d, top 3, 3-day rebalance, abs filter) | 10 | 38 | 44.7 | +40.36% | 26.8% |
| Momentum, Kraken data | 10 | 38 | 47.4 | +39.68% | 28.2% |
| Momentum | original 5 | 25 | 52.0 | +38.42% | 32.5% |
| Momentum: test half 1 / half 2 | 10 | 24 / 11 | 38 / 36 | −1.39% / +31.12% | 26.3% / 10.5% |
| Momentum without BTC regime filter (ablation) | 10 | 62 | 38.7 | −15.63% | 63.0% |
| *Hold USD* | – | 0 | – | 0.00% | 0% |
| *Buy & hold BTC* | 1 | – | – | −20.79% | 53.1% |
| *Equal-weight buy & hold, 10 coins* | 10 | – | – | −22.28% | 69.6% |
| *Equal-weight buy & hold, original 5* | 5 | – | – | −19.76% | 72.4% |
| *RSI old (original rules, 4h)* | 10 / 5 | 0 / 0 | – | 0.00% | 0% |
| *RSI conservative (4h)* | 10 / 5 | 0 / 0 | – | 0.00% | 0% |
| *RSI tuned_a (4h, one coin at a time)* | 10 / 5 | 19 / 13 | 89.5 / 84.6 | +2.21% / −6.45% | 19.7% / 16.1% |

Train period, for reference:
- Trend, daily: +174% with 18.1% max DD.
- Momentum: +467% with 40.0% max DD.
- BTC buy-and-hold: +551% with 28.2% max DD.

In the bull market, trend following lags holding by a wide margin. Its value is mainly in
cutting losses in a falling market.

**Robustness of the daily trend set.** Each row changes one setting; test return / max DD:
- Default: +9.2% / 13.9%.
- Entry 30 d: +9.5% / 18.9%. Entry 20 d: +10.9% / 21.9%.
- MA 100: +6.5% / 16.8%. No MA filter: +6.8% / 17.3%.
- ATR 4×: +11.7% / 12.0%. No ATR trail: +8.8% / 14.3%.
- 20-day exit: +18.9% / 12.7%.
- BTC regime filter: +4.4% / 13.8%.
- All 20 of the top-20 train sets were positive on test, with a median of +7.9% (4h: 20/20,
  median +13.2%).
- **Position count matters a lot:** k=5 gives +13.8% / 23.4%, k=3 gives +48.4% / 15.7%, and
  k=1 gives +124% / 19.4%. Those smaller-k results come from very few trades (5 for k=1) and
  were poor in train (k=1: +28.5% with 45% DD). k=10, which diversifies the most, was chosen
  on train and is kept.

### Why daily trend and not 4h or momentum
- **4h trend** gave similar results with a larger drawdown (20.7%). It can't be checked on
  Kraken data, because 720 4h candles don't cover a 200-day MA. Daily candles are also much
  less sensitive to fill timing.
- **Momentum** had the highest return, but with 27–33% drawdowns. It always holds 1–3
  correlated coins whenever BTC is above its 200-day MA. It also depends heavily on the
  regime filter: without it, momentum lost 15.6% with a 63% drawdown. Its train drawdown was
  40–50%. That doesn't meet the "safe trades" goal, so it is not implemented. The simulator
  and grid remain in `optimize.py` / `portfolio_backtest.py`.

### What the `trend` preset does (`STRATEGY_PRESET=trend`, `STRATEGY_MODE='trend'`)
- **Entries:**
  - Uses Kraken **daily** candles for the 10 coins above.
  - **Entry:** the closed daily candle's close is above the prior 55-day high *and* above the
    200-day SMA.
  - Candidates are ranked by breakout strength.
  - Each position buys **8% of equity** (80% / 10), capped by free USD, as a **post-only**
    limit order at the bid. Entries are retried during that day and skipped if price has run
    more than 3% (`TREND_MAX_ENTRY_CHASE_PCT`) above the signal close.
- **Exits:**
  - **Normal exit:** a daily close below the prior 10-day low. The bot sells post-only at the
    ask and escalates to a marketable limit after `TREND_EXIT_MAX_ATTEMPTS` (4) unfilled
    tries.
  - **ATR trailing stop:** set at 6 × ATR(14) below the highest close, raised only. It is
    checked against the ticker on every loop. When hit, the bot sells with a marketable limit
    capped at `STOP_LOSS_MAX_SLIPPAGE_PCT` below the bid, with a
    `STOP_LOSS_ORDER_TIMEOUT_SEC` timeout. If it doesn't fill, the exit stays flagged and is
    retried on every loop.
- **Safety features kept, extended to multi-position:**
  - Positions live in `state["positions"]` and are written atomically.
  - Fills are taken from real order data (partial fills and dust handled).
  - Each order has a `pending_order` entry with `userref`, plus startup recovery (`mode: portfolio`).
  - Startup reconcile checks each position against balances.
  - `DRY_RUN` uses the paper wallet.
  - The `DASHBOARD_PASSWORD` auth is unchanged.
  - The bot won't trade if it finds a position from the other mode (RSI vs trend).
- **Dashboard:** shows a breakout-signal table and an Open Positions card.
- **New constants:** `STRATEGY_MODE` ('rsi'), `TREND_TIMEFRAME` ('1d'), `TREND_ENTRY_DAYS`
  (55), `TREND_MA_DAYS` (200), `TREND_EXIT_DAYS` (10), `TREND_ATR_MULT` (6),
  `TREND_ATR_PERIOD` (14), `TREND_MAX_POSITIONS` (10), `TREND_REGIME_FILTER` (False),
  `TREND_MAX_ENTRY_CHASE_PCT` (0.03), `TREND_EXIT_MAX_ATTEMPTS` (4).
- **Shared logic:** the signal and stop functions (`donchian_high/low`, `wilder_atr`,
  `trend_entry_signal`, `trend_exit_signal`, `trend_initial_stop`, `trend_update_stop`,
  `trend_stop_hit`) are shared by the live loop and `portfolio_backtest.trend_sim`.
- **Tests:** `tests/test_offline.py` has 15 new checks for live trend mode, 89 in total. They
  pass under each preset (`STRATEGY_PRESET=conservative|tuned_a|trend`) with the network
  blocked.
- Try it with `DRY_RUN=true STRATEGY_PRESET=trend python app.py` first.

### Caveats
- **One test regime.** The test period is a down market. Trend following is expected to do
  well there relative to holding, but it **lost 6.8% in the first half** of the test period.
  Profits came from the second half's rebound. Expect long flat or losing stretches; the
  win rate is about 50%.
- **Small sample:** 28 test trades. A different number of slots changes results a lot (see
  the k sensitivity above).
- **The history is a proxy.** It's Coinbase prices. The Kraken daily cross-check matches
  closely (+9.24% vs +9.22%), but it covers only the test window.
- **Fills are modelled.** The model assumes post-only orders fill at the next open, and
  breakouts can gap away from the limit price. The 3% chase limit means some real entries
  will be skipped. The all-taker stress test (+8.5%) covers the fee side but not missed fills.
- **The live multi-position code is new.** It is tested only offline against a fake
  exchange, never against the real Kraken API. Run it in `DRY_RUN` for a while first.
  Positions need at least $10 each (8% of equity), so the account needs more than about
  $125.
- **Momentum's results** came from a forced regime filter that the train grid itself did not
  prefer, which is a further reason not to rely on it.
