import os
import time
import json
import threading
import logging
from datetime import datetime, timedelta
import ccxt
import pandas as pd
from flask import Flask, render_template_string
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

# Watchlist & Configuration Parameters
WATCHLIST = ['SOL/USD', 'AVAX/USD', 'DOGE/USD', 'NEAR/USD', 'SUI/USD']
TIMEFRAME = '4h'
RSI_OVERSOLD = 28
RSI_OVERBOUGHT = 68
CHECK_INTERVAL_SEC = 900
LEDGER_FILE = 'trade_ledger.json'
STATE_FILE = 'bot_state.json'

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
    "results": []
}

def get_exchange():
    return ccxt.kraken({
        'apiKey': os.getenv("KRAKEN_API_KEY"),
        'secret': os.getenv("KRAKEN_SECRET_KEY"),
        'enableRateLimit': True
    })

def calculate_rsi(data, period=14):
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"symbol": None, "last_buy_price": 0.0, "peak_price": 0.0}
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading state file: {e}")
        return {"symbol": None, "last_buy_price": 0.0, "peak_price": 0.0}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving state file: {e}")

def load_ledger():
    if not os.path.exists(LEDGER_FILE):
        return []
    try:
        with open(LEDGER_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading trade ledger: {e}")
        return []

def save_trade(trade_type, price, amount, cost):
    ledger = load_ledger()
    ledger.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": trade_type,
        "price": price,
        "amount": amount,
        "cost": cost
    })
    try:
        with open(LEDGER_FILE, 'w') as f:
            json.dump(ledger, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving trade ledger: {e}")

def run_backtest_simulation(symbol):
    try:
        exchange = get_exchange()
        since = exchange.parse8601((datetime.utcnow() - timedelta(days=30)).isoformat())
        candles = exchange.fetch_ohlcv(symbol, timeframe='4h', since=since, limit=200)
        
        if not candles or len(candles) < 50:
            return {"symbol": symbol.split('/')[0], "return_pct": 0.0, "win_rate": 0.0, "trades": 0}

        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = calculate_rsi(df, period=14)
        df['ema_50'] = df['close'].ewm(span=50).mean()
        df.dropna(inplace=True)

        start_cash = 1000.0
        cash = start_cash
        asset_held = 0.0
        trade_amt = 500.0
        maker_fee = 0.0025
        trades, wins = 0, 0
        last_buy_price = 0.0

        for i in range(len(df)):
            price = df['close'].iat[i]
            rsi = df['rsi'].iat[i]
            ema = df['ema_50'].iat[i]

            if asset_held == 0 and rsi < RSI_OVERSOLD and price > ema and cash >= trade_amt:
                fee = trade_amt * maker_fee
                asset_held = (trade_amt - fee) / price
                cash -= trade_amt
                last_buy_price = price
                trades += 1
            elif asset_held > 0 and (price >= last_buy_price * 1.03 or rsi > RSI_OVERBOUGHT):
                gross_sale = asset_held * price
                fee = gross_sale * maker_fee
                net_sale = gross_sale - fee
                profit = net_sale - trade_amt
                cash += net_sale
                if profit > 0:
                    wins += 1
                asset_held = 0.0

        final_val = cash + (asset_held * df['close'].iloc[-1])
        return_pct = ((final_val - start_cash) / start_cash) * 100
        win_rate = (wins / trades * 100) if trades > 0 else 0.0

        return {
            "symbol": symbol.split('/')[0],
            "return_pct": round(return_pct, 2),
            "win_rate": round(win_rate, 1),
            "trades": trades
        }
    except Exception as e:
        logging.error(f"Error in backtest simulation for {symbol}: {e}")
        return {"symbol": symbol.split('/')[0], "return_pct": 0.0, "win_rate": 0.0, "trades": 0}

def update_scanner_cache():
    global scanner_cache
    while True:
        try:
            results = [run_backtest_simulation(sym) for sym in WATCHLIST]
            scanner_cache["results"] = sorted(results, key=lambda x: x['return_pct'], reverse=True)
            scanner_cache["last_updated"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            logging.error(f"Error updating scanner cache: {e}")
        time.sleep(3600)

def run_trading_bot():
    global bot_status
    logging.info("Starting multi-asset rotation trading bot loop...")
    
    while True:
        try:
            exchange = get_exchange()
            balance = exchange.fetch_balance()
            usd_free = balance['free'].get('USD', 0)
            
            state = load_state()
            active_symbol = state.get("symbol")
            last_buy_price = state.get("last_buy_price", 0.0)
            peak_price = state.get("peak_price", 0.0)
            
            if active_symbol:
                base_currency = active_symbol.split('/')[0]
                asset_free = balance['free'].get(base_currency, 0)
                ticker = exchange.fetch_ticker(active_symbol)
                current_price = ticker['last']
                
                if current_price > peak_price:
                    peak_price = current_price
                    state["peak_price"] = peak_price
                    save_state(state)
                
                bot_status["active_symbol"] = active_symbol
                bot_status["price"] = current_price
                bot_status["asset_balance"] = asset_free
                
                target_price = last_buy_price * 1.03
                trailing_stop_trigger = last_buy_price * 1.015
                is_trailing_active = current_price >= trailing_stop_trigger
                trailing_stop_price = peak_price * 0.99
                
                sell_condition = (current_price >= target_price) or (is_trailing_active and current_price <= trailing_stop_price)
                
                if asset_free * current_price >= 10.0 and sell_condition:
                    reason = "3% Target Hit" if current_price >= target_price else "Trailing Stop Triggered"
                    logging.info(f"Executing Limit Sell on {active_symbol} due to: {reason}")
                    
                    order = exchange.create_limit_sell_order(active_symbol, asset_free, current_price)
                    save_trade("SELL", current_price, asset_free, asset_free * current_price)
                    
                    save_state({"symbol": None, "last_buy_price": 0.0, "peak_price": 0.0})
                    bot_status["last_action"] = f"Sold {base_currency} at ${current_price:,.2f} ({reason})"
                else:
                    bot_status["last_action"] = f"Holding {base_currency}. Peak: ${peak_price:,.2f}"
                
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            best_symbol = None
            best_score = -999
            target_data = None
            
            for sym in WATCHLIST:
                ohlcv = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['rsi'] = calculate_rsi(df, period=14)
                df['ema_50'] = df['close'].ewm(span=50).mean()
                df.dropna(inplace=True)
                
                current_price = df['close'].iloc[-1]
                current_rsi = df['rsi'].iloc[-1]
                ema_50 = df['ema_50'].iloc[-1]
                
                if current_rsi < RSI_OVERSOLD and current_price > ema_50:
                    score = (RSI_OVERSOLD - current_rsi)
                    if score > best_score:
                        best_score = score
                        best_symbol = sym
                        target_data = (df, current_price, current_rsi, ema_50)

            if best_symbol and usd_free >= 10.0:
                df, current_price, current_rsi, ema_50 = target_data
                trade_amount_usd = usd_free * 0.80
                base_currency = best_symbol.split('/')[0]
                
                asset_to_buy = trade_amount_usd / current_price
                logging.info(f"Executing Limit Buy on {best_symbol} at RSI {current_rsi:.1f}")
                
                order = exchange.create_limit_buy_order(best_symbol, asset_to_buy, current_price)
                save_trade("BUY", current_price, asset_to_buy, trade_amount_usd)
                
                save_state({"symbol": best_symbol, "last_buy_price": current_price, "peak_price": current_price})
                bot_status["active_symbol"] = best_symbol
                bot_status["last_action"] = f"Bought {base_currency} with ${trade_amount_usd:,.2f} at ${current_price:,.2f}"
            else:
                bot_status["active_symbol"] = "None (Scanning Watchlist)"
                bot_status["last_action"] = "No valid EMA + RSI setups found. Holding USD."

            bot_status["last_check"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            bot_status["usd_balance"] = usd_free

        except Exception as e:
            logging.exception(f"Critical error in trading bot loop: {e}")
            bot_status["last_action"] = f"Error: {e}"
        
        time.sleep(CHECK_INTERVAL_SEC)

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
    <h1>Kraken Advanced Bot Dashboard</h1>
    
    <div class="card">
        <h2>Active Status & Portfolio</h2>
        <div class="grid">
            <div class="metric"><h3>Active Symbol</h3><p>{{ status.active_symbol }}</p></div>
            <div class="metric"><h3>Current Price</h3><p>${{ "%.2f"|format(status.price) }}</p></div>
            <div class="metric"><h3>USD Balance</h3><p>${{ "%.2f"|format(status.usd_balance) }}</p></div>
            <div class="metric"><h3>Asset Balance</h3><p>{{ "%.4f"|format(status.asset_balance) }}</p></div>
        </div>
        <p><strong>Last Checked:</strong> {{ status.last_check }} | <strong>Last Action:</strong> {{ status.last_action }}</p>
    </div>

    <div class="card">
        <h2>Multi-Asset Rotation Scanner</h2>
        <p style="font-size: 13px; color: #aaa;">Simulates 30-day performance using the 50 EMA Trend Filter and RSI rules. (Updated: {{ scanner.last_updated }})</p>
        <table>
            <tr><th>Asset</th><th>30-Day Return</th><th>Win Rate</th><th>Trades Executed</th></tr>
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
                <td style="color: {{ '#28a745' if trade.type == 'BUY' else '#dc3545' }}; font-weight:bold;">{{ trade.type }}</td>
                <td>${{ "%.2f"|format(trade.price) }}</td>
                <td>{{ "%.6f"|format(trade.amount) }}</td>
                <td>${{ "%.2f"|format(trade.cost) }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    try:
        pnl_data = load_ledger()
        return render_template_string(
            HTML_TEMPLATE, 
            status=bot_status, 
            trades=pnl_data[-10:],
            scanner=scanner_cache
        )
    except Exception as e:
        logging.exception("Error rendering dashboard GET request")
        return f"An error occurred: {e}", 500

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_trading_bot, daemon=True)
    bot_thread.start()
    
    scanner_thread = threading.Thread(target=update_scanner_cache, daemon=True)
    scanner_thread.start()
    
    app.run(host='0.0.0.0', port=5000)
