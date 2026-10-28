"""Kalshi paper-only 15-minute bot. No authentication or order submission.

Pyth live spot observations are NOT guaranteed to equal the Pyth candle close used
for settlement. CF Benchmarks RTI markets fail closed until a verified feed exists.
"""
import argparse
import datetime as dt
import json
import logging
import math
import os
import subprocess
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.parse import quote

import requests
from bot import Kalshi, implied_ask, best_bid_depth

LOG = logging.getLogger('paper_runner')
BASE = 'https://api.elections.kalshi.com/trade-api/v2'
STATE_FILE = Path('paper_state.json')
# These are the series and source names confirmed from Kalshi rules on 2026-10-28.
# No feed is assumed to be equivalent to an official settlement observation.
PYTH_SYMBOLS = {
    'KXGOLD15M': 'Metal.Index.1OZGOLD/USD',
    'KXSILVER15M': 'Metal.Index.SILVER/USD',
    'KXBRENT15M': 'Commodities.Index.BRENT/USD',
    'KXEURUSD15M': 'FX.EUR/USD',
    'KXGBPUSD15M': 'FX.GBP/USD',
}
CF_SERIES = {'KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXHYPE15M'}
ALL_SERIES = sorted(set(PYTH_SYMBOLS) | CF_SERIES)


def parse_time(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def default_state():
    return {'mode': 'SCANNING', 'balance': 1000.0, 'trade': None, 'history': []}


def load_state(path=STATE_FILE):
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('mode') not in ('SCANNING', 'WAITING_FOR_SETTLEMENT', 'STOPPED'):
        raise ValueError('Invalid saved mode; refusing to proceed')
    if data['mode'] == 'WAITING_FOR_SETTLEMENT' and not data.get('trade'):
        raise ValueError('Pending trade missing; refusing to proceed')
    return data


def persist(state, path=STATE_FILE, push=False):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)
    if push:
        # Fail closed: state must reach GitHub before the process can continue.
        subprocess.run(['git', 'add', str(path)], check=True)
        result = subprocess.run(['git', 'diff', '--cached', '--quiet'], check=False)
        if result.returncode == 1:
            subprocess.run(['git', '-c', 'user.name=Kalshi Paper Bot',
                            '-c', 'user.email=paper-bot@users.noreply.github.com',
                            'commit', '-m', 'Persist Kalshi paper state'], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=True)
        elif result.returncode != 0:
            raise RuntimeError('Unable to check state changes')


def discover(client):
    # Direct queries avoid broad market pagination; discover.py remains useful for catalog coverage.
    markets = []
    for series in ALL_SERIES:
        data = client.get('/markets', {'series_ticker': series, 'status': 'open', 'limit': 200})
        for m in data.get('markets', []):
            if m.get('ticker') and m.get('status') == 'open':
                markets.append(m)
        if data.get('cursor'):
            raise RuntimeError('Series pagination incomplete: ' + series)
    return markets


class Pyth:
    """Read public Hermes observations, without claiming settlement equivalence."""
    def __init__(self):
        self.session = requests.Session()
        self.ids = {}

    def feed_id(self, symbol):
        if symbol not in self.ids:
            r = self.session.get('https://hermes.pyth.network/v2/price_feeds',
                                 params={'query': symbol}, timeout=12)
            r.raise_for_status()
            matches = [x for x in r.json() if x.get('attributes', {}).get('symbol') == symbol]
            if len(matches) != 1:
                raise ValueError('Exact Pyth feed not uniquely found: ' + symbol)
            self.ids[symbol] = matches[0]['id']
        return self.ids[symbol]

    def price(self, symbol):
        feed_id = self.feed_id(symbol)
        r = self.session.get('https://hermes.pyth.network/v2/updates/price/latest',
                             params={'ids[]': feed_id}, timeout=12)
        r.raise_for_status()
        parsed = r.json().get('parsed', [])
        matching = [x for x in parsed if x.get('id', '').lower().removeprefix('0x') == feed_id.lower().removeprefix('0x')]
        if len(matching) != 1:
            raise ValueError('Pyth observation missing or ambiguous')
        value = matching[0]['price']
        px = float(value['price']) * (10 ** int(value['expo']))
        ts = int(value['publish_time'])
        if not math.isfinite(px) or px <= 0 or not 0 <= time.time()-ts <= 15:
            raise ValueError('Invalid or stale Pyth price')
        return px, ts


def fee_for(ask, count):
    # Conservative per-share paper reserve, not an assertion of Kalshi's actual fees.
    return Decimal('0.10') * count


def choose(candidates):
    if not candidates:
        return None
    return sorted(candidates, key=lambda c: (-c['distance_pct'], -c['gross_payout'], c['ticker'], c['side']))[0]


