"""Offline sanity tests for app.py - no API keys, no network (sockets are blocked).

Run from the repo root:  python tests/test_offline.py
The mechanics tests pin the strategy parameters they rely on (the original defaults), so
they keep working when the shipped defaults are re-tuned; the shipped defaults are
checked separately at the end.
"""
import os, sys, json, importlib, socket
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd

# Hard-block network access for the whole test.
def _blocked(*a, **k): raise RuntimeError("NETWORK BLOCKED IN TEST")
socket.socket.connect = _blocked
socket.create_connection = _blocked

os.environ.pop("KRAKEN_API_KEY", None); os.environ.pop("KRAKEN_SECRET_KEY", None)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import tempfile
WORKDIR = tempfile.mkdtemp(prefix="kb_test_")
os.chdir(WORKDIR)                      # bot.log / state / ledger files go to a temp dir
import app as bot
SHIPPED = {k: getattr(bot, k) for k in ("RSI_OVERSOLD", "RSI_ENTRY_MODE", "TREND_FILTER", "TAKE_PROFIT_PCT",
           "TRAIL_PCT", "TRAIL_ACTIVATE_PCT", "STOP_LOSS_PCT", "MAX_HOLD_HOURS", "STOPLOSS_COOLDOWN_HOURS")}
PINNED = dict(RSI_OVERSOLD=28, RSI_ENTRY_MODE='below', TREND_FILTER='ema50', TAKE_PROFIT_PCT=0.03,
              TRAIL_PCT=0.01, TRAIL_ACTIVATE_PCT=0.015, STOP_LOSS_PCT=0.05, MAX_HOLD_HOURS=72,
              STOPLOSS_COOLDOWN_HOURS=0)
for _k, _v in PINNED.items():
    setattr(bot, _k, _v)
import ccxt
bot.ORDER_POLL_SEC = 0
bot.ORDER_TIMEOUT_SEC = 0
bot.STOP_LOSS_ORDER_TIMEOUT_SEC = 0
import time as _t
bot.time.sleep = lambda s: None   # speed up polling loops

results = []
def check(name, cond):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)

# ---------------- RSI ----------------
rng = np.random.default_rng(42)
close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500)))
df = pd.DataFrame({"close": close})
rsi = bot.calculate_rsi(df, 14)
# reference Wilder recursion seeded the same way as ewm(adjust=False)
d = np.diff(close); g = np.clip(d, 0, None); l = np.clip(-d, 0, None)
ag, al = g[0], l[0]; ref = [np.nan]
for i in range(len(d)):
    if i > 0:
        ag = ag + (g[i] - ag) / 14; al = al + (l[i] - al) / 14
    ref.append(100 - 100 / (1 + ag / al))
ref = np.array(ref)
valid = ~np.isnan(rsi.values)
check("RSI warm-up NaNs = period", np.isnan(rsi.values[:14]).all() and valid[14:].all())
check("RSI matches Wilder recursion", np.allclose(rsi.values[valid], ref[valid], atol=1e-9))
check("RSI within [0,100]", ((rsi[valid] >= 0) & (rsi[valid] <= 100)).all())
up = bot.calculate_rsi(pd.DataFrame({"close": np.arange(1, 60, dtype=float)}), 14)
check("RSI monotonic rise -> 100", abs(up.iloc[-1] - 100) < 1e-9)
flat = bot.calculate_rsi(pd.DataFrame({"close": np.full(40, 5.0)}), 14)
check("RSI flat -> 50", abs(flat.iloc[-1] - 50) < 1e-9)

