"""Portfolio-level backtests for alternative strategies (offline, public data only).

Strategies
  trend    : Donchian breakout (close above the prior N-day high), optional long-MA
             filter and BTC>200d regime filter; exits on an ATR trailing stop
             (intrabar, taker fee + slippage) and/or a close below the prior M-day low
             (next open). Up to K concurrent positions, each sized 80%/K of equity.
  momentum : every R days rank coins by L-day return, hold the top k (80% of equity
             split equally) while BTC is above its 200-day MA (checked daily), optional
             absolute-momentum filter (own L-day return > 0).
  benchmarks: buy-and-hold BTC, buy-and-hold equal-weight universe.

Conventions (same as the RSI study): signals on CLOSED bars, fills at the next bar's
open with the maker fee (0.25%); stop exits fill at min(open, stop) minus 0.1% slippage
with the taker fee (0.40%). One account, mark-to-market equity at each close; max
drawdown is on that equity curve. Positions open at the end are valued at the close
minus the maker fee.
"""
import numpy as np
import pandas as pd

MAKER_FEE = 0.0025
TAKER_FEE = 0.0040
STOP_SLIPPAGE = 0.001
EXPOSURE = 0.80          # like the live bot: use 80% of equity, keep 20% USD
MIN_TRADE_USD = 10.0
START_CASH = 1000.0


# ----------------------------------------------------------------------------- panel
class Panel:
    """Aligned OHLC arrays (T x n) for a universe; NaN where a coin has no data."""

    def __init__(self, dfs, btc_df, bars_per_day):
        ts = sorted(set().union(*[set(d['timestamp']) for d in dfs.values()]) | set(btc_df['timestamp']))
        self.ts = np.array(ts, dtype='int64')
        self.coins = list(dfs)
        self.bpd = bars_per_day
        idx = pd.Index(self.ts)

        def mat(col):
            return np.column_stack([d.set_index('timestamp')[col].reindex(idx).to_numpy(dtype=float)
                                    for d in dfs.values()])
        self.O, self.H, self.L, self.C = mat('open'), mat('high'), mat('low'), mat('close')
        btc = btc_df.set_index('timestamp')['close'].reindex(idx)
        self.btc = btc.to_numpy(dtype=float)
        self._cache = {}

    def T(self):
        return len(self.ts)

    # indicators (cached); values at row t use data up to and including bar t
    def donch_hi(self, days):
        k = ('dh', days)
        if k not in self._cache:
            n = int(days * self.bpd)
            self._cache[k] = pd.DataFrame(self.H).rolling(n, min_periods=n).max().shift(1).to_numpy()
        return self._cache[k]

    def donch_lo(self, days):
        k = ('dl', days)
        if k not in self._cache:
            n = int(days * self.bpd)
            self._cache[k] = pd.DataFrame(self.L).rolling(n, min_periods=n).min().shift(1).to_numpy()
        return self._cache[k]

    def sma(self, days):
        k = ('ma', days)
        if k not in self._cache:
            n = int(days * self.bpd)
            self._cache[k] = pd.DataFrame(self.C).rolling(n, min_periods=n).mean().to_numpy()
        return self._cache[k]

    def ret(self, days):
        k = ('ret', days)
        if k not in self._cache:
            n = int(days * self.bpd)
            c = pd.DataFrame(self.C)
            self._cache[k] = (c / c.shift(n) - 1).to_numpy()
        return self._cache[k]

    def atr(self, period=14):
        k = ('atr', period)
        if k not in self._cache:
            prev_c = np.vstack([np.full((1, self.C.shape[1]), np.nan), self.C[:-1]])
            tr = np.nanmax(np.stack([self.H - self.L, np.abs(self.H - prev_c), np.abs(self.L - prev_c)]), axis=0)
            tr[np.isnan(self.C)] = np.nan
            self._cache[k] = pd.DataFrame(tr).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().to_numpy()
        return self._cache[k]

    def btc_regime(self, days=200):
        k = ('regime', days)
        if k not in self._cache:
            n = int(days * self.bpd)
            ma = pd.Series(self.btc).rolling(n, min_periods=n).mean().to_numpy()
            self._cache[k] = self.btc > ma          # False while MA is NaN
        return self._cache[k]