def evaluate_market(m, px, current_time, book, balance):
    try:
        target = float(m['floor_strike'])
        remaining = parse_time(m['close_time']) - current_time
        if not 5 <= remaining <= 60 or target <= 0 or m.get('status') != 'open':
            return []
        if m.get('strike_type') != 'greater_or_equal':
            return []
        out = []
        for side in ('YES', 'NO'):
            ask = implied_ask(m, side)
            if ask is None or not 0.80 <= ask <= 0.90:
                continue
            gross = 1 / ask
            if not 1.10 <= gross <= 1.20:
                continue
            # Distance is only for ranking. Negative values are permitted.
            distance = 100 * ((px-target) if side == 'YES' else (target-px)) / target
            depth = best_bid_depth(book, side, ask)
            if depth < 1:
                continue
            budget = min(5.0, balance)
            count = min(depth, math.floor((budget + 1e-8) / (ask + 0.10)))
            if count < 1:
                continue
            cost = round(count * ask + float(fee_for(ask, count)), 8)
            if cost > budget + 1e-8:
                continue
            out.append({'ticker': m['ticker'], 'side': side, 'ask': ask,
                        'gross_payout': gross, 'distance_pct': distance,
                        'target': target, 'underlying': px, 'count': count,
                        'cost': cost, 'fee_reserve': float(fee_for(ask, count)),
                        'close_time': m['close_time']})
        return out
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return []


def settle(state, client):
    trade = state['trade']
    if not trade:
        raise RuntimeError('Missing pending trade')
    market = client.market(trade['ticker'])
    result = str(market.get('result') or '').lower()
    if result not in ('yes', 'no'):
        return False
    won = result == trade['side'].lower()
    payout = trade['count'] if won else 0.0
    trade.update(result=result, won=won, pnl=round(payout-trade['cost'], 8),
                 settled_at=time.time())
    state['balance'] = round(state['balance'] + payout, 8)
    state['history'].append(trade)
    state['trade'] = None
    state['mode'] = 'SCANNING' if won else 'STOPPED'
    LOG.warning('SETTLED %s won=%s mode=%s', trade['ticker'], won, state['mode'])
    return True


def loop(args):
    client = Kalshi(BASE)
    pyth = Pyth()
    state = load_state()
    started = time.monotonic()
    if state['mode'] == 'STOPPED':
        LOG.error('PERMANENT STOP: prior simulated loss; no new paper entries')
        return
    if args.paper and not args.persist_to_github:
        raise ValueError('Paper mode requires --persist-to-github for restart-safe stop state')
    if args.paper and not os.getenv('GITHUB_ACTIONS'):
        raise ValueError('Paper mode is restricted to GitHub Actions for persistent state')
    while time.monotonic() - started < args.duration:
        tick = time.monotonic()
        try:
            if state['mode'] == 'WAITING_FOR_SETTLEMENT':
                if settle(state, client):
                    persist(state, push=args.persist_to_github)
            else:
                markets = discover(client)
                LOG.info('SCAN markets=%d (Pyth supported=%d; CF requires verified feed)',
                         len(markets), len(PYTH_SYMBOLS))
                candidates = []
                for m in markets:
                    series = m.get('series_ticker') or m['ticker'].split('-')[0]
                    if series not in PYTH_SYMBOLS:
                        continue
                    remaining = (parse_time(m.get('close_time')) or 0) - time.time()
                    if not 5 <= remaining <= 60:
                        continue
                    try:
                        px, _ = pyth.price(PYTH_SYMBOLS[series])
                        # Re-fetch market and book for a fresh decision.
                        fresh = client.market(m['ticker'])
                        book = client.orderbook(m['ticker'])
                        candidates.extend(evaluate_market(fresh, px, time.time(), book, state['balance']))
                    except Exception as exc:
                        LOG.warning('FEED UNAVAILABLE %s %s', m['ticker'], exc)
                selected = choose(candidates)
                if selected:
                    LOG.info('SELECT %s %s distance=%.5f%% ask=%.3f gross=%.3fx cost=%.2f',
                             selected['ticker'], selected['side'], selected['distance_pct'],
                             selected['ask'], selected['gross_payout'], selected['cost'])
                    if args.paper:
                        state['trade'] = dict(selected, entered_at=time.time())
                        state['balance'] = round(state['balance'] - selected['cost'], 8)
                        state['mode'] = 'WAITING_FOR_SETTLEMENT'
                        persist(state, push=True)
                        LOG.warning('PAPER ENTRY PERSISTED %s', selected['ticker'])
                    else:
                        LOG.info('DRY RUN ONLY: no paper entry')
        except Exception:
            LOG.exception('FAIL CLOSED: stopping this run; state unchanged on GitHub')
            raise
        time.sleep(max(0, args.interval-(time.monotonic()-tick)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--interval', type=float, default=2)
    p.add_argument('--duration', type=int, default=270)
    p.add_argument('--paper', action='store_true', help='Opt-in Pyth-proxy paper mode')
    p.add_argument('--persist-to-github', action='store_true')
    args = p.parse_args()
    if args.interval < 2 or not 1 <= args.duration <= 300:
        p.error('interval must be >=2 and duration between 1 and 300')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    LOG.warning('NO REAL ORDERS. Pyth live prices may differ from official candle settlement values.')
    loop(args)


if __name__ == '__main__':
    main()