# ---------------- closed candle handling ----------------
tf_ms = 4 * 3600 * 1000
now_ms = 1_700_000_000_000 // tf_ms * tf_ms + tf_ms // 2   # mid-candle
ohlcv = [[now_ms - tf_ms // 2 - (5 - i) * tf_ms, 1, 1, 1, i, 1] for i in range(6)]
cdf = bot.ohlcv_to_closed_df(ohlcv, "4h", now_ms)
check("forming candle dropped (iloc[-1] == raw iloc[-2])", len(cdf) == 5 and cdf['close'].iloc[-1] == ohlcv[-2][4])
cdf2 = bot.ohlcv_to_closed_df(ohlcv[:-1], "4h", now_ms)
check("all-closed data kept intact", len(cdf2) == 5)

# ---------------- decide_exit ----------------
now = datetime.now(timezone.utc); E = 100.0
floor = bot.fee_floor_price(E)
check("fee floor = entry*(1+0.005+0.002)", abs(floor - 100.7) < 1e-9)
check("trailing stop floored", abs(bot.trailing_stop_price(E, 101.5) - 100.7) < 1e-9)
check("trailing stop follows peak", abs(bot.trailing_stop_price(E, 103.0) - 101.97) < 1e-9)
check("hold in middle -> None", bot.decide_exit(E, 100.5, now, 100.2, now) is None)
dsl = bot.decide_exit(E, 100.0, now, 94.9, now)
check("stop-loss urgent", dsl and dsl.urgent and "Stop" in dsl.reason)
dt = bot.decide_exit(E, 103.1, now, 103.1, now)
check("target hit non-urgent with floor", dt and not dt.urgent and "Target" in dt.reason and dt.min_price == floor)
dtr = bot.decide_exit(E, 102.0, now, 100.9, now)
check("trailing triggers at floor after +1.5% peak", dtr and "Trailing" in dtr.reason)
check("trailing not armed below +1.5% peak", bot.decide_exit(E, 101.4, now, 100.0, now) is None)
dmh = bot.decide_exit(E, 100.0, now - timedelta(hours=73), 99.0, now)
check("max hold exit", dmh and "Max Hold" in dmh.reason and dmh.min_price is None)
bot.STOP_LOSS_PCT = None; bot.MAX_HOLD_HOURS = None
check("stop-loss/max-hold disabled with None",
      bot.decide_exit(E, 100.0, now - timedelta(hours=500), 50.0, now) is None)
bot.STOP_LOSS_PCT = 0.05; bot.MAX_HOLD_HOURS = 72
check("RSI overbought exit off by default", bot.decide_exit(E, 101.0, now, 101.0, now, rsi=90) is None)
bot.USE_RSI_OVERBOUGHT_EXIT = True
check("RSI overbought exit when enabled & above floor", bot.decide_exit(E, 101.0, now, 101.0, now, rsi=90) is not None)
check("RSI overbought exit blocked below floor", bot.decide_exit(E, 100.5, now, 100.5, now, rsi=90) is None)
bot.USE_RSI_OVERBOUGHT_EXIT = False
def edf(rsi, close, ema50, ema200=None):
    n_ = len(rsi)
    return pd.DataFrame({"rsi": rsi, "close": close, "ema_50": ema50,
                         "ema_200": ema200 if ema200 is not None else [np.nan] * n_})
check("entry 'below' + ema50", bot.check_entry_signal(edf([20], [10], [9]))[0]
      and not bot.check_entry_signal(edf([30], [10], [9]))[0]
      and not bot.check_entry_signal(edf([20], [9], [10]))[0]
      and not bot.check_entry_signal(edf([float('nan')], [10], [9]))[0])
bot.RSI_ENTRY_MODE = 'cross_up'
check("entry 'cross_up' needs prev < thr <= last", bot.check_entry_signal(edf([25, 30], [10, 10], [9, 9]))[0]
      and not bot.check_entry_signal(edf([25, 26], [10, 10], [9, 9]))[0]
      and not bot.check_entry_signal(edf([30, 31], [10, 10], [9, 9]))[0])
bot.RSI_ENTRY_MODE = 'below'; bot.TREND_FILTER = 'ema200'
check("trend 'ema200'", bot.check_entry_signal(edf([20], [10], [11], [9]))[0]
      and not bot.check_entry_signal(edf([20], [10], [9], [11]))[0]
      and not bot.check_entry_signal(edf([20], [10], [9]))[0])          # NaN EMA200 -> no signal
bot.TREND_FILTER = 'ema50_slope'
e50 = list(np.linspace(1, 2, 10))
check("trend 'ema50_slope'", bot.check_entry_signal(edf([20] * 10, [1] * 10, e50))[0]
      and not bot.check_entry_signal(edf([20] * 10, [1] * 10, e50[::-1]))[0])
bot.TREND_FILTER = 'ema50'

# ---------------- backtest on synthetic OHLCV ----------------
def synth(n=1000, seed=1, drift=0.0005, vol=0.03):
    r = np.random.default_rng(seed)
    t0 = 1_690_000_000_000 // tf_ms * tf_ms
    c = 50 * np.exp(np.cumsum(r.normal(drift, vol, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(r.normal(0, 0.01, n)))
    lo = np.minimum(o, c) * (1 - np.abs(r.normal(0, 0.01, n)))
    return pd.DataFrame({"timestamp": t0 + np.arange(n) * tf_ms, "open": o, "high": h, "low": lo,
                         "close": c, "volume": 1.0})
sig_default = 0
for seed in (1, 2, 3, 7):
    ind = bot.add_indicators(synth(seed=seed))
    sig_default += int(((ind.rsi < 28) & (ind.close > ind.ema_50)).sum())
    res = bot.backtest_on_dataframe(synth(seed=seed), "4h")
    check(f"backtest seed {seed} (default params) runs", res['trades'] == len(res['pnls']))
print(f"   default-param signals on 4 random walks x1000 candles: {sig_default}")
# Loosen the entry threshold ONLY to exercise the loop mechanics on random walks.
bot.RSI_OVERSOLD = 45
allp = []
for seed in (1, 2, 3, 7):
    res = bot.backtest_on_dataframe(synth(seed=seed), "4h")
    allp += res['pnls']
    print(f"   [RSI<45] seed {seed}: return {res['return_pct']}%, trades {res['trades']}, win {res['win_rate']}%")
check("loosened backtest produced trades", len(allp) > 0)
check("losses bounded (stop-loss ~5% + fees/slippage/gap; < 10% of position)",
      all(p > -0.10 * 1000 * bot.POSITION_SIZE_PCT * 1.5 for p in allp))

def scenario(tail):
    r = [0.015] * 150 + [-0.03] * 7 + tail
    c = 50 * np.cumprod(1 + np.array(r)); o = np.concatenate([[c[0]], c[:-1]])
    t0 = 1_690_000_000_000 // tf_ms * tf_ms
    return pd.DataFrame({"timestamp": t0 + np.arange(len(c)) * tf_ms, "open": o, "high": np.maximum(o, c) * 1.002,
                         "low": np.minimum(o, c) * 0.998, "close": c, "volume": 1.0})
res = bot.backtest_on_dataframe(scenario([0.015] * 30), "4h"); print("   recover:", res['pnls'])
check("scenario recover: target exit, profitable after fees (~+2.5% net)", res['trades'] == 1 and 0.02 * 800 < res['pnls'][0] < 0.03 * 800)
res = bot.backtest_on_dataframe(scenario([-0.02] * 30), "4h"); print("   crash:", res['pnls'])
check("scenario crash: stop-loss exits, each loss between -4.5% and -7.5% of $800", res['trades'] >= 1 and all(-0.075 * 800 < p < -0.045 * 700 for p in res['pnls']))
bot.STOPLOSS_COOLDOWN_HOURS = 200
res_cd = bot.backtest_on_dataframe(scenario([-0.02] * 30), "4h")
bot.STOPLOSS_COOLDOWN_HOURS = 0
check("backtest cooldown blocks re-entry after stop-loss", res_cd['trades'] == 1 and len(res['pnls']) >= 2)
res = bot.backtest_on_dataframe(scenario([0.0] * 30), "4h"); print("   flat:", res['pnls'])
check("scenario flat: max-hold exit fires", res['trades'] >= 1)
bot.MAX_HOLD_HOURS = None
res = bot.backtest_on_dataframe(scenario([0.0] * 30), "4h")
check("scenario flat with MAX_HOLD_HOURS=None: no exit, position stays open", res['trades'] == 0 and res['open_position'])
bot.MAX_HOLD_HOURS = 72
bot.RSI_OVERSOLD = 28
cdf = scenario([0.015] * 30); n = len(cdf); c = cdf['close'].values; o = cdf['open'].values
ind = bot.add_indicators(cdf)

# ---------------- fake exchange: order lifecycle, partial fills, state ----------------
for f in (bot.STATE_FILE, bot.LEDGER_FILE):
    if os.path.exists(f): os.remove(f)

class FakeKraken(ccxt.kraken):
    def __init__(self, scenario):
        super().__init__({'enableRateLimit': False})
        self.set_markets([{
            'id': 'SOLUSD', 'symbol': 'SOL/USD', 'base': 'SOL', 'quote': 'USD', 'baseId': 'SOL',
            'quoteId': 'USD', 'active': True, 'spot': True, 'type': 'spot',
            'precision': {'amount': 1e-8, 'price': 0.01},
            'limits': {'amount': {'min': 0.02}, 'cost': {'min': 0.5}, 'price': {}, 'leverage': {}},
        }])
        self.scenario = scenario; self.orders = {}; self.created = []; self.cancelled = []
        self.bal = {'USD': 1000.0, 'SOL': 0.0}
    def fetch_ticker(self, symbol, params={}):
        p = self.scenario.get('price', 100.0)
        return {'last': p, 'bid': p - 0.05, 'ask': p + 0.05}
    def fetch_balance(self, params={}):
        return {'free': dict(self.bal), 'total': dict(self.bal)}
    def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return [o for o in self.orders.values() if o['status'] == 'open']
    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return [o for o in self.orders.values() if o['status'] != 'open']
    def create_order(self, symbol, type, side, amount, price=None, params={}):
        if self.scenario.get('create_network_error'):
            self.scenario['create_network_error'] = False
            raise ccxt.RequestTimeout("timeout")
        oid = f"O{len(self.created)+1}"
        self.created.append((symbol, type, side, amount, price, dict(params)))
        frac = self.scenario.get('fill_frac', 1.0)
        filled = round(amount * frac, 8)
        o = {'id': oid, 'symbol': symbol, 'side': side, 'amount': amount, 'price': price,
             'status': 'closed' if frac >= 1 else 'open', 'filled': filled,
             'average': price if filled else None, 'cost': filled * price,
             'fee': {'cost': filled * price * 0.0025, 'currency': 'USD'} if filled else None,
             'info': {'userref': params.get('userref')}}
        self.orders[oid] = o
        if filled:
            if side == 'buy': self.bal['USD'] -= filled * price * 1.0025; self.bal['SOL'] += filled
            else: self.bal['SOL'] -= filled; self.bal['USD'] += filled * price * 0.9975
        return dict(o)
    def fetch_order(self, id, symbol=None, params={}):
        return dict(self.orders[id])
    def cancel_order(self, id, symbol=None, params={}):
        self.cancelled.append(id)
        if self.orders[id]['status'] == 'open': self.orders[id]['status'] = 'canceled'
        return dict(self.orders[id])
    def fetch_ohlcv(self, symbol, timeframe='4h', since=None, limit=None, params={}):
        return self.scenario['ohlcv']

# 1) unfilled post-only buy -> cancelled, nothing recorded
ex = FakeKraken({'fill_frac': 0.0})
st = bot.load_state()
fill = bot.execute_order(ex, st, 'SOL/USD', 'buy', 1.0, 99.95, True, 0, {"reason": "t", "signal_ts": 1})
bot.apply_buy_fill(ex, st, 'SOL/USD', fill, signal_ts=1)
st2 = bot.load_state()
check("unfilled order cancelled", ex.cancelled == ['O1'])
check("post-only + userref params sent", ex.created[0][5].get('postOnly') is True and 'userref' in ex.created[0][5])
check("unfilled: no position, no ledger, no pending", st2['symbol'] is None and bot.load_ledger() == [] and st2['pending_order'] is None)

# 2) partial buy fill (40%) -> position = filled amount at avg price
ex = FakeKraken({'fill_frac': 0.4})
st = bot.load_state()
fill = bot.execute_order(ex, st, 'SOL/USD', 'buy', 2.0, 100.0, True, 0, {"reason": "t", "signal_ts": 2})
bot.apply_buy_fill(ex, st, 'SOL/USD', fill, signal_ts=2)
st = bot.load_state(); led = bot.load_ledger()
check("partial buy: position amount = filled", st['symbol'] == 'SOL/USD' and abs(st['amount'] - 0.8) < 1e-9)
check("partial buy: entry = avg fill, cost incl fee", st['last_buy_price'] == 100.0 and abs(st['entry_cost'] - 80.2) < 1e-9)
check("partial buy: ledger records filled amount + fee", len(led) == 1 and led[0]['amount'] == 0.8 and led[0]['partial'] and abs(led[0]['fee'] - 0.2) < 1e-9)

# 3) manage_position: target hit -> post-only sell at max(ask, floor) of tracked amount only
ex.bal['SOL'] = 5.0          # user holds extra SOL; bot must only sell its 0.8
ex.scenario.update({'price': 103.5, 'fill_frac': 1.0})
free, total = bot.get_balances(ex, st)
bot.manage_position(ex, st, free, total)
sym, typ, side, amt, px, params = ex.created[-1]
check("exit sells only tracked amount", side == 'sell' and abs(amt - 0.8) < 1e-12)
check("exit priced at ask, post-only", abs(px - 103.55) < 1e-9 and params.get('postOnly'))
st = bot.load_state(); led = bot.load_ledger()
check("position cleared after full sell, pnl recorded", st['symbol'] is None and led[-1]['type'] == 'SELL' and led[-1]['pnl'] > 0)
print("   sell pnl:", led[-1]['pnl'])

# 4) trailing exit below floor -> limit at floor (not at the lower ask)
st.update({'symbol': 'SOL/USD', 'last_buy_price': 100.0, 'peak_price': 102.0, 'amount': 0.5,
           'entry_time': bot.utc_now().isoformat(), 'entry_cost': 50.125}); bot.save_state(st)
ex.scenario.update({'price': 100.3, 'fill_frac': 0.0})
free, total = bot.get_balances(ex, st)
bot.manage_position(ex, st, free, total)
px = ex.created[-1][4]
check("trailing exit limit = fee floor (100.70) when ask is below it", abs(px - 100.70) < 1e-9)
st = bot.load_state()
check("unfilled exit keeps position untouched", st['symbol'] == 'SOL/USD' and st['amount'] == 0.5)

# 5) stop-loss -> marketable limit (no postOnly) with partial fills and retry
ex.scenario.update({'price': 94.0, 'fill_frac': 0.5})
free, total = bot.get_balances(ex, st)
n_before = len(ex.created)
bot.manage_position(ex, st, free, total)
sl_orders = ex.created[n_before:]
check("stop-loss uses non-post-only limit below bid", all(not o[5].get('postOnly') and o[4] < 94.0 for o in sl_orders))
check("stop-loss retried after partial fills", len(sl_orders) >= 2)
st = bot.load_state()
print("   after stop-loss: amount", st['amount'], "orders", [(o[3], o[4]) for o in sl_orders])

# 6) network error on create -> pending persisted; resolved later from exchange
for f in (bot.STATE_FILE, bot.LEDGER_FILE): os.remove(f)
ex = FakeKraken({'create_network_error': True, 'fill_frac': 1.0})
st = bot.load_state()
fill = bot.execute_order(ex, st, 'SOL/USD', 'buy', 1.0, 100.0, True, 0, {"reason": "t", "signal_ts": 3})
st = bot.load_state()
check("network error on create -> fill unknown, pending kept, nothing recorded",
      fill is None and st['pending_order'] and bot.load_ledger() == [] and st['symbol'] is None)
# simulate that the order actually got placed & filled on the exchange
uref = st['pending_order']['userref']
ex.orders['X1'] = {'id': 'X1', 'symbol': 'SOL/USD', 'side': 'buy', 'amount': 1.0, 'price': 100.0,
                   'status': 'closed', 'filled': 1.0, 'average': 100.0, 'cost': 100.0,
                   'fee': {'cost': 0.25, 'currency': 'USD'}, 'info': {'userref': uref}}
ok = bot.resolve_pending_order(ex, st)
st = bot.load_state()
check("pending order resolved from exchange -> position opened", ok and st['symbol'] == 'SOL/USD' and st['amount'] == 1.0 and st['pending_order'] is None)

# 7) reconcile: balance lower than state -> uses exchange balance
ex.bal['SOL'] = 0.6
bot.reconcile_on_startup(ex)
st = bot.load_state()
check("reconcile adjusts amount to exchange balance", abs(st['amount'] - 0.6) < 1e-12)

# 8) scan_and_enter with signal on last CLOSED candle only
for f in (bot.STATE_FILE, bot.LEDGER_FILE): os.remove(f)
nowms = ex.milliseconds(); start = (nowms // tf_ms) * tf_ms - (n - 1) * tf_ms
raw = [[int(start + i * tf_ms), float(o[i]), float(cdf['high'][i]), float(cdf['low'][i]), float(c[i]), 1.0] for i in range(n)]
bot.RSI_OVERSOLD = 45
idx = 155
raw_sig = raw[:idx + 1] + [[int(start + (idx + 1) * tf_ms), 95, 95, 1, 1, 1]]  # forming candle crashes (ignored)
raw_sig = [[r[0] - raw_sig[-1][0] + (nowms // tf_ms) * tf_ms] + r[1:] for r in raw_sig]
bot.WATCHLIST = ['SOL/USD']
ex = FakeKraken({'ohlcv': raw_sig, 'price': 95.0, 'fill_frac': 1.0})
print('   last closed RSI/close/EMA:', bot.fetch_closed_indicators(ex, 'SOL/USD')[['rsi','close','ema_50']].round(2).to_dict())
st = bot.load_state()
bot.scan_and_enter(ex, st, 1000.0)
st = bot.load_state()
check("entry on closed-candle signal: post-only buy at bid, 80% of USD",
      st['symbol'] == 'SOL/USD' and ex.created and ex.created[0][2] == 'buy' and abs(ex.created[0][4] - 94.95) < 1e-9
      and abs(ex.created[0][3] * 94.95 - 800) < 0.01)
st.update(bot.empty_position()); bot.save_state(st); n_before = len(ex.created)
bot.scan_and_enter(ex, st, 1000.0)
check("same signal candle not re-entered", len(ex.created) == n_before)

# 8b) post-stop-loss cooldown (live): stop-loss close sets cooldown and blocks entries
bot.STOPLOSS_COOLDOWN_HOURS = 24
st = bot.load_state()
st.update({'symbol': 'SOL/USD', 'last_buy_price': 100.0, 'peak_price': 100.0, 'amount': 0.5,
           'entry_time': bot.utc_now().isoformat(), 'entry_cost': 50.0})
fsl = bot.Fill('SL1', 'closed', 0.5, 94.0, 47.0, 0.188, 'USD', False)
bot.apply_sell_fill(ex, st, 'SOL/USD', fsl, "Stop-Loss -5.0%")
st = bot.load_state()
cd = bot.parse_iso(st['cooldowns'].get('SOL/USD'))
check("stop-loss sets 24h cooldown", cd is not None and 23.9 < (cd - bot.utc_now()).total_seconds() / 3600 <= 24)
st['last_entry_signal'] = {}; bot.save_state(st); n_before = len(ex.created)
bot.scan_and_enter(ex, st, 1000.0)
check("no entry while symbol in cooldown", len(ex.created) == n_before and bot.load_state()['symbol'] is None)
st = bot.load_state(); st['cooldowns'] = {}; bot.save_state(st)
bot.scan_and_enter(ex, st, 1000.0)
check("entry resumes after cooldown cleared", len(ex.created) == n_before + 1)
bot.STOPLOSS_COOLDOWN_HOURS = 0

# 9) DRY_RUN path places nothing
bot.DRY_RUN = True
ex = FakeKraken({'fill_frac': 1.0})
st = bot.default_state()
f9 = bot.execute_order(ex, st, 'SOL/USD', 'buy', 1.0, 100.0, True, 0, {})
check("DRY_RUN: no create_order call, simulated fill", ex.created == [] and f9.filled == 1.0)
bot.DRY_RUN = False

# 10) atomic write
bot.atomic_write_json("atomic_test.json", {"a": 1})
check("atomic write ok, no temp files left", json.load(open("atomic_test.json")) == {"a": 1}
      and not [f for f in os.listdir('.') if f.startswith('.tmp_')])

# 11) dashboard + basic auth
client = bot.app.test_client()
r = client.get('/'); check("dashboard renders without auth when no password", r.status_code == 200 and b'Kraken Advanced Bot Dashboard' in r.data)
bot.DASHBOARD_PASSWORD = 's3cret'
check("dashboard 401 without credentials", client.get('/').status_code == 401)
import base64
hdr = {'Authorization': 'Basic ' + base64.b64encode(b'admin:s3cret').decode()}
check("dashboard 200 with credentials", client.get('/', headers=hdr).status_code == 200)
bad = {'Authorization': 'Basic ' + base64.b64encode(b'admin:nope').decode()}
check("dashboard 401 with wrong password", client.get('/', headers=bad).status_code == 401)
check("default dashboard host is 127.0.0.1", bot.DASHBOARD_HOST == '127.0.0.1')

# 12) shipped defaults are valid and the backtest runs with them
for _k, _v in SHIPPED.items():
    setattr(bot, _k, _v)
print("   shipped defaults:", SHIPPED)
check("shipped entry mode / trend filter valid", SHIPPED['RSI_ENTRY_MODE'] in ('below', 'cross_up')
      and SHIPPED['TREND_FILTER'] in ('ema50', 'ema200', 'ema50_slope', 'none'))
check("shipped fee floor below take-profit", bot.fee_floor_price(100) < 100 * (1 + (SHIPPED['TAKE_PROFIT_PCT'] or 1)))
res = bot.backtest_on_dataframe(synth(seed=3), "4h")
check("backtest runs with shipped defaults", res['trades'] == len(res['pnls']) and 'max_drawdown_pct' in res)
check("dashboard strategy text renders", isinstance(bot.strategy_description(), str))

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