# ----------------------------------------------------------------------------- account
class Account:
    def __init__(self, cash=START_CASH, maker=MAKER_FEE, taker=TAKER_FEE):
        self.cash = cash
        self.maker, self.taker = maker, taker
        self.pos = {}            # j -> dict(qty, cost, entry_px, ...)
        self.trades = []         # net pnl per round trip
        self.fees = 0.0

    def buy(self, j, px, usd, **extra):
        usd = min(usd, self.cash)
        if usd < MIN_TRADE_USD or not np.isfinite(px) or px <= 0:
            return False
        fee = usd * self.maker
        self.pos[j] = dict(qty=(usd - fee) / px, cost=usd, entry_px=px, **extra)
        self.cash -= usd
        self.fees += fee
        return True

    def sell(self, j, px, taker=False, slippage=0.0):
        p = self.pos.pop(j)
        fill = px * (1 - slippage)
        gross = p['qty'] * fill
        fee = gross * (self.taker if taker else self.maker)
        self.cash += gross - fee
        self.fees += fee
        self.trades.append(gross - fee - p['cost'])

    def equity(self, prices):
        v = self.cash
        for j, p in self.pos.items():
            px = prices[j]
            v += p['qty'] * (px if np.isfinite(px) else p['entry_px'])
        return v


def _metrics(acct, eq, ts, panel_close_last):
    for j in list(acct.pos):
        acct.sell(j, panel_close_last[j])
    eq = np.asarray(eq)
    final = acct.cash
    peak = np.maximum.accumulate(eq) if len(eq) else np.array([START_CASH])
    dd = float(np.max((peak - eq) / peak)) if len(eq) else 0.0
    n = len(acct.trades)
    days = (ts[-1] - ts[0]) / 86_400_000 if len(ts) > 1 else 0
    return {'trades': n, 'win_rate': round(100 * sum(x > 0 for x in acct.trades) / n, 1) if n else 0.0,
            'return_pct': round(100 * (final - START_CASH) / START_CASH, 2),
            'max_dd': round(100 * dd, 2), 'days': round(days),
            'fees_pct': round(100 * acct.fees / START_CASH, 2)}


def _range(panel, start_ms, end_ms):
    ts = panel.ts
    i0 = int(np.searchsorted(ts, start_ms, 'left')) if start_ms is not None else 1
    i1 = int(np.searchsorted(ts, end_ms, 'left')) if end_ms is not None else len(ts)
    return max(i0, 1), i1


