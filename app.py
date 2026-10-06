import os
import time
import json
import hmac
import random
import tempfile
import threading
import logging
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import ccxt
import pandas as pd
from flask import Flask, Response, request, render_template_string
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup Logging to both a file (bot.log) and the console
logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger('').addHandler(console_handler)


def _env_flag(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# =============================================================================
# Configuration
# =============================================================================
# --- Mode -------------------------------------------------------------------
# DRY_RUN=true  -> paper trading: orders are logged, not sent. Fills are simulated
#                  at the limit price against a paper wallet (DRY_RUN_USD_BALANCE),
#                  and state/ledger go to separate *.dryrun.json files.
#                  No API keys are required in this mode.
# DRY_RUN unset/false -> LIVE trading (same as the original bot).
DRY_RUN = _env_flag("DRY_RUN", False)
DRY_RUN_USD_BALANCE = float(os.getenv("DRY_RUN_USD_BALANCE", "1000"))

# --- Watchlist & signals (defaults unchanged from the original bot) -----------
WATCHLIST = ['SOL/USD', 'AVAX/USD', 'DOGE/USD', 'NEAR/USD', 'SUI/USD']
TIMEFRAME = '4h'
RSI_PERIOD = 14
EMA_PERIOD = 50
RSI_OVERSOLD = 28                  # entry RSI threshold (see RSI_ENTRY_MODE)
RSI_ENTRY_MODE = 'below'           # 'below':    RSI(last closed candle) < RSI_OVERSOLD
#                                    'cross_up': RSI crosses back up through RSI_OVERSOLD
#                                                (prev candle < threshold <= last candle)
TREND_FILTER = 'ema50'             # 'ema50':       close > EMA50
#                                    'ema200':      close > EMA200
#                                    'ema50_slope': EMA50 higher than EMA_SLOPE_LOOKBACK candles ago
#                                    'none':        no trend filter
EMA_LONG_PERIOD = 200
EMA_SLOPE_LOOKBACK = 6
STOPLOSS_COOLDOWN_HOURS = 0        # after a stop-loss exit, no new entry on that symbol for
#                                    this many hours (0 or None = no cooldown)
RSI_OVERBOUGHT = 68
USE_RSI_OVERBOUGHT_EXIT = False    # optional extra exit (off by default). When on, it
#                                    only fires if price is above the fee floor.
CHECK_INTERVAL_SEC = 900
OHLCV_LIMIT = 720                  # candles fetched per symbol for live signals (Kraken max;
#                                    EMA200 needs the warm-up)
POSITION_SIZE_PCT = 0.80           # fraction of free USD used per entry
MIN_TRADE_USD = 10.0               # don't open positions smaller than this

# --- Fees (Kraken Pro starting tier; change if your tier differs) ------------
MAKER_FEE = 0.0025
TAKER_FEE = 0.0040
# Entries and profit-taking exits are post-only (maker) orders, so a normal round
# trip pays maker fees on both legs. Set to MAKER_FEE + TAKER_FEE to be stricter.
ROUND_TRIP_FEE = 2 * MAKER_FEE
MIN_PROFIT_MARGIN = 0.002          # required net profit above fees for the trailing floor

# --- Exits -------------------------------------------------------------------
TAKE_PROFIT_PCT = 0.03             # +3% target (original behaviour)
TRAIL_ACTIVATE_PCT = 0.015         # trailing stop arms once the peak is >= entry +1.5%
TRAIL_PCT = 0.01                   # trail 1% below the peak ...
#   ... but never below the fee floor: entry * (1 + ROUND_TRIP_FEE + MIN_PROFIT_MARGIN)
STOP_LOSS_PCT = 0.05               # exit if price <= entry * (1 - 5%).  None = disabled
MAX_HOLD_HOURS = 72                # exit after this many hours in a trade. None = disabled
# NOTE: STOP_LOSS_PCT is enforced by this bot's polling loop (every CHECK_INTERVAL_SEC),
# so it does not protect you if the PC/bot is offline or during a fast crash between
# checks. A further option is an exchange-side stop order (Kraken 'stop-loss' order
# type) placed right after each buy, so the exchange enforces it even when the bot
# is down. That is not implemented here because it reserves the balance and needs
# its own cancel/replace handling on every exit.
STOP_LOSS_MAX_SLIPPAGE_PCT = 0.01  # stop-loss uses a marketable limit at bid*(1-this)
STOP_LOSS_RETRIES = 3              # re-price & retry the stop-loss exit this many times

# --- Order handling ------------------------------------------------------------
ORDER_TIMEOUT_SEC = 300            # cancel unfilled (post-only) orders after this long
STOP_LOSS_ORDER_TIMEOUT_SEC = 60   # shorter wait for urgent exits
ORDER_POLL_SEC = 5                 # fetch_order polling interval
PENDING_ORDER_GIVE_UP_SEC = max(2 * ORDER_TIMEOUT_SEC, 900)  # unknown-order resolution
CANCEL_STRAY_ORDERS_ON_START = False  # cancel open watchlist orders not created by this bot
POSITION_TOLERANCE_PCT = 0.01      # balance-vs-state mismatch tolerance at reconcile

# --- Backtest / scanner ----------------------------------------------------------
BACKTEST_DAYS = 180                # NOTE: Kraken's OHLC API only serves the latest 720
#                                    candles (~120 days on 4h); the dashboard shows the
#                                    actual number of days covered.
BACKTEST_START_CASH = 1000.0
BACKTEST_TAKER_SLIPPAGE_PCT = 0.001  # extra slippage assumed on stop-loss (taker) exits
SCANNER_REFRESH_SEC = 3600

# --- Files -------------------------------------------------------------------------
LEDGER_FILE = 'trade_ledger.dryrun.json' if DRY_RUN else 'trade_ledger.json'
STATE_FILE = 'bot_state.dryrun.json' if DRY_RUN else 'bot_state.json'

# --- Dashboard -------------------------------------------------------------------------
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")   # use 0.0.0.0 for LAN access
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")        # unset = no auth

app = Flask(__name__)

bot_status = {
    "last_check": "Initializing...",
    "active_symbol": "None",
    "price": 0.0,
    "rsi": 0.0,
    "ema_50": 0.0,
    "usd_balance": 0.0,
    "asset_balance": 0.0,
    "last_action": "Scanning watchlist...",
    "timestamps": [],
    "prices": [],
    "rsis": []
}

scanner_cache = {
    "last_updated": "Never",
    "days": BACKTEST_DAYS,
    "results": []
}


# =============================================================================
# Time helpers (always timezone-aware UTC)
# =============================================================================
def utc_now():
    return datetime.now(timezone.utc)


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# =============================================================================
# Exchange (created once, reused)
# =============================================================================
_exchange = None
_public_exchange = None
_exchange_init_lock = threading.Lock()


def get_exchange():
    """Trading exchange instance (with API keys). Created once; markets loaded once."""
    global _exchange
    with _exchange_init_lock:
        if _exchange is None:
            _exchange = ccxt.kraken({
                'apiKey': os.getenv("KRAKEN_API_KEY"),
                'secret': os.getenv("KRAKEN_SECRET_KEY"),
                'enableRateLimit': True
            })
        return _exchange


def get_public_exchange():
    """Separate key-less instance for the scanner thread (public data only), so the
    scanner never shares a ccxt object / nonce with the trading thread."""
    global _public_exchange
    with _exchange_init_lock:
        if _public_exchange is None:
            _public_exchange = ccxt.kraken({'enableRateLimit': True})
        return _public_exchange


def ensure_markets(exchange):
    """Load markets once (ccxt caches them on the instance)."""
    if not exchange.markets:
        exchange.load_markets()
    return exchange.markets


# =============================================================================
# Indicators & shared strategy logic (used by BOTH the live loop and backtest)
# =============================================================================
def calculate_rsi(data, period=14):
    """RSI with Wilder's smoothing (EMA with alpha = 1/period)."""
    delta = data['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # No losses in the window -> RSI 100 (avoid NaN from division by zero);
    # completely flat window -> 50.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi


def add_indicators(df):
    df = df.copy()
    df['rsi'] = calculate_rsi(df, period=RSI_PERIOD)
    df['ema_50'] = df['close'].ewm(span=EMA_PERIOD, min_periods=EMA_PERIOD).mean()
    df['ema_200'] = df['close'].ewm(span=EMA_LONG_PERIOD, min_periods=EMA_LONG_PERIOD).mean()
    return df


def ohlcv_to_closed_df(ohlcv, timeframe, now_ms=None):
    """Build a DataFrame of CLOSED candles only.

    Kraken returns the still-forming candle as the last row; signals must use the
    last closed candle (i.e. what used to be iloc[-2]). We drop the last row when
    its period hasn't ended yet, so df.iloc[-1] is always the last closed candle.
    """
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    if df.empty:
        return df
    tf_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    if now_ms is None:
        now_ms = int(utc_now().timestamp() * 1000)
    if df['timestamp'].iloc[-1] + tf_ms > now_ms:
        df = df.iloc[:-1]
    return df.reset_index(drop=True)


def entry_signals(df):
    """Entry rule, shared by the live loop and the backtest.

    df must contain CLOSED candles with indicators (add_indicators). Returns
    (signal: bool Series, score: float Series); row i is the decision made once
    candle i has closed. Higher score = more oversold (used to pick between symbols).
    """
    rsi, close = df['rsi'], df['close']
    if RSI_ENTRY_MODE == 'cross_up':
        prev_rsi = rsi.shift(1)
        rsi_ok = (prev_rsi < RSI_OVERSOLD) & (rsi >= RSI_OVERSOLD)
        score = RSI_OVERSOLD - prev_rsi
    elif RSI_ENTRY_MODE == 'below':
        rsi_ok = rsi < RSI_OVERSOLD
        score = RSI_OVERSOLD - rsi
    else:
        raise ValueError(f"Unknown RSI_ENTRY_MODE {RSI_ENTRY_MODE!r}")

    if TREND_FILTER == 'ema50':
        trend_ok = close > df['ema_50']
    elif TREND_FILTER == 'ema200':
        trend_ok = close > df['ema_200']
    elif TREND_FILTER == 'ema50_slope':
        trend_ok = df['ema_50'] > df['ema_50'].shift(EMA_SLOPE_LOOKBACK)
    elif TREND_FILTER == 'none':
        trend_ok = df['ema_50'].notna()
    else:
        raise ValueError(f"Unknown TREND_FILTER {TREND_FILTER!r}")
    signal = (rsi_ok & trend_ok).fillna(False).astype(bool)   # NaN warm-up -> no signal
    return signal, score.fillna(0.0)


def check_entry_signal(df):
    """Convenience wrapper: (signal, score) for the last CLOSED candle of df."""
    if df is None or len(df) == 0:
        return False, 0.0
    signal, score = entry_signals(df)
    return bool(signal.iloc[-1]), float(score.iloc[-1])


def strategy_description():
    rsi_txt = (f"RSI crosses back above {RSI_OVERSOLD}" if RSI_ENTRY_MODE == 'cross_up'
               else f"RSI < {RSI_OVERSOLD}")
    trend_txt = {'ema50': 'close > EMA50', 'ema200': f'close > EMA{EMA_LONG_PERIOD}',
                 'ema50_slope': 'EMA50 rising', 'none': 'no trend filter'}.get(TREND_FILTER, TREND_FILTER)
    parts = [f"entry {rsi_txt} + {trend_txt}"]
    if TAKE_PROFIT_PCT is not None:
        parts.append(f"+{TAKE_PROFIT_PCT * 100:g}% target")
    if TRAIL_PCT is not None:
        parts.append(f"{TRAIL_PCT * 100:g}% fee-floored trailing stop (armed at +{TRAIL_ACTIVATE_PCT * 100:g}%)")
    if STOP_LOSS_PCT is not None:
        parts.append(f"-{STOP_LOSS_PCT * 100:g}% stop-loss")
    if MAX_HOLD_HOURS is not None:
        parts.append(f"{MAX_HOLD_HOURS}h max hold")
    if STOPLOSS_COOLDOWN_HOURS:
        parts.append(f"{STOPLOSS_COOLDOWN_HOURS}h post-stop cooldown")
    return ", ".join(parts)


def fee_floor_price(entry_price):
    """Lowest exit price that is still profitable after round-trip fees + margin."""
    return entry_price * (1 + ROUND_TRIP_FEE + MIN_PROFIT_MARGIN)


def trailing_stop_price(entry_price, peak_price):
    return max(peak_price * (1 - TRAIL_PCT), fee_floor_price(entry_price))


ExitDecision = namedtuple('ExitDecision', ['reason', 'urgent', 'min_price'])
# reason:    human readable reason
# urgent:    True -> get out now (marketable limit, taker fee acceptable)
# min_price: lowest acceptable sell price for non-urgent exits (None = any price)


def decide_exit(entry_price, peak_price, entry_time, price, now, rsi=None):
    """Single exit-decision function shared by the live loop and the backtest.

    entry_price: average fill price of the position
    peak_price:  highest observed price since entry (caller keeps it updated)
    entry_time:  aware datetime of the entry fill (None = unknown)
    price:       current price
    now:         aware datetime
    rsi:         RSI of the last CLOSED candle (only used by the optional RSI exit)
    """
    if not entry_price or entry_price <= 0 or price is None:
        return None
    peak = max(peak_price or 0.0, price)
    floor = fee_floor_price(entry_price)

    # 1) Hard stop-loss (downside protection, highest priority)
    if STOP_LOSS_PCT is not None and price <= entry_price * (1 - STOP_LOSS_PCT):
        return ExitDecision(f"Stop-Loss -{STOP_LOSS_PCT * 100:.1f}%", True, None)

    # 2) Take-profit target
    if TAKE_PROFIT_PCT is not None and price >= entry_price * (1 + TAKE_PROFIT_PCT):
        return ExitDecision(f"{TAKE_PROFIT_PCT * 100:.0f}% Target Hit", False, floor)

    # 3) Fee-floored trailing stop (armed once the peak reached entry + TRAIL_ACTIVATE_PCT)
    if TRAIL_PCT is not None and peak >= entry_price * (1 + TRAIL_ACTIVATE_PCT):
        if price <= trailing_stop_price(entry_price, peak):
            return ExitDecision("Trailing Stop Triggered", False, floor)

    # 4) Optional RSI overbought exit (only when profitable after fees)
    if USE_RSI_OVERBOUGHT_EXIT and rsi is not None and not pd.isna(rsi) \
            and rsi > RSI_OVERBOUGHT and price >= floor:
        return ExitDecision(f"RSI Overbought ({rsi:.1f})", False, floor)

    # 5) Max holding time (time stop; may realise a small loss, maker order at ask)
    if MAX_HOLD_HOURS is not None and entry_time is not None \
            and now - entry_time >= timedelta(hours=MAX_HOLD_HOURS):
        return ExitDecision(f"Max Hold {MAX_HOLD_HOURS}h Reached", False, None)

    return None


# =============================================================================
# State & ledger (atomic JSON writes)
# =============================================================================
def empty_position():
    return {
        "symbol": None,
        "last_buy_price": 0.0,   # average entry fill price
        "peak_price": 0.0,
        "amount": 0.0,           # base amount held by this position (from fills)
        "entry_time": None,      # ISO-8601 UTC
        "entry_cost": 0.0,       # quote spent incl. quote-denominated fees
    }


def default_state():
    state = empty_position()
    state["pending_order"] = None
    state["last_entry_signal"] = {}   # symbol -> signal candle timestamp already traded
    state["cooldowns"] = {}           # symbol -> ISO time until which new entries are blocked
    if DRY_RUN:
        state["paper"] = {"USD": DRY_RUN_USD_BALANCE}
    return state


def atomic_write_json(path, data):
    """Write JSON to a temp file in the same directory, fsync, then os.replace()."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp_', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        # On Windows os.replace can briefly fail if another thread has the file open.
        for attempt in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_state():
    state = default_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, 'r') as f:
            loaded = json.load(f)
        state.update(loaded or {})
        if state.get("last_entry_signal") is None:
            state["last_entry_signal"] = {}
        if state.get("cooldowns") is None:
            state["cooldowns"] = {}
        if DRY_RUN and not state.get("paper"):
            state["paper"] = {"USD": DRY_RUN_USD_BALANCE}
        return state
    except Exception as e:
        logging.error(f"Error loading state file: {e}")
        # Don't silently forget an open position: keep the corrupt file for inspection.
        raise


def save_state(state):
    try:
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        logging.error(f"Error saving state file: {e}")
        raise


def load_ledger():
    if not os.path.exists(LEDGER_FILE):
        return []
    try:
        with open(LEDGER_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading trade ledger: {e}")
        return []


def save_trade(trade_type, price, amount, cost, symbol=None, fee=None, fee_currency=None,
               order_id=None, reason=None, pnl=None, partial=False):
    ledger = load_ledger()
    entry = {
        "timestamp": fmt_ts(utc_now()),
        "type": trade_type,
        "symbol": symbol,
        "price": price,
        "amount": amount,
        "cost": cost,
        "fee": fee,
        "fee_currency": fee_currency,
        "order_id": order_id,
        "reason": reason,
        "partial": partial,
        "dry_run": DRY_RUN,
    }
    if pnl is not None:
        entry["pnl"] = round(pnl, 6)
    ledger.append(entry)
    try:
        atomic_write_json(LEDGER_FILE, ledger)
    except Exception as e:
        logging.error(f"Error saving trade ledger: {e}")


# =============================================================================
# Market helpers (precision, minimums)
# =============================================================================
def market_base_quote(exchange, symbol):
    try:
        m = exchange.market(symbol)
        return m['base'], m['quote']
    except Exception:
        base, quote = symbol.split('/')
        return base, quote


def _tick_size(exchange, symbol):
    p = exchange.market(symbol)['precision'].get('price')
    if p is None:
        return None
    if exchange.precisionMode == ccxt.TICK_SIZE:
        return float(p)
    return 10 ** (-int(p))


def round_price(exchange, symbol, price, side):
    """price_to_precision, nudged so buys never round UP and sells never round DOWN
    (keeps post-only orders passive and keeps sells at/above the fee floor)."""
    p = float(exchange.price_to_precision(symbol, price))
    tick = _tick_size(exchange, symbol)
    if tick:
        if side == 'buy' and p > price * (1 + 1e-12):
            p = float(exchange.price_to_precision(symbol, p - tick))
        elif side == 'sell' and p < price * (1 - 1e-12):
            p = float(exchange.price_to_precision(symbol, p + tick))
    return p


def round_amount(exchange, symbol, amount):
    try:
        return float(exchange.amount_to_precision(symbol, amount))
    except ccxt.InvalidOrder:
        return 0.0


def check_order_limits(exchange, symbol, amount, price):
    """Return (ok, message) against the market's minimum amount and cost."""
    if amount <= 0:
        return False, "amount rounds to zero"
    limits = exchange.market(symbol).get('limits') or {}
    min_amount = (limits.get('amount') or {}).get('min')
    min_cost = (limits.get('cost') or {}).get('min')
    if min_amount and amount < min_amount:
        return False, f"amount {amount} < market minimum {min_amount}"
    if min_cost and amount * price < min_cost:
        return False, f"cost {amount * price:.4f} < market minimum cost {min_cost}"
    return True, ""


# =============================================================================
# Orders: place -> poll until filled -> cancel on timeout -> use actual fill
# =============================================================================
Fill = namedtuple('Fill', ['order_id', 'status', 'filled', 'average', 'cost',
                           'fee', 'fee_currency', 'fee_estimated'])


def fill_from_order(order, symbol, exchange, post_only):
    filled = float(order.get('filled') or 0.0)
    average = order.get('average')
    cost = order.get('cost')
    if filled > 0:
        if not average:
            average = (float(cost) / filled) if cost else float(order.get('price') or 0.0)
        if not cost:
            cost = filled * float(average)
    average = float(average or 0.0)
    cost = float(cost or 0.0)

    fee_cost, fee_currency, estimated = None, None, False
    fee = order.get('fee')
    fees = order.get('fees') or ([fee] if fee else [])
    fees = [f for f in fees if f and f.get('cost') is not None]
    if fees:
        fee_currency = fees[0].get('currency')
        fee_cost = sum(float(f['cost']) for f in fees if f.get('currency') == fee_currency)
    elif filled > 0:
        _, quote = market_base_quote(exchange, symbol)
        fee_cost = cost * (MAKER_FEE if post_only else TAKER_FEE)
        fee_currency = quote
        estimated = True
    return Fill(order.get('id'), order.get('status'), filled, average, cost,
                fee_cost, fee_currency, estimated)


def find_order_by_userref(exchange, symbol, userref):
    """Locate an order we may have placed (e.g. after a network error on create)."""
    for fetch in (exchange.fetch_open_orders, exchange.fetch_closed_orders):
        try:
            orders = fetch(symbol, params={'userref': userref})
        except ccxt.BaseError as e:
            logging.warning(f"Lookup by userref {userref} failed: {e}")
            continue
        for o in orders or []:
            info_ref = (o.get('info') or {}).get('userref')
            if info_ref is None or str(info_ref) == str(userref):
                return o
    return None


def wait_for_order(exchange, order_id, symbol, timeout_sec):
    """Poll until closed/canceled; on timeout cancel and return the FINAL order state.
    Returns an order dict whose status may still be 'open' if it couldn't be confirmed."""
    deadline = time.time() + timeout_sec
    order = None
    while True:
        try:
            order = exchange.fetch_order(order_id, symbol)
            status = order.get('status')
            amount = order.get('amount') or 0
            if status in ('closed', 'canceled', 'expired', 'rejected'):
                return order
            if amount and (order.get('filled') or 0) >= amount:
                return order
        except ccxt.NetworkError as e:
            logging.warning(f"fetch_order {order_id} network error: {e}")
        if time.time() >= deadline:
            break
        time.sleep(ORDER_POLL_SEC)

    logging.info(f"Order {order_id} not filled within {timeout_sec}s; cancelling.")
    for attempt in range(3):
        try:
            exchange.cancel_order(order_id, symbol)
            break
        except ccxt.OrderNotFound:
            break   # already closed/canceled - the final fetch below tells us which
        except ccxt.NetworkError as e:
            logging.warning(f"cancel_order {order_id} network error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)
        except ccxt.ExchangeError as e:
            logging.warning(f"cancel_order {order_id} exchange error: {e}")
            break

    for attempt in range(6):
        try:
            order = exchange.fetch_order(order_id, symbol)
            if order.get('status') != 'open':
                return order
        except ccxt.NetworkError as e:
            logging.warning(f"final fetch_order {order_id} error: {e}")
        time.sleep(2)
    return order or {'id': order_id, 'status': 'open', 'filled': 0.0}


def execute_order(exchange, state, symbol, side, amount, price, post_only, timeout_sec, meta):
    """Place a limit order and wait for the result.

    The order is recorded in state['pending_order'] BEFORE it is sent, so a crash or
    network error never loses track of it. Returns a Fill (filled may be 0), or None
    if the order's fate is unknown (pending_order is then left in state and resolved
    on the next loop / at startup). The caller applies the fill, which also clears
    pending_order in the same atomic state write.
    """
    params = {}
    if post_only:
        params['postOnly'] = True

    if DRY_RUN:
        logging.info(f"[DRY RUN] Would place {'post-only ' if post_only else ''}limit {side} "
                     f"{amount} {symbol} @ {price} (simulating full fill)")
        cost = amount * price
        _, quote = market_base_quote(exchange, symbol)
        return Fill(f"dryrun-{int(time.time() * 1000)}", 'closed', amount, price, cost,
                    cost * (MAKER_FEE if post_only else TAKER_FEE), quote, True)

    userref = random.randint(1, 2 ** 31 - 1)
    params['userref'] = userref
    state["pending_order"] = dict(meta, symbol=symbol, side=side, amount=amount, price=price,
                                  post_only=post_only, userref=userref, id=None,
                                  created=utc_now().isoformat())
    save_state(state)

    logging.info(f"Placing {'post-only ' if post_only else ''}limit {side} {amount} {symbol} @ {price}")
    try:
        order = exchange.create_order(symbol, 'limit', side, amount, price, params)
    except ccxt.NetworkError as e:
        logging.error(f"Network error creating order (it may or may not exist): {e}")
        time.sleep(5)
        order = find_order_by_userref(exchange, symbol, userref)
        if order is None:
            logging.error("Order not found yet; leaving it pending to be resolved next loop.")
            return None
    except ccxt.ExchangeError as e:
        # Rejected (e.g. post-only would cross, insufficient funds, below minimum):
        # nothing was placed, so nothing to record.
        logging.warning(f"Order rejected by exchange: {e}")
        state["pending_order"] = None
        save_state(state)
        return Fill(None, 'rejected', 0.0, 0.0, 0.0, None, None, False)

    state["pending_order"]["id"] = order['id']
    save_state(state)

    final = wait_for_order(exchange, order['id'], symbol, timeout_sec)
    if final.get('status') == 'open':
        logging.error(f"Order {order['id']} could not be confirmed closed/canceled; "
                      f"leaving it pending to be resolved next loop.")
        return None
    fill = fill_from_order(final, symbol, exchange, post_only)
    logging.info(f"Order {fill.order_id} final status={fill.status} filled={fill.filled} "
                 f"avg={fill.average} fee={fill.fee} {fill.fee_currency or ''}")
    return fill


# =============================================================================
# Applying fills to state + ledger (the only place positions are opened/closed)
# =============================================================================
def is_sellable(exchange, symbol, amount, price):
    ok, _ = check_order_limits(exchange, symbol, round_amount(exchange, symbol, amount), price)
    return ok


def apply_buy_fill(exchange, state, symbol, fill, signal_ts=None, reason="RSI + EMA Setup"):
    state["pending_order"] = None
    if fill is None or fill.filled <= 0:
        save_state(state)
        return False
    base, quote = market_base_quote(exchange, symbol)
    amount_held = fill.filled
    entry_cost = fill.cost
    if fill.fee:
        if fill.fee_currency == base:
            amount_held -= fill.fee
        elif fill.fee_currency == quote:
            entry_cost += fill.fee
    partial = fill.status != 'closed'

    if DRY_RUN:
        paper = state.setdefault("paper", {})
        paper[quote] = paper.get(quote, 0.0) - entry_cost
        paper[base] = paper.get(base, 0.0) + amount_held

    if signal_ts is not None:
        state.setdefault("last_entry_signal", {})[symbol] = signal_ts

    if is_sellable(exchange, symbol, amount_held, fill.average):
        state.update({
            "symbol": symbol,
            "last_buy_price": fill.average,
            "peak_price": fill.average,
            "amount": amount_held,
            "entry_time": utc_now().isoformat(),
            "entry_cost": entry_cost,
        })
        opened = True
    else:
        logging.warning(f"Buy fill of {amount_held} {base} is below the market minimum (dust); "
                        f"not opening a tracked position.")
        opened = False
    save_state(state)
    save_trade("BUY", fill.average, fill.filled, fill.cost, symbol=symbol, fee=fill.fee,
               fee_currency=fill.fee_currency, order_id=fill.order_id, reason=reason,
               partial=partial)
    return opened


def apply_sell_fill(exchange, state, symbol, fill, reason):
    state["pending_order"] = None
    if fill is None or fill.filled <= 0:
        save_state(state)
        return False
    base, quote = market_base_quote(exchange, symbol)
    position_amount = float(state.get("amount") or 0.0)
    quote_fee = (fill.fee or 0.0) if fill.fee_currency == quote else 0.0
    proceeds = fill.cost - quote_fee
    pnl = None
    if position_amount > 0 and state.get("entry_cost"):
        basis_per_unit = float(state["entry_cost"]) / position_amount
        pnl = proceeds - basis_per_unit * fill.filled
        state["entry_cost"] = float(state["entry_cost"]) - basis_per_unit * fill.filled

    if DRY_RUN:
        paper = state.setdefault("paper", {})
        paper[base] = paper.get(base, 0.0) - fill.filled
        paper[quote] = paper.get(quote, 0.0) + proceeds

    remaining = max(position_amount - fill.filled, 0.0)
    partial = fill.status != 'closed'
    if remaining > 0 and is_sellable(exchange, symbol, remaining, fill.average):
        state["amount"] = remaining
        logging.info(f"Partial sell: {remaining} {base} still held; will retry exit next loop.")
        closed = False
    else:
        if remaining > 0:
            logging.warning(f"Remaining {remaining} {base} is below the market minimum (dust); "
                            f"closing tracked position.")
        state.update(empty_position())
        closed = True
        if STOPLOSS_COOLDOWN_HOURS and str(reason).startswith("Stop-Loss"):
            until = utc_now() + timedelta(hours=STOPLOSS_COOLDOWN_HOURS)
            state.setdefault("cooldowns", {})[symbol] = until.isoformat()
            logging.info(f"Stop-loss on {symbol}: no new entries until {fmt_ts(until)}.")
    save_state(state)
    save_trade("SELL", fill.average, fill.filled, fill.cost, symbol=symbol, fee=fill.fee,
               fee_currency=fill.fee_currency, order_id=fill.order_id, reason=reason,
               pnl=pnl, partial=partial)
    return closed


def resolve_pending_order(exchange, state):
    """Handle an order left over from a crash / network error. Never records a trade
    unless the exchange confirms a fill. Returns True if nothing is pending anymore."""
    pending = state.get("pending_order")
    if not pending:
        return True
    if DRY_RUN:
        state["pending_order"] = None
        save_state(state)
        return True

    symbol = pending["symbol"]
    logging.warning(f"Resolving pending {pending['side']} order on {symbol}: {pending}")
    order = None
    try:
        if pending.get("id"):
            order = exchange.fetch_order(pending["id"], symbol)
        else:
            order = find_order_by_userref(exchange, symbol, pending["userref"])
    except ccxt.OrderNotFound:
        order = None

    if order is None:
        created = parse_iso(pending.get("created")) or utc_now()
        if (utc_now() - created).total_seconds() > PENDING_ORDER_GIVE_UP_SEC:
            logging.warning("Pending order never appeared on the exchange; assuming it was "
                            "not placed and clearing it.")
            state["pending_order"] = None
            save_state(state)
            return True
        logging.warning("Pending order not found yet; will retry next loop.")
        return False

    if order.get('status') == 'open':
        order = wait_for_order(exchange, order['id'], symbol, 0)  # cancels, then final fetch
        if order.get('status') == 'open':
            logging.error(f"Could not cancel pending order {order.get('id')}; will retry.")
            return False

    fill = fill_from_order(order, symbol, exchange, pending.get("post_only", True))
    if pending["side"] == 'buy':
        apply_buy_fill(exchange, state, symbol, fill, signal_ts=pending.get("signal_ts"),
                       reason=pending.get("reason", "RSI + EMA Setup") + " (recovered)")
    else:
        apply_sell_fill(exchange, state, symbol, fill,
                        reason=pending.get("reason", "Exit") + " (recovered)")
    return True


# =============================================================================
# Balances, reconciliation
# =============================================================================
def get_balances(exchange, state):
    """Return (free, total) balance dicts (paper wallet in DRY_RUN)."""
    if DRY_RUN:
        paper = dict(state.get("paper") or {})
        return paper, paper
    balance = exchange.fetch_balance()
    return balance.get('free') or {}, balance.get('total') or {}


def reconcile_on_startup(exchange):
    """Compare state with the exchange before trading; log discrepancies."""
    state = load_state()

    # Migrate state written by the original bot (no amount / entry_time fields).
    if state.get("symbol") and not state.get("entry_time"):
        logging.warning("State has no entry_time (old format); starting the max-hold "
                        "clock from now.")
        state["entry_time"] = utc_now().isoformat()

    if DRY_RUN:
        logging.info(f"[DRY RUN] Paper wallet: {state.get('paper')}; position: "
                     f"{state.get('symbol')} {state.get('amount')}")
        state["pending_order"] = None
        save_state(state)
        return

    resolve_pending_order(exchange, state)

    open_orders = exchange.fetch_open_orders()
    stray = [o for o in open_orders if o.get('symbol') in WATCHLIST]
    for o in stray:
        logging.warning(f"Open order on watchlist symbol not tracked by bot: {o.get('symbol')} "
                        f"{o.get('side')} {o.get('amount')} @ {o.get('price')} (id {o.get('id')})")
        if CANCEL_STRAY_ORDERS_ON_START:
            try:
                exchange.cancel_order(o['id'], o['symbol'])
                logging.warning(f"Cancelled stray order {o['id']}.")
            except ccxt.BaseError as e:
                logging.error(f"Failed to cancel stray order {o['id']}: {e}")
    if stray and not CANCEL_STRAY_ORDERS_ON_START:
        logging.warning("New entries on those symbols are skipped while their orders stay open.")

    free, total = get_balances(exchange, state)
    symbol = state.get("symbol")
    if symbol:
        base, _ = market_base_quote(exchange, symbol)
        held_total = float(total.get(base) or 0.0)
        held_free = float(free.get(base) or 0.0)
        amount = float(state.get("amount") or 0.0)
        if amount <= 0:
            amount = held_free
            logging.warning(f"State has no position amount (old format); adopting free "
                            f"{base} balance {amount}.")
            state["amount"] = amount
            if not state.get("entry_cost"):
                state["entry_cost"] = amount * float(state.get("last_buy_price") or 0.0)
        price = float(state.get("last_buy_price") or 0.0) or 1.0
        if held_total < amount * (1 - POSITION_TOLERANCE_PCT):
            logging.warning(f"Discrepancy: state says {amount} {base} but exchange holds "
                            f"{held_total}. Using the exchange balance.")
            if held_total > 0 and is_sellable(exchange, symbol, held_total, price):
                state["amount"] = held_total
            else:
                logging.error(f"No sellable {base} balance for the tracked position; "
                              f"clearing position state.")
                state.update(empty_position())
        elif held_total > amount * (1 + POSITION_TOLERANCE_PCT):
            logging.info(f"Exchange holds {held_total} {base}, more than the tracked "
                         f"{amount}; the extra is not managed by the bot.")
    else:
        for sym in WATCHLIST:
            base, _ = market_base_quote(exchange, sym)
            if float(total.get(base) or 0.0) > 0:
                logging.info(f"Holding {total.get(base)} {base} that is not a tracked "
                             f"position; the bot will not sell it.")
    save_state(state)
    logging.info(f"Reconcile complete. Position: {state.get('symbol')} amount={state.get('amount')}")


# =============================================================================
# Backtest (uses the same entry/exit functions as the live loop)
# =============================================================================
def _candle_path(o, h, l, c, steps):
    """Approximate intra-candle price path (O->L->H->C for up candles, O->H->L->C for
    down candles), linearly interpolated into ~`steps` points, to mimic the live loop
    polling every CHECK_INTERVAL_SEC inside each candle."""
    pts = [o, l, h, c] if c >= o else [o, h, l, c]
    per_seg = max(1, steps // 3)
    path = [o]
    for a, b in zip(pts[:-1], pts[1:]):
        for k in range(1, per_seg + 1):
            path.append(a + (b - a) * k / per_seg)
    return path


def simulate_exit_in_candle(pos, o, h, l, c, candle_start_ms, tf_sec, steps, rsi):
    """Shared backtest exit model for one candle while a position is open.

    pos: dict with entry_price, peak, entry_time (aware datetime); pos['peak'] is updated.
    Returns (fill_price, fee_rate, decision, exit_time) or None if still holding.
    Non-urgent exits fill (maker fee) only if price >= decision.min_price, mirroring the
    fee-floored post-only sell; stop-loss fills at the path price minus slippage with
    taker fee.
    """
    candle_start = datetime.fromtimestamp(candle_start_ms / 1000, tz=timezone.utc)
    candle_end = candle_start + timedelta(seconds=tf_sec)
    # Fast path: decide_exit is monotone in price/peak/time, so if neither extreme of
    # the candle (low with the highest possible peak, or high) at the candle's end
    # triggers anything, nothing inside the candle can.
    peak_hi = max(pos['peak'], h)
    if decide_exit(pos['entry_price'], peak_hi, pos['entry_time'], l, candle_end, rsi=rsi) is None \
            and decide_exit(pos['entry_price'], peak_hi, pos['entry_time'], h, candle_end, rsi=rsi) is None:
        pos['peak'] = peak_hi
        return None
    path = _candle_path(o, h, l, c, steps)
    n = len(path) - 1
    for k, p in enumerate(path):
        t = candle_start + timedelta(seconds=tf_sec * k / n)
        pos['peak'] = max(pos['peak'], p)
        d = decide_exit(pos['entry_price'], pos['peak'], pos['entry_time'], p, t, rsi=rsi)
        if d is None:
            continue
        if d.urgent:
            return p * (1 - BACKTEST_TAKER_SLIPPAGE_PCT), TAKER_FEE, d, t
        if d.min_price is not None and p < d.min_price:
            continue    # fee-floored post-only sell would not fill here
        return p, MAKER_FEE, d, t
    return None


def backtest_on_dataframe(df, timeframe=TIMEFRAME, start_cash=BACKTEST_START_CASH,
                          return_trades=False):
    """Simulate the live strategy on CLOSED candles of one symbol (offline / testable).

    - Entry: entry_signals() on closed candle i -> post-only buy at the open of candle
      i+1 (maker fee), sized like live (POSITION_SIZE_PCT of cash). One entry per signal
      candle; no entries during the post-stop-loss cooldown (same rules as live).
    - Exits: shared decide_exit() via simulate_exit_in_candle().
    df may already contain indicator columns (optimize.py precomputes them).
    """
    if 'rsi' not in df.columns or 'ema_200' not in df.columns:
        df = add_indicators(df)
    signal, _ = entry_signals(df)
    sig = signal.to_numpy()
    ts = df['timestamp'].to_numpy(dtype='int64')
    o_, h_, l_, c_ = (df[k].to_numpy(dtype=float) for k in ('open', 'high', 'low', 'close'))
    rsi_ = df['rsi'].to_numpy(dtype=float)
    tf_sec = ccxt.Exchange.parse_timeframe(timeframe)
    steps = max(4, int(tf_sec // CHECK_INTERVAL_SEC))
    cooldown_ms = int((STOPLOSS_COOLDOWN_HOURS or 0) * 3_600_000)

    cash = start_cash
    amount = entry_cost = 0.0
    pos = None
    cooldown_until = -1
    trades = wins = 0
    pnls, trade_log = [], []
    equity_peak, max_dd = start_cash, 0.0

    for i in range(1, len(ts)):
        if amount == 0:
            trade_usd = cash * POSITION_SIZE_PCT
            if sig[i - 1] and ts[i] >= cooldown_until and trade_usd >= MIN_TRADE_USD:
                entry_price = o_[i]
                amount = (trade_usd - trade_usd * MAKER_FEE) / entry_price
                cash -= trade_usd
                entry_cost = trade_usd
                pos = {'entry_price': entry_price, 'peak': entry_price, 'entry_i': i,
                       'entry_time': datetime.fromtimestamp(ts[i] / 1000, tz=timezone.utc)}
            else:
                continue    # flat: equity unchanged

        res = simulate_exit_in_candle(pos, o_[i], h_[i], l_[i], c_[i], ts[i], tf_sec, steps,
                                      rsi_[i - 1])
        if res is not None:
            fill_price, fee_rate, d, t = res
            gross = amount * fill_price
            net = gross - gross * fee_rate
            pnl = net - entry_cost
            cash += net
            trades += 1
            wins += 1 if pnl > 0 else 0
            pnls.append(pnl)
            if return_trades:
                trade_log.append({"entry_ts": int(ts[pos['entry_i']]), "exit_time": t.isoformat(),
                                  "entry": pos['entry_price'], "exit": fill_price,
                                  "reason": d.reason, "pnl": pnl, "ret": pnl / entry_cost})
            if d.urgent and cooldown_ms:
                cooldown_until = int(t.timestamp() * 1000) + cooldown_ms
            amount = 0.0
            pos = None
        equity = cash + amount * c_[i]
        equity_peak = max(equity_peak, equity)
        max_dd = max(max_dd, (equity_peak - equity) / equity_peak)

    final_val = cash + amount * float(c_[-1]) * (1 - MAKER_FEE) if len(ts) else cash
    days = (ts[-1] - ts[0]) / 86_400_000 if len(ts) > 1 else 0.0
    out = {
        "return_pct": round((final_val - start_cash) / start_cash * 100, 2),
        "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
        "trades": trades,
        "open_position": amount > 0,
        "days": round(float(days), 1),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "pnls": pnls,
    }
    if return_trades:
        out["trade_log"] = trade_log
    return out


def fetch_ohlcv_history(exchange, symbol, timeframe, days):
    """Paginate fetch_ohlcv forward from `days` ago. Kraken only serves the most
    recent 720 candles, so for 4h this yields at most ~120 days."""
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = exchange.milliseconds()
    since = now_ms - days * 86_400_000
    candles = {}
    for _ in range(20):
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=720)
        if not batch:
            break
        new = 0
        for c in batch:
            if c[0] not in candles:
                candles[c[0]] = c
                new += 1
        last_ts = batch[-1][0]
        if new == 0 or last_ts + tf_ms >= now_ms:
            break
        since = last_ts + tf_ms
    rows = [candles[k] for k in sorted(candles) if k >= now_ms - days * 86_400_000]
    return rows


def run_backtest_simulation(symbol):
    name = symbol.split('/')[0]
    try:
        exchange = get_public_exchange()
        candles = fetch_ohlcv_history(exchange, symbol, TIMEFRAME, BACKTEST_DAYS)
        df = ohlcv_to_closed_df(candles, TIMEFRAME)
        if len(df) < EMA_LONG_PERIOD + 10:
            return {"symbol": name, "return_pct": 0.0, "win_rate": 0.0, "trades": 0, "days": 0}
        res = backtest_on_dataframe(df, TIMEFRAME)
        return {"symbol": name, "return_pct": res["return_pct"], "win_rate": res["win_rate"],
                "trades": res["trades"], "days": res["days"]}
    except Exception as e:
        logging.error(f"Error in backtest simulation for {symbol}: {e}")
        return {"symbol": name, "return_pct": 0.0, "win_rate": 0.0, "trades": 0, "days": 0}


def update_scanner_cache():
    while True:
        try:
            results = [run_backtest_simulation(sym) for sym in WATCHLIST]
            scanner_cache["results"] = sorted(results, key=lambda x: x['return_pct'], reverse=True)
            days = [r["days"] for r in results if r.get("days")]
            scanner_cache["days"] = int(round(min(days))) if days else BACKTEST_DAYS
            scanner_cache["last_updated"] = fmt_ts(utc_now())
        except Exception as e:
            logging.error(f"Error updating scanner cache: {e}")
        time.sleep(SCANNER_REFRESH_SEC)


# =============================================================================
# Live trading loop
# =============================================================================
def fetch_closed_indicators(exchange, symbol, return_df=False):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=OHLCV_LIMIT)
    df = ohlcv_to_closed_df(ohlcv, TIMEFRAME, exchange.milliseconds())
    if len(df) < EMA_PERIOD + 1:
        return None
    df = add_indicators(df)
    return df if return_df else df.iloc[-1]   # last CLOSED candle


def manage_position(exchange, state, free, total):
    symbol = state["symbol"]
    base, _ = market_base_quote(exchange, symbol)
    entry_price = float(state.get("last_buy_price") or 0.0)
    position_amount = float(state.get("amount") or 0.0)
    entry_time = parse_iso(state.get("entry_time"))

    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker.get('last') or ticker.get('bid')

    held_total = float(total.get(base) or 0.0)
    if not is_sellable(exchange, symbol, min(held_total, position_amount) or held_total, current_price):
        logging.error(f"Tracked position {position_amount} {base} but exchange holds "
                      f"{held_total} (not sellable). Clearing position state.")
        state.update(empty_position())
        save_state(state)
        bot_status["last_action"] = f"Position in {base} no longer on exchange; state cleared."
        return

    if current_price > float(state.get("peak_price") or 0.0):
        state["peak_price"] = current_price
        save_state(state)
    peak_price = state["peak_price"]

    bot_status["active_symbol"] = symbol
    bot_status["price"] = current_price
    bot_status["asset_balance"] = position_amount

    rsi = None
    if USE_RSI_OVERBOUGHT_EXIT:
        row = fetch_closed_indicators(exchange, symbol)
        if row is not None:
            rsi = float(row['rsi'])
            bot_status["rsi"] = rsi
            bot_status["ema_50"] = float(row['ema_50'])

    decision = decide_exit(entry_price, peak_price, entry_time, current_price, utc_now(), rsi=rsi)
    if decision is None:
        trail = trailing_stop_price(entry_price, peak_price)
        armed = TRAIL_PCT is not None and peak_price >= entry_price * (1 + TRAIL_ACTIVATE_PCT)
        bot_status["last_action"] = (
            f"Holding {position_amount:g} {base} @ entry ${entry_price:,.4f}. Peak: ${peak_price:,.4f}"
            + (f", trail stop ${trail:,.4f}" if armed else "")
        )
        return

    logging.info(f"Exit signal on {symbol}: {decision.reason} (price {current_price})")
    attempts = STOP_LOSS_RETRIES if decision.urgent else 1
    for attempt in range(attempts):
        sell_amount = min(float(state.get("amount") or 0.0), float(free.get(base) or 0.0))
        sell_amount = round_amount(exchange, symbol, sell_amount)
        ticker = exchange.fetch_ticker(symbol) if attempt > 0 else ticker
        bid = ticker.get('bid') or current_price
        ask = ticker.get('ask') or current_price
        if decision.urgent:
            # Marketable limit (crosses the spread; taker fee) - getting out matters more.
            price = round_price(exchange, symbol, bid * (1 - STOP_LOSS_MAX_SLIPPAGE_PCT), 'buy')
            post_only, timeout = False, STOP_LOSS_ORDER_TIMEOUT_SEC
        else:
            # Post-only at the ask, never below the fee floor (if one applies).
            price = max(ask, decision.min_price or 0.0)
            price = round_price(exchange, symbol, price, 'sell')
            post_only, timeout = True, ORDER_TIMEOUT_SEC

        ok, msg = check_order_limits(exchange, symbol, sell_amount, price)
        if not ok:
            logging.warning(f"Cannot sell {symbol}: {msg} (free {free.get(base)}, tracked "
                            f"{state.get('amount')})")
            bot_status["last_action"] = f"Exit blocked for {base}: {msg}"
            return

        fill = execute_order(exchange, state, symbol, 'sell', sell_amount, price, post_only,
                             timeout, {"reason": decision.reason})
        if fill is None:
            bot_status["last_action"] = f"Sell order on {base} unresolved; will reconcile."
            return
        closed = apply_sell_fill(exchange, state, symbol, fill, decision.reason)
        if fill.filled > 0:
            bot_status["last_action"] = (f"Sold {fill.filled:g} {base} at ${fill.average:,.4f} "
                                         f"({decision.reason}){'' if closed else ' - partial'}")
        else:
            bot_status["last_action"] = f"Sell on {base} not filled ({decision.reason}); will retry."
        if closed:
            return
        if decision.urgent:
            free, _ = get_balances(exchange, state)


def scan_and_enter(exchange, state, usd_free):
    blocked = set()
    if not DRY_RUN:
        blocked = {o.get('symbol') for o in exchange.fetch_open_orders()
                   if o.get('symbol') in WATCHLIST}
        if blocked:
            logging.warning(f"Skipping entries on symbols with open orders: {sorted(blocked)}")

    best = None
    now = utc_now()
    for sym in WATCHLIST:
        if sym in blocked:
            continue
        cd_until = parse_iso((state.get("cooldowns") or {}).get(sym))
        if cd_until and cd_until > now:
            logging.info(f"{sym} in post-stop-loss cooldown until {fmt_ts(cd_until)}; skipping.")
            continue
        df = fetch_closed_indicators(exchange, sym, return_df=True)
        if df is None:
            continue
        row = df.iloc[-1]
        signal, score = check_entry_signal(df)
        signal_ts = int(row['timestamp'])
        if signal and state.get("last_entry_signal", {}).get(sym) == signal_ts:
            continue    # already traded this exact signal candle
        if signal and (best is None or score > best[1]):
            best = (sym, score, row, signal_ts)

    if best is None:
        bot_status["active_symbol"] = "None (Scanning Watchlist)"
        bot_status["last_action"] = "No valid entry setups found. Holding USD."
        return
    sym, score, row, signal_ts = best
    base, _ = market_base_quote(exchange, sym)
    bot_status["rsi"] = float(row['rsi'])
    bot_status["ema_50"] = float(row['ema_50'])

    if usd_free < MIN_TRADE_USD:
        bot_status["last_action"] = f"Setup on {base} but USD balance below ${MIN_TRADE_USD:.2f}."
        return

    ticker = exchange.fetch_ticker(sym)
    bid = ticker.get('bid') or ticker.get('last')
    bot_status["price"] = bid
    price = round_price(exchange, sym, bid, 'buy')
    trade_amount_usd = usd_free * POSITION_SIZE_PCT
    amount = round_amount(exchange, sym, trade_amount_usd / price)
    ok, msg = check_order_limits(exchange, sym, amount, price)
    if not ok or amount * price < MIN_TRADE_USD:
        logging.info(f"Skipping buy on {sym}: {msg or 'below MIN_TRADE_USD'}")
        bot_status["last_action"] = f"Setup on {base} but order too small: {msg or 'below MIN_TRADE_USD'}"
        return

    logging.info(f"Executing post-only limit buy on {sym} at RSI {row['rsi']:.1f}: {amount} @ {price}")
    fill = execute_order(exchange, state, sym, 'buy', amount, price, True, ORDER_TIMEOUT_SEC,
                         {"reason": "RSI + EMA Setup", "signal_ts": signal_ts})
    if fill is None:
        bot_status["last_action"] = f"Buy order on {base} unresolved; will reconcile."
        return
    opened = apply_buy_fill(exchange, state, sym, fill, signal_ts=signal_ts)
    if fill.filled > 0:
        bot_status["active_symbol"] = sym if opened else bot_status["active_symbol"]
        bot_status["asset_balance"] = state.get("amount", 0.0)
        bot_status["last_action"] = (f"Bought {fill.filled:g} {base} for ${fill.cost:,.2f} at "
                                     f"${fill.average:,.4f}{'' if fill.status == 'closed' else ' (partial fill)'}")
    else:
        bot_status["last_action"] = f"Buy on {base} not filled within {ORDER_TIMEOUT_SEC}s; cancelled."


def trading_iteration(exchange):
    state = load_state()
    free, total = get_balances(exchange, state)
    usd_free = float(free.get('USD') or 0.0)
    bot_status["usd_balance"] = usd_free
    bot_status["last_check"] = fmt_ts(utc_now())
    if state.get("pending_order"):
        if not resolve_pending_order(exchange, state):
            bot_status["last_action"] = "Waiting to resolve a pending order..."
            return
        free, total = get_balances(exchange, state)
        usd_free = float(free.get('USD') or 0.0)

    if state.get("symbol"):
        manage_position(exchange, state, free, total)
    else:
        bot_status["asset_balance"] = 0.0
        scan_and_enter(exchange, state, usd_free)

    # refresh balance shown on the dashboard after any trade
    free, _ = get_balances(exchange, load_state())
    bot_status["usd_balance"] = float(free.get('USD') or 0.0)
    bot_status["last_check"] = fmt_ts(utc_now())


def run_trading_bot():
    logging.info(f"Starting multi-asset rotation trading bot loop... "
                 f"({'DRY RUN - no real orders' if DRY_RUN else 'LIVE TRADING'})")
    if not DRY_RUN and not (os.getenv("KRAKEN_API_KEY") and os.getenv("KRAKEN_SECRET_KEY")):
        logging.error("KRAKEN_API_KEY / KRAKEN_SECRET_KEY not set. Set them, or run with "
                      "DRY_RUN=true to paper trade. Trading loop not started.")
        bot_status["last_action"] = "Error: API keys missing (set DRY_RUN=true to paper trade)"
        return

    exchange = get_exchange()
    backoff = 0
    ready = False
    while True:
        sleep_for = CHECK_INTERVAL_SEC
        try:
            if not ready:
                ensure_markets(exchange)
                reconcile_on_startup(exchange)
                ready = True
            trading_iteration(exchange)
            backoff = 0
        except ccxt.NetworkError as e:
            backoff = min(max(30, backoff * 2), CHECK_INTERVAL_SEC)
            sleep_for = backoff
            logging.warning(f"Network error: {e}. Retrying in {backoff}s.")
            bot_status["last_action"] = f"Network error (retrying in {backoff}s): {e}"
        except ccxt.AuthenticationError as e:
            logging.error(f"Authentication error - check API keys/permissions: {e}")
            bot_status["last_action"] = f"Authentication error: {e}"
        except ccxt.ExchangeError as e:
            backoff = min(max(60, backoff * 2), CHECK_INTERVAL_SEC)
            sleep_for = backoff if not ready else CHECK_INTERVAL_SEC
            logging.error(f"Exchange error: {e}")
            bot_status["last_action"] = f"Exchange error: {e}"
        except Exception as e:
            logging.exception(f"Critical error in trading bot loop: {e}")
            bot_status["last_action"] = f"Error: {e}"
        time.sleep(sleep_for)


# =============================================================================
# Dashboard
# =============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Kraken Advanced RSI Bot Dashboard</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #e0e0e0; padding: 20px; }
        .card { background: #1e1e1e; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h1, h2 { color: #00adb5; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .metric { background: #2d2d2d; padding: 15px; border-radius: 6px; text-align: center; }
        .metric h3 { margin: 0; font-size: 14px; color: #aaa; }
        .metric p { font-size: 20px; font-weight: bold; margin: 10px 0 0 0; color: #fff; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #00adb5; }
    </style>
</head>
<body>
    <h1>Kraken Advanced Bot Dashboard{% if dry_run %} <span style="color:#f0ad4e;">(DRY RUN)</span>{% endif %}</h1>
    
    <div class="card">
        <h2>Active Status & Portfolio</h2>
        <div class="grid">
            <div class="metric"><h3>Active Symbol</h3><p>{{ status.active_symbol }}</p></div>
            <div class="metric"><h3>Current Price</h3><p>${{ "%.4f"|format(status.price or 0) if (status.price or 0) < 1 else "%.2f"|format(status.price) }}</p></div>
            <div class="metric"><h3>USD Balance</h3><p>${{ "%.2f"|format(status.usd_balance) }}</p></div>
            <div class="metric"><h3>Asset Balance</h3><p>{{ "%.4f"|format(status.asset_balance) }}</p></div>
        </div>
        <p><strong>Last Checked:</strong> {{ status.last_check }} | <strong>Last Action:</strong> {{ status.last_action }}</p>
    </div>

    <div class="card">
        <h2>Multi-Asset Rotation Scanner</h2>
        <p style="font-size: 13px; color: #aaa;">Simulates ~{{ scanner.days }}-day performance of the live strategy ({{ timeframe }} candles: {{ strategy }}), including Kraken fees. (Updated: {{ scanner.last_updated }})</p>
        <table>
            <tr><th>Asset</th><th>{{ scanner.days }}-Day Return</th><th>Win Rate</th><th>Trades Executed</th></tr>
            {% for item in scanner.results %}
            <tr>
                <td style="font-weight:bold; color: #00adb5;">{{ item.symbol }}</td>
                <td style="color: {{ '#28a745' if item.return_pct >= 0 else '#dc3545' }}; font-weight:bold;">{{ item.return_pct }}%</td>
                <td>{{ item.win_rate }}%</td>
                <td>{{ item.trades }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>

    <div class="card">
        <h2>Recent Trade Ledger</h2>
        <table>
            <tr><th>Timestamp</th><th>Type</th><th>Price</th><th>Amount</th><th>Cost/Value</th></tr>
            {% for trade in trades %}
            <tr>
                <td>{{ trade.timestamp }}</td>
                <td style="color: {{ '#28a745' if trade.type == 'BUY' else '#dc3545' }}; font-weight:bold;">{{ trade.type }}{% if trade.symbol %} {{ trade.symbol.split('/')[0] }}{% endif %}</td>
                <td>${{ "%.4f"|format(trade.price) if trade.price < 1 else "%.2f"|format(trade.price) }}</td>
                <td>{{ "%.6f"|format(trade.amount) }}</td>
                <td>${{ "%.2f"|format(trade.cost) }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""


@app.before_request
def require_basic_auth():
    if not DASHBOARD_PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.password is not None \
            and hmac.compare_digest((auth.username or '').encode(), DASHBOARD_USER.encode()) \
            and hmac.compare_digest(auth.password.encode(), DASHBOARD_PASSWORD.encode()):
        return None
    return Response('Authentication required', 401,
                    {'WWW-Authenticate': 'Basic realm="KrakenBot Dashboard"'})


@app.route('/')
def dashboard():
    try:
        pnl_data = load_ledger()
        return render_template_string(
            HTML_TEMPLATE,
            status=bot_status,
            trades=pnl_data[-10:],
            scanner=scanner_cache,
            timeframe=TIMEFRAME,
            strategy=strategy_description(),
            dry_run=DRY_RUN
        )
    except Exception as e:
        logging.exception("Error rendering dashboard GET request")
        return f"An error occurred: {e}", 500


if __name__ == '__main__':
    if DASHBOARD_HOST not in ('127.0.0.1', 'localhost', '::1') and not DASHBOARD_PASSWORD:
        logging.warning(f"Dashboard bound to {DASHBOARD_HOST} without DASHBOARD_PASSWORD; "
                        f"anyone on the network can view it.")

    bot_thread = threading.Thread(target=run_trading_bot, daemon=True)
    bot_thread.start()

    scanner_thread = threading.Thread(target=update_scanner_cache, daemon=True)
    scanner_thread.start()

    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT)
