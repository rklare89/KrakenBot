"""Fetch public daily candles for the momentum universe study (no API keys).

Pool = every Kraken USD spot pair whose base is also traded vs USD on Coinbase Exchange
(today's listings -> disclosed survivorship bias), minus fiat / stablecoins / wrapped or
staked derivatives / commodity tokens (EXCLUDE). For each coin:
  data/universe/cb_<COIN>_1d.csv  Coinbase daily candles since 2022-01-01 (long history)
  data/universe/kr_<COIN>_1d.csv  Kraken daily candles (last 720 days; Kraken's limit)
  data/universe/pool.json         pool with today's Kraken 24h USD volume (for reference only)
"""
import json
import os
import sys
import time

import pandas as pd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'universe')
COLS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
FIAT = {'EUR', 'GBP', 'AUD', 'CAD', 'CHF', 'JPY', 'USD'}
STABLE = {'USDT', 'USDC', 'DAI', 'PYUSD', 'USDG', 'RLUSD', 'EURC', 'TUSD', 'USDS', 'USDE', 'FDUSD', 'GUSD',
          'USD1', 'EURQ', 'USDQ', 'USDR', 'FRAX', 'LUSD', 'UST', 'USTC', 'EUROP', 'AUDD', 'EURR', 'USDD', 'SUSDE',
          'MIM', 'GHO', 'CRVUSD', 'FDIT', 'USDP', 'PAX', 'BUSD', 'ZUSD', 'TGBP', 'XSGD'}
WRAPPED = {'WBTC', 'CBBTC', 'WETH', 'CBETH', 'STETH', 'WSTETH', 'TBTC', 'LSETH', 'RETH', 'MSOL', 'JITOSOL',
           'BSOL', 'JUPSOL', 'CBDOGE', 'CBXRP', 'CBADA', 'CBLTC', 'WAXL', 'METH', 'EETH', 'WEETH', 'EZETH', 'SOLVBTC'}
COMMODITY = {'PAXG', 'XAUT'}
EXCLUDE = FIAT | STABLE | WRAPPED | COMMODITY


def build_pool():
    import ccxt
    k = ccxt.kraken({'enableRateLimit': True})
    cb = ccxt.coinbaseexchange({'enableRateLimit': True})
    km = k.load_markets()
    cm = cb.load_markets()
    cb_usd = {m['base'] for m in cm.values() if m['quote'] == 'USD' and m.get('spot') and m.get('active') is not False}
    tick = k.publicGetTicker()['result']
    pool = []
    for sym, m in km.items():
        if m['quote'] != 'USD' or not m.get('spot') or m['base'] in EXCLUDE or m['base'] not in cb_usd:
            continue
        if m.get('darkpool') or '.d' in m['id']:
            continue
        t = tick.get(m['id'])
        vol = float(t['v'][1]) * float(t['p'][1]) if t else 0.0
        pool.append({'coin': m['base'], 'kraken': sym, 'coinbase': f"{m['base']}/USD", 'vol24h_usd': vol})
    pool.sort(key=lambda r: -r['vol24h_usd'])
    return k, cb, pool


def main():
    os.makedirs(OUT, exist_ok=True)
    k, cb, pool = build_pool()
    with open(f'{OUT}/pool.json', 'w') as f:
        json.dump(pool, f, indent=1)
    print(len(pool), 'coins in pool', flush=True)
    which = sys.argv[1] if len(sys.argv) > 1 else 'both'
    for i, r in enumerate(pool):
        c = r['coin']
        if which in ('both', 'kraken') and not os.path.exists(f'{OUT}/kr_{c}_1d.csv'):
            try:
                o = k.fetch_ohlcv(r['kraken'], '1d', limit=720)
                pd.DataFrame(o, columns=COLS).to_csv(f'{OUT}/kr_{c}_1d.csv', index=False)
            except Exception as e:  # noqa: BLE001 - public data
                print('kraken fail', c, e, flush=True)
        if which in ('both', 'coinbase') and not os.path.exists(f'{OUT}/cb_{c}_1d.csv'):
            rows, t, now = {}, cb.parse8601('2022-01-01T00:00:00Z'), cb.milliseconds()
            while t < now:
                for _ in range(4):
                    try:
                        batch = cb.fetch_ohlcv(r['coinbase'], '1d', since=t, limit=300)
                        break
                    except Exception as e:  # noqa: BLE001
                        print('retry', c, e, flush=True)
                        time.sleep(2)
                else:
                    batch = []
                for x in batch:
                    rows[x[0]] = x
                t += 300 * 86_400_000
            pd.DataFrame([rows[x] for x in sorted(rows)], columns=COLS).to_csv(f'{OUT}/cb_{c}_1d.csv', index=False)
        print(i, c, flush=True)


if __name__ == '__main__':
    main()
