"""Offline parameter tuning / walk-forward check for app.py (public data only, no keys).

Usage:
    python optimize.py fetch                 # download public candles into ./data (Kraken + Coinbase)
    python optimize.py tune                  # grid search on the TRAIN period, report TEST period
    python optimize.py compare               # old vs current app.py settings, train/test + Kraken
    python optimize.py tune --timeframe 1h   # same on 1h candles

Data:
  * Kraken's public OHLC endpoint only serves the latest 720 candles (~120 days of 4h,
    ~30 days of 1h), far too little to tune on. For history we use Coinbase Exchange's
    public 1h USD candles since 2023 (resampled to 4h; same UTC boundaries as Kraken).
    Prices of these liquid USD pairs track Kraken's within a few bps, so this is a
    reasonable proxy; results are then re-checked on Kraken's own recent candles.
Method:
  * Indicators are computed on the full series (they are causal), then each symbol is
    split by time: first TRAIN_FRAC (65%) = train, rest = test. Parameters are chosen
    on TRAIN only, pooled across all symbols, favouring parameter sets whose grid
    neighbours are also good (robustness), then reported once on TEST.
  * The backtest is app.backtest_on_dataframe(), i.e. the same entry_signals() /
    decide_exit() code the live bot uses, with maker fees on entries and profit exits,
    taker fee + slippage on stop-losses, and an intra-candle price path.
"""
import argparse
import itertools
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

import app

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
WATCH = ['SOL', 'AVAX', 'DOGE', 'NEAR', 'SUI']
EXTRA = ['BTC', 'ETH', 'XRP', 'LINK', 'ADA']
TRAIN_FRAC = 0.65
COLS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

# Settings shipped in the original revised bot (for the old-vs-new comparison).
OLD_PARAMS = dict(RSI_OVERSOLD=28, RSI_ENTRY_MODE='below', TREND_FILTER='ema50',
                  TAKE_PROFIT_PCT=0.03, TRAIL_PCT=0.01, TRAIL_ACTIVATE_PCT=0.015,
                  STOP_LOSS_PCT=0.05, MAX_HOLD_HOURS=72, STOPLOSS_COOLDOWN_HOURS=0)
PARAM_KEYS = list(OLD_PARAMS)


def current_params():
    return {k: getattr(app, k) for k in PARAM_KEYS}


def apply_params(p):
    for k, v in p.items():
        setattr(app, k, v)


# ----------------------------------------------------------------------------- data
def fetch(out=DATA_DIR, since='2023-01-01T00:00:00Z'):
    import ccxt
    os.makedirs(out, exist_ok=True)
    k = ccxt.kraken({'enableRateLimit': True})
    for b in WATCH + EXTRA:
        for tf in ('4h', '1h'):
            o = k.fetch_ohlcv(f'{b}/USD', tf, limit=720)
            pd.DataFrame(o, columns=COLS).to_csv(f'{out}/kraken_{b}_{tf}.csv', index=False)
        print('kraken', b, flush=True)
    cb = ccxt.coinbaseexchange({'enableRateLimit': True})
    for b in WATCH + EXTRA:
        rows, t, now = {}, cb.parse8601(since), cb.milliseconds()
        while t < now:
            for _ in range(5):
                try:
                    batch = cb.fetch_ohlcv(f'{b}/USD', '1h', since=t, limit=300)
                    break
                except Exception as e:  # noqa: BLE001 - public data, just retry
                    print('retry', b, e, flush=True)
                    time.sleep(3)
            else:
                batch = []
            for c in batch:
                rows[c[0]] = c
            t += 300 * 3_600_000
        pd.DataFrame([rows[x] for x in sorted(rows)], columns=COLS).to_csv(
            f'{out}/coinbase_{b}_1h.csv', index=False)
        print('coinbase', b, len(rows), flush=True)


