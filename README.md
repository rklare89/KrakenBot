# KrakenBot
A Windows service that places trades on Kraken Pro, with a Flask dashboard.

## Running
```
pip install ccxt pandas numpy flask python-dotenv
set KRAKEN_API_KEY=...            (not needed with DRY_RUN=true)
set KRAKEN_SECRET_KEY=...
set DRY_RUN=true                  (paper trading: no orders are sent)
set STRATEGY_PRESET=conservative  (see below)
python app.py                     dashboard on http://127.0.0.1:5000
```
Other env vars:
- `DRY_RUN_USD_BALANCE`: paper wallet size (default 1000).
- `DASHBOARD_HOST` / `DASHBOARD_PORT`: where the dashboard listens.
- `DASHBOARD_USER` / `DASHBOARD_PASSWORD`: basic auth. Set a password before binding to `0.0.0.0`.

Dry runs keep their own state and ledger: `bot_state.dryrun.json` and `trade_ledger.dryrun.json`.

## Strategy presets (`STRATEGY_PRESET`)
| Preset | Mode | What it does | Out-of-sample backtest (Jun 2025 – Oct 2026) |
|---|---|---|---|
| `conservative` (default) | single position, 4h RSI | The original RSI dip-buy rules plus a 72h cooldown after a stop-loss. They rarely trigger. | 0 trades |
| `tuned_a` | single position, 4h RSI | RSI < 33 above EMA200, +2% target, 12% stop, 10 coins | +2.2% / 19.7% max DD |
| `trend` | up to 10 positions, daily | 55-day breakout above the 200-day MA. Exits on the 10-day low or a 6×ATR trailing stop. 8% of equity per coin. | +9.2% / 13.9% max DD |
| `momentum` | up to 2 positions, daily | Weekly rotation into the top 2 of 10 coins (needs top-2 on 2 of the 30/60/90-day returns, and positive momentum). Sized to 60%/yr volatility, max 80% invested. Holds USD while BTC < 200-day MA. | +35.9% / 22.4% max DD |

For comparison, over the same period BTC buy-and-hold made −21.5% with a 53% drawdown.

- **Always paper-trade a new preset first:** `set DRY_RUN=true` and `set STRATEGY_PRESET=momentum`, then run `python app.py`.
- **Don't switch presets with positions open.** The bot refuses to trade if it finds positions opened by a different mode (RSI, trend or momentum). Close them, or switch back.
- **Backtests aren't predictions.** Each preset's caveats are in CHANGES.md. For momentum, the 10-coin list was chosen with hindsight, so expect less than the backtest.
- **Account size:** each order must be at least $10. Trend needs more than about $125. Momentum needs about $100 or more, because a volatile coin's weight can drop to around 15%.

## Research tools (public data only, no API keys)
- `python optimize.py fetch` then `python optimize.py tune --extras`: RSI parameter study.
- `python optimize.py strategies`: trend-following and momentum portfolio backtests.
- `python universe_data.py` then `python optimize.py universes`: momentum on wider coin lists.
- `python optimize.py momentum_check`: backtest of the live `momentum` settings.
- `python tests/test_offline.py`: offline test suite (network blocked).