# ----------------------------------------------------------------------------- trend
def trend_sim(panel, p, start_ms=None, end_ms=None, maker=MAKER_FEE, taker=TAKER_FEE):
    """p: entry_days, ma_days (0 = off), atr_mult (None = off), exit_days (None = off),
    regime (bool), k (max concurrent positions)."""
    O, H, L, C = panel.O, panel.H, panel.L, panel.C
    dh = panel.donch_hi(p['entry_days'])
    dl = panel.donch_lo(p['exit_days']) if p.get('exit_days') else None
    ma = panel.sma(p['ma_days']) if p.get('ma_days') else None
    atr = panel.atr(14)
    reg = panel.btc_regime(200) if p.get('regime') else None
    k, mult = p['k'], p.get('atr_mult')
    acct = Account(maker=maker, taker=taker)
    i0, i1 = _range(panel, start_ms, end_ms)
    eq, pend_exit, pend_entry = [], set(), []
    for t in range(i0, i1):
        # --- at the open: pending signal exits, then entries
        for j in list(pend_exit):
            if j in acct.pos and np.isfinite(O[t, j]):
                acct.sell(j, O[t, j])
        pend_exit.clear()
        if pend_entry:
            equity = acct.equity(C[t - 1])
            for j in pend_entry:
                if len(acct.pos) >= k or j in acct.pos or not np.isfinite(O[t, j]):
                    continue
                stop = O[t, j] - mult * atr[t - 1, j] if mult else None
                acct.buy(j, O[t, j], equity * EXPOSURE / k, stop=stop, hi=O[t, j])
            pend_entry = []
        # --- during the bar: ATR trailing stop (taker + slippage)
        if mult:
            for j in list(acct.pos):
                s = acct.pos[j]['stop']
                if s is not None and np.isfinite(L[t, j]) and L[t, j] <= s:
                    acct.sell(j, min(O[t, j], s), taker=True, slippage=STOP_SLIPPAGE)
        # --- at the close: update stops, exit signals, entry signals
        for j, pos in acct.pos.items():
            if not np.isfinite(C[t, j]):
                continue
            pos['hi'] = max(pos['hi'], C[t, j])
            if mult and np.isfinite(atr[t, j]):
                pos['stop'] = max(pos['stop'] if pos['stop'] is not None else -np.inf, pos['hi'] - mult * atr[t, j])
            if dl is not None and np.isfinite(dl[t, j]) and C[t, j] < dl[t, j]:
                pend_exit.add(j)
        if len(acct.pos) < k and (reg is None or reg[t]):
            with np.errstate(invalid='ignore'):
                sig = C[t] > dh[t]
                if ma is not None:
                    sig &= C[t] > ma[t]
            cands = [j for j in np.flatnonzero(sig) if j not in acct.pos]
            # strongest breakout first
            cands.sort(key=lambda j: C[t, j] / dh[t, j], reverse=True)
            pend_entry = cands[:k - len(acct.pos)]
        eq.append(acct.equity(C[t]))
    return _metrics(acct, eq, panel.ts[i0:i1], C[i1 - 1])


# ----------------------------------------------------------------------------- momentum
def momentum_targets(rets_row, regime_ok, top_k, abs_filter):
    """Shared ranking rule: indices of the top_k coins by lookback return (NaN skipped);
    empty if the BTC regime is off; optional absolute-momentum filter (return > 0)."""
    if not regime_ok:
        return []
    order = [j for j in np.argsort(-np.nan_to_num(rets_row, nan=-np.inf)) if np.isfinite(rets_row[j])]
    if abs_filter:
        order = [j for j in order if rets_row[j] > 0]
    return order[:top_k]


def momentum_sim(panel, p, start_ms=None, end_ms=None, maker=MAKER_FEE, taker=TAKER_FEE):
    """p: lookback_days, top_k, rebalance_days, abs_filter (bool), regime (bool)."""
    O, C = panel.O, panel.C
    r = panel.ret(p['lookback_days'])
    reg = panel.btc_regime(200) if p.get('regime', True) else np.ones(panel.T(), dtype=bool)
    step = int(p['rebalance_days'] * panel.bpd)
    acct = Account(maker=maker, taker=taker)
    i0, i1 = _range(panel, start_ms, end_ms)
    eq = []
    for t in range(i0, i1):
        rebalance = (t - i0) % step == 0
        regime_off = not reg[t - 1]
        if rebalance or (regime_off and acct.pos):
            target = momentum_targets(r[t - 1], bool(reg[t - 1]), p['top_k'], p['abs_filter'])
            for j in [j for j in acct.pos if j not in target]:
                if np.isfinite(O[t, j]):
                    acct.sell(j, O[t, j])
            new = [j for j in target if j not in acct.pos]
            if new:
                equity = acct.equity(C[t - 1])
                for j in new:
                    acct.buy(j, O[t, j], equity * EXPOSURE / p['top_k'])
        eq.append(acct.equity(C[t]))
    return _metrics(acct, eq, panel.ts[i0:i1], C[i1 - 1])


# ----------------------------------------------------------------------------- benchmarks
def buy_hold(panel, cols, start_ms=None, end_ms=None, exposure=1.0):
    O, C = panel.O, panel.C
    acct = Account()
    i0, i1 = _range(panel, start_ms, end_ms)
    live = [j for j in cols if np.isfinite(O[i0, j])]
    for j in live:
        acct.buy(j, O[i0, j], START_CASH * exposure / len(live))
    eq = [acct.equity(C[t]) for t in range(i0, i1)]
    return _metrics(acct, eq, panel.ts[i0:i1], C[i1 - 1])