def resample(df, tf):
    if tf == '1h':
        return df.reset_index(drop=True)
    tf_ms = app.ccxt.Exchange.parse_timeframe(tf) * 1000
    g = df.assign(bucket=df['timestamp'] // tf_ms * tf_ms).groupby('bucket')
    out = pd.DataFrame({'timestamp': g['timestamp'].first().index, 'open': g['open'].first().values,
                        'high': g['high'].max().values, 'low': g['low'].min().values,
                        'close': g['close'].last().values, 'volume': g['volume'].sum().values,
                        'n': g.size().values})
    need = tf_ms // 3_600_000
    out = out[out['n'] >= max(1, need - 1)].drop(columns='n')   # drop sparse buckets
    return out.iloc[:-1].reset_index(drop=True)                 # last bucket may be partial


def load(source, tf, symbols):
    dfs = {}
    for b in symbols:
        if source == 'coinbase':
            path = f'{DATA_DIR}/coinbase_{b}_1h.csv'
        else:
            path = f'{DATA_DIR}/kraken_{b}_{tf}.csv'
        if not os.path.exists(path):
            print('missing', path)
            continue
        df = pd.read_csv(path)
        if source == 'coinbase':
            df = resample(df, tf)
        else:
            df = df.iloc[:-1].reset_index(drop=True)        # drop forming candle
        dfs[b] = app.add_indicators(df)
    return dfs


CUTOFF_MS = None   # set by set_cutoff(): one calendar date for all symbols


def set_cutoff(dfs):
    """Train/test boundary = TRAIN_FRAC of the overall calendar span (same date for
    every symbol, so the test period is the same market regime for all of them)."""
    global CUTOFF_MS
    t0 = min(int(d['timestamp'].iloc[0]) for d in dfs.values())
    t1 = max(int(d['timestamp'].iloc[-1]) for d in dfs.values())
    CUTOFF_MS = t0 + int((t1 - t0) * TRAIN_FRAC)
    return CUTOFF_MS


def split(df, part):
    if part == 'train':
        return df[df['timestamp'] < CUTOFF_MS].reset_index(drop=True)
    if part == 'test':
        return df[df['timestamp'] >= CUTOFF_MS].reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------- evaluation
_G = {}


def _init(dfs_by_part, tf):
    _G['dfs'] = dfs_by_part
    _G['tf'] = tf


def _run(p):
    apply_params(p)
    out = {part: {b: app.backtest_on_dataframe(df, _G['tf']) for b, df in dfs.items()}
           for part, dfs in _G['dfs'].items()}
    return p, out


def summarize(per_sym):
    rets = [r['return_pct'] for r in per_sym.values()]
    trades = sum(r['trades'] for r in per_sym.values())
    pnls = [x for r in per_sym.values() for x in r['pnls']]
    wins = sum(1 for x in pnls if x > 0)
    return {
        'trades': trades,
        'win_rate': round(100 * wins / trades, 1) if trades else 0.0,
        'mean_ret': round(float(np.mean(rets)), 2) if rets else 0.0,
        'median_ret': round(float(np.median(rets)), 2) if rets else 0.0,
        'pos_syms': sum(1 for x in rets if x > 0),
        'neg_syms': sum(1 for x in rets if x < 0),
        'n_syms': len(rets),
        'worst_dd': round(max((r['max_drawdown_pct'] for r in per_sym.values()), default=0.0), 2),
        'mean_dd': round(float(np.mean([r['max_drawdown_pct'] for r in per_sym.values()])), 2) if rets else 0.0,
        'avg_trade_pct': round(100 * float(np.mean(pnls)) / (app.BACKTEST_START_CASH * app.POSITION_SIZE_PCT), 3) if pnls else 0.0,
    }


def score(s, min_trades):
    """Train-period objective: robust (median + mean) return, penalise drawdown and
    symbols that lose money; parameter sets with too few trades are ineligible."""
    if s['trades'] < min_trades:
        return -1e9
    return s['median_ret'] + 0.5 * s['mean_ret'] - 0.25 * s['mean_dd'] - 1.0 * s['neg_syms']


def run_grid(grid, dfs_by_part, tf, procs):
    with Pool(procs, initializer=_init, initargs=(dfs_by_part, tf)) as pool:
        return pool.map(_run, grid, chunksize=4)


def print_table(rows, title):
    print(f'\n== {title}')
    hdr = f"{'set':52s} {'part':6s} {'trades':>6s} {'win%':>6s} {'meanRet%':>9s} {'medRet%':>8s} {'+/-syms':>8s} {'worstDD%':>9s} {'avgTrade%':>9s}"
    print(hdr)
    for name, part, s in rows:
        print(f"{name:52s} {part:6s} {s['trades']:6d} {s['win_rate']:6.1f} {s['mean_ret']:9.2f} {s['median_ret']:8.2f} "
              f"{s['pos_syms']:>3d}/{s['neg_syms']:<3d}  {s['worst_dd']:9.2f} {s['avg_trade_pct']:9.3f}")


# ----------------------------------------------------------------------------- portfolio
def portfolio_sim(dfs, tf, start_cash=app.BACKTEST_START_CASH):
    """Live-like rotation: one position at a time across all symbols, picking the
    highest entry score (same as scan_and_enter), using the shared exit model."""
    tf_sec = app.ccxt.Exchange.parse_timeframe(tf)
    steps = max(4, int(tf_sec // app.CHECK_INTERVAL_SEC))
    prepared = {}
    for b, df in dfs.items():
        sig, sc = app.entry_signals(df)
        prepared[b] = df.assign(sig=sig.values, score=sc.values).set_index('timestamp')
    all_ts = sorted(set().union(*[set(p.index) for p in prepared.values()]))
    cash, pos, sym, amount, entry_cost = start_cash, None, None, 0.0, 0.0
    cooldown = {}
    pnls, eq_peak, max_dd = [], start_cash, 0.0
    prev_ts = None
    cd_ms = int((app.STOPLOSS_COOLDOWN_HOURS or 0) * 3_600_000)
    for t in all_ts:
        if pos is None and prev_ts is not None:
            best = None
            for b, p in prepared.items():
                if prev_ts in p.index and t in p.index and p.at[prev_ts, 'sig'] and t >= cooldown.get(b, -1):
                    s = p.at[prev_ts, 'score']
                    if best is None or s > best[1]:
                        best = (b, s)
            trade_usd = cash * app.POSITION_SIZE_PCT
            if best and trade_usd >= app.MIN_TRADE_USD:
                sym = best[0]
                ep = prepared[sym].at[t, 'open']
                amount = (trade_usd * (1 - app.MAKER_FEE)) / ep
                cash -= trade_usd
                entry_cost = trade_usd
                pos = {'entry_price': ep, 'peak': ep,
                       'entry_time': pd.Timestamp(t, unit='ms', tz='UTC').to_pydatetime()}
        if pos is not None and t in prepared[sym].index:
            r = prepared[sym].loc[t]
            rsi_prev = prepared[sym].at[prev_ts, 'rsi'] if prev_ts in prepared[sym].index else None
            res = app.simulate_exit_in_candle(pos, r['open'], r['high'], r['low'], r['close'],
                                              t, tf_sec, steps, rsi_prev)
            if res is not None:
                fp, fee, d, et = res
                net = amount * fp * (1 - fee)
                pnls.append(net - entry_cost)
                cash += net
                if d.urgent and cd_ms:
                    cooldown[sym] = int(et.timestamp() * 1000) + cd_ms
                pos, amount = None, 0.0
            eq = cash + amount * r['close']
        else:
            eq = cash
        eq_peak = max(eq_peak, eq)
        max_dd = max(max_dd, (eq_peak - eq) / eq_peak)
        prev_ts = t
    if pos is not None:
        cash += amount * prepared[sym]['close'].iloc[-1] * (1 - app.MAKER_FEE)
    wins = sum(1 for x in pnls if x > 0)
    return {'trades': len(pnls), 'win_rate': round(100 * wins / len(pnls), 1) if pnls else 0.0,
            'return_pct': round(100 * (cash - start_cash) / start_cash, 2), 'max_dd': round(100 * max_dd, 2)}


# ----------------------------------------------------------------------------- main
CATEGORICAL = ('TREND_FILTER', 'RSI_ENTRY_MODE')


def neighbours(p, axes):
    """Grid neighbours of p: change one ORDINAL parameter by one step along its axis
    (categorical choices like the trend filter have no meaningful 'neighbour')."""
    out = []
    for k, vals in axes.items():
        if k in CATEGORICAL or k not in p or p[k] not in vals:
            continue
        i = vals.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                out.append(tuple(sorted({**p, k: vals[j]}.items(), key=lambda kv: kv[0])))
    return out


def key(p):
    return tuple(sorted(p.items(), key=lambda kv: kv[0]))


def tune(args):
    tf = args.timeframe
    syms = WATCH + (EXTRA if args.extras else [])
    full = load('coinbase', tf, syms)
    set_cutoff(full)
    parts = {'train': {b: split(d, 'train') for b, d in full.items()},
             'test': {b: split(d, 'test') for b, d in full.items()}}
    for b, d in full.items():
        tr, te = parts['train'][b], parts['test'][b]
        print(f"{b}: {len(d)} candles; train {pd.Timestamp(tr.timestamp.iloc[0], unit='ms').date()}.."
              f"{pd.Timestamp(tr.timestamp.iloc[-1], unit='ms').date()}, test "
              f"{pd.Timestamp(te.timestamp.iloc[0], unit='ms').date()}..{pd.Timestamp(te.timestamp.iloc[-1], unit='ms').date()}")
    min_trades = args.min_trades or 3 * len(full)

    # Stage 1: entry rule with the original exits
    entry_axes = {'RSI_OVERSOLD': [28, 30, 33, 36, 40, 45],
                  'TREND_FILTER': ['ema50', 'ema200', 'ema50_slope', 'none'],
                  'RSI_ENTRY_MODE': ['below', 'cross_up']}
    base_exit = {k: OLD_PARAMS[k] for k in PARAM_KEYS if k not in entry_axes}
    grid1 = [dict(base_exit, **dict(zip(entry_axes, v))) for v in itertools.product(*entry_axes.values())]
    t0 = time.time()
    res1 = run_grid(grid1, parts, tf, args.procs)
    print(f'stage 1: {len(grid1)} sets in {time.time() - t0:.0f}s')
    ranked1 = sorted(res1, key=lambda r: score(summarize(r[1]['train']), min_trades), reverse=True)
    print_table([(f"{p['RSI_ENTRY_MODE'][:5]} rsi{p['RSI_OVERSOLD']} {p['TREND_FILTER']}", 'train', summarize(o['train']))
                 for p, o in ranked1[:12]], 'stage 1 (entry rule, original exits) - top 12 by TRAIN score')

    # Stage 2: exits + cooldown for the best few entry rules
    exit_axes = {'TAKE_PROFIT_PCT': [0.02, 0.03, 0.04, 0.05, 0.07],
                 'TRAIL_PCT': [0.01, 0.02, 0.03],
                 'TRAIL_ACTIVATE_PCT': [0.015, 0.03],
                 'STOP_LOSS_PCT': [0.03, 0.05, 0.08, 0.12],
                 'MAX_HOLD_HOURS': [48, 72, 120, 240],
                 'STOPLOSS_COOLDOWN_HOURS': [0, 24, 72]}
    if args.max_stop:
        exit_axes['STOP_LOSS_PCT'] = [x for x in exit_axes['STOP_LOSS_PCT'] if x <= args.max_stop]
    if args.max_hold:
        exit_axes['MAX_HOLD_HOURS'] = [x for x in exit_axes['MAX_HOLD_HOURS'] if x <= args.max_hold]
    # top entry rules by TRAIN score; also include every RSI-threshold neighbour of them so
    # the neighbour smoothing below can see the threshold's sensitivity
    top_entries = []
    for p, _ in ranked1[:args.top_entries]:
        for thr in entry_axes['RSI_OVERSOLD']:
            if abs(entry_axes['RSI_OVERSOLD'].index(thr) - entry_axes['RSI_OVERSOLD'].index(p['RSI_OVERSOLD'])) <= 1:
                e = {'RSI_OVERSOLD': thr, 'TREND_FILTER': p['TREND_FILTER'], 'RSI_ENTRY_MODE': p['RSI_ENTRY_MODE']}
                if e not in top_entries:
                    top_entries.append(e)
    grid2 = [dict(e, **dict(zip(exit_axes, v))) for e in top_entries for v in itertools.product(*exit_axes.values())]
    t0 = time.time()
    res2 = run_grid(grid2, parts, tf, args.procs)
    print(f'stage 2: {len(grid2)} sets in {time.time() - t0:.0f}s')
    scores = {key(p): score(summarize(o['train']), min_trades) for p, o in res2}
    axes = dict(entry_axes, **exit_axes)

    def robust(p):   # average of own score and grid neighbours' scores (train only)
        nb = [scores[n] for n in neighbours(p, axes) if n in scores]
        vals = [scores[key(p)]] + nb
        return float(np.mean(vals)) if min(vals) > -1e8 else -1e9

    ranked2 = sorted(res2, key=lambda r: robust(r[0]), reverse=True)
    rows = []
    for p, o in ranked2[:10]:
        name = (f"{p['RSI_ENTRY_MODE'][:2]}{p['RSI_OVERSOLD']} {p['TREND_FILTER'][3:9]} tp{p['TAKE_PROFIT_PCT']} "
                f"tr{p['TRAIL_PCT']}/{p['TRAIL_ACTIVATE_PCT']} sl{p['STOP_LOSS_PCT']} h{p['MAX_HOLD_HOURS']} cd{p['STOPLOSS_COOLDOWN_HOURS']}")
        rows += [(name, 'train', summarize(o['train'])), ('', 'test', summarize(o['test']))]
    print_table(rows, 'stage 2 - top 10 by neighbour-smoothed TRAIN score (TEST shown for information only)')
    best = ranked2[0][0]
    print('\nchosen (by train robustness):', json.dumps(best))
    if args.out:
        with open(args.out, 'w') as f:
            json.dump({'best': best, 'top': [p for p, _ in ranked2[:20]],
                       'stage1': [(p, summarize(o['train']), summarize(o['test'])) for p, o in ranked1],
                       'stage2_top': [(p, robust(p), summarize(o['train']), summarize(o['test'])) for p, o in ranked2[:50]]},
                      f, indent=1, default=str)
    return best


def compare(args, new_params=None):
    """Out-of-sample comparison: original settings vs the shipped default vs presets
    (and the freshly tuned set when called from `tune`)."""
    tf = args.timeframe
    strip = lambda p: {k: v for k, v in p.items() if k in PARAM_KEYS}
    sets = [('old', OLD_PARAMS), ('default', current_params())]
    for name, p in app.STRATEGY_PRESETS.items():
        if p:
            sets.append((name, dict(OLD_PARAMS, **strip(p))))
    if new_params:
        sets.append(('tuned', dict(OLD_PARAMS, **strip(new_params))))
    baseline = current_params()
    set_cutoff(load('coinbase', tf, WATCH + EXTRA))
    for label, syms in (('watchlist', WATCH), ('watchlist+extras', WATCH + EXTRA)):
        full = load('coinbase', tf, syms)
        kr = load('kraken', tf, syms)
        rows, port = [], []
        for name, p in sets:
            apply_params(p)
            for part in ('train', 'test'):
                per = {b: app.backtest_on_dataframe(split(d, part), tf) for b, d in full.items()}
                rows.append((f'{name} [{label}]', part, summarize(per)))
            # Test period in two halves (regime stability)
            mid = CUTOFF_MS + (max(int(d['timestamp'].iloc[-1]) for d in full.values()) - CUTOFF_MS) // 2
            for half, lo, hi in (('test1', CUTOFF_MS, mid), ('test2', mid, 1 << 62)):
                per = {b: app.backtest_on_dataframe(
                    d[(d['timestamp'] >= lo) & (d['timestamp'] < hi)].reset_index(drop=True), tf)
                    for b, d in full.items()}
                rows.append((f'{name} [{label}]', half, summarize(per)))
            # Stress: every fill pays the taker fee (post-only entries/exits not achieved)
            maker = app.MAKER_FEE
            app.MAKER_FEE = app.TAKER_FEE
            per = {b: app.backtest_on_dataframe(split(d, 'test'), tf) for b, d in full.items()}
            rows.append((f'{name} [{label}] all-taker', 'test', summarize(per)))
            app.MAKER_FEE = maker
            per = {b: app.backtest_on_dataframe(d, tf) for b, d in kr.items()}
            rows.append((f'{name} [{label}]', 'kraken', summarize(per)))
            port.append((name, 'test', portfolio_sim({b: split(d, 'test') for b, d in full.items()}, tf)))
            port.append((name, 'kraken', portfolio_sim(kr, tf)))
            if args.verbose:
                for b, d in full.items():
                    r = app.backtest_on_dataframe(split(d, 'test'), tf)
                    print(f"   {name} {b} test: trades {r['trades']} win {r['win_rate']} ret {r['return_pct']} dd {r['max_drawdown_pct']}")
        print_table(rows, f'{tf}, {label}: per-symbol backtests (test/test1/test2/kraken are out-of-sample)')
        print(f'   live-like rotation portfolio, one position at a time ({label}):')
        for name, part, r in port:
            print(f'     {name:12s} {part:6s} trades {r["trades"]:4d} win {r["win_rate"]:5.1f}% '
                  f'return {r["return_pct"]:7.2f}% maxDD {r["max_dd"]:6.2f}%')
    apply_params(baseline)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['fetch', 'tune', 'compare'])
    ap.add_argument('--timeframe', default='4h')
    ap.add_argument('--extras', action='store_true', help='tune on watchlist + BTC/ETH/XRP/LINK/ADA')
    ap.add_argument('--procs', type=int, default=os.cpu_count() or 2)
    ap.add_argument('--top-entries', type=int, default=4)
    ap.add_argument('--min-trades', type=int, default=0)
    ap.add_argument('--out', default=None)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--max-stop', type=float, default=None, help='only consider STOP_LOSS_PCT <= this')
    ap.add_argument('--max-hold', type=int, default=None, help='only consider MAX_HOLD_HOURS <= this')
    args = ap.parse_args()
    if args.cmd == 'fetch':
        fetch()
    elif args.cmd == 'tune':
        best = tune(args)
        compare(args, best)
    else:
        compare(args)


if __name__ == '__main__':
    main()
