
"""Bitcoin-only Kalshi 15-minute PAPER simulator. Never submits orders.

Live index: authenticated Kalshi CF Benchmarks BRTI. Settlement: Kalshi's
published market result. Fees are a conservative *simulation reserve*, not a
verified quote of Kalshi's actual fee schedule.
"""
import argparse
import base64
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from bot import Kalshi, implied_ask, best_bid_depth
from discover import discover_open, discover_series

LOG = logging.getLogger('paper_runner')
BASE = 'https://api.elections.kalshi.com/trade-api/v2'
STATE_FILE = Path('paper_state.json')
CF_INDEX_IDS = {
    'KXBTC15M': 'BRTI',
    'KXETH15M': 'ETHUSD_RTI',
    'KXSOL15M': 'SOLUSD_RTI',
    'KXHYPE15M': 'HYPEUSD_RTI',
}
FEE_RESERVE_PER_SHARE = 0.10  # PAPER estimate, deliberately conservative


def parse_time(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def default_state():
    return {'mode': 'SCANNING', 'balance': 1000.0, 'trade': None,
            'history': [], 'version': 1}


def load_state(path=STATE_FILE):
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('mode') not in ('SCANNING', 'WAITING_FOR_SETTLEMENT', 'STOPPED'):
        raise ValueError('Invalid saved mode; refusing to continue')
    if data['mode'] == 'WAITING_FOR_SETTLEMENT' and not data.get('trade'):
        raise ValueError('Missing pending paper trade; refusing to continue')
    if not isinstance(data.get('history'), list):
        raise ValueError('Invalid paper history')
    if not isinstance(data.get('balance'), (int, float)) or data['balance'] < 0:
        raise ValueError('Invalid paper balance')
    if any(t.get('won') is False for t in data['history']) and data['mode'] != 'STOPPED':
        raise ValueError('Prior loss found: state must remain STOPPED')
    return data


def persist(state, path=STATE_FILE, push=False):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)
    if push:
        subprocess.run(['git', 'add', '--', str(path)], check=True)
        diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], check=False)
        if diff.returncode == 1:
            subprocess.run(['git', '-c', 'user.name=Kalshi Paper Bot',
                            '-c', 'user.email=paper-bot@users.noreply.github.com',
                            'commit', '-m', 'Save simulated Kalshi trading state'], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=True)
        elif diff.returncode != 0:
            raise RuntimeError('Cannot verify state commit')


def discover(client):
    series, catalog = discover_series(client, max_pages=10)
    if not catalog.get('series_complete'):
        raise RuntimeError('Incomplete Kalshi series catalog: ' + str(catalog))
    stats = {}
    markets = discover_open(client, max_pages=10, page_size=200,
                            series_tickers=series, stats=stats)
    if stats.get('failed_series') or stats.get('truncated_series'):
        raise RuntimeError('Incomplete Kalshi market discovery: ' + str(stats))
supported = [
    m for m in markets
    if (m.get('series_ticker') or m.get('ticker', '').split('-')[0])
    in CF_INDEX_IDS
]
LOG.info(
    'DISCOVERY series=%s total_markets=%d supported_crypto_markets=%d',
    stats.get('series_checked'), len(markets), len(supported)
)
return supported


class KalshiCFPrices:
    def __init__(self):
        self.key_id = os.environ.get('KALSHI_API_KEY_ID', '')
        pem = os.environ.get('KALSHI_PRIVATE_KEY', '')
        if not self.key_id or not pem:
            raise RuntimeError('Kalshi API secrets missing from GitHub Actions')
        self.key = serialization.load_pem_private_key(pem.encode('utf-8'), password=None)
        self.session = requests.Session()

    def price(self, series):
    if series not in CF_INDEX_IDS:
        raise ValueError('No verified CF price mapping for ' + series)
    index_id = CF_INDEX_IDS[series]
        path = '/trade-api/v2/cfbenchmarks/values'
        stamp = str(int(time.time() * 1000))
        msg = (stamp + 'GET' + path).encode('utf-8')
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        response = self.session.get('https://external-api.kalshi.com' + path,
            params={'id': index_id}, headers={
                'KALSHI-ACCESS-KEY': self.key_id,
                'KALSHI-ACCESS-TIMESTAMP': stamp,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode('ascii'),
            }, timeout=12)
        response.raise_for_status()
        payload = response.json().get('data', {}).get('payload')
        if not isinstance(payload, list):
            raise ValueError('Unexpected BRTI payload')
        observations = []
        for item in payload:
            try:
                px, raw_ts = float(item['value']), float(item['time'])
                ts = raw_ts / 1000 if raw_ts > 1e11 else raw_ts
                if math.isfinite(px) and px > 0 and math.isfinite(ts):
                    observations.append((ts, px))
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
        if not observations:
            raise ValueError('No valid BRTI values')
        ts, px = max(observations)
        age = time.time() - ts
        if not -5 <= age <= 15:
            raise ValueError('BRTI price too old (age %.1fs)' % age)
        return px, ts


def fee_for(ask, count):
    return round(FEE_RESERVE_PER_SHARE * count, 8)


def choose(candidates):
    return sorted(candidates, key=lambda c: (-c['distance_pct'],
                  -c['gross_payout'], c['ticker'], c['side']))[0] if candidates else None


def evaluate_market(m, px, current_time, book, balance):
    try:
        target = float(m['floor_strike'])
        close = parse_time(m['close_time'])
        remaining = close - current_time
        if not 0 < remaining <= 60 or target <= 0 or m.get('status') != 'open':
            return []
        if m.get('strike_type') != 'greater_or_equal':
            return []
        options = []
        for side in ('YES', 'NO'):
            ask = implied_ask(m, side)
            if ask is None or not 0.80 <= ask <= 0.90:
                continue
            gross = 1 / ask
            if not 1.10 <= gross <= 1.20:
                continue
            depth = best_bid_depth(book, side, ask)
            if depth < 1:
                continue
            budget = min(5.0, balance)
            count = min(int(depth), math.floor((budget + 1e-8) /
                        (ask + FEE_RESERVE_PER_SHARE)))
            if count < 1:
                continue
            fee = fee_for(ask, count)
            cost = round(count * ask + fee, 8)
            if cost > budget + 1e-8:
                continue
            distance = 100 * ((px - target) if side == 'YES' else (target - px)) / target
            options.append({'ticker': m['ticker'], 'side': side, 'ask': ask,
                'gross_payout': gross, 'distance_pct': distance,
                'target': target, 'underlying': px, 'count': count,
                'cost': cost, 'fee_reserve': fee, 'close_time': m['close_time'],
                'round_id': m['close_time']})
        return options
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return []


def settle(state, client):
    trade = state.get('trade')
    if not trade:
        raise RuntimeError('Missing pending trade')
    market = client.market(trade['ticker'])
    result = str(market.get('result') or '').lower()
    if result not in ('yes', 'no'):
        LOG.info('WAITING FOR OFFICIAL SETTLEMENT %s', trade['ticker'])
        return False
    won = result == trade['side'].lower()
    payout = trade['count'] if won else 0.0
    trade.update(result=result, won=won, pnl=round(payout - trade['cost'], 8),
                 settled_at=time.time())
    state['balance'] = round(state['balance'] + payout, 8)
    state['history'].append(trade)
    state['trade'] = None
    state['mode'] = 'SCANNING' if won else 'STOPPED'
    LOG.warning('OFFICIAL SETTLEMENT %s result=%s won=%s mode=%s',
                trade['ticker'], result, won, state['mode'])
    return True


def loop(args):
    client = Kalshi(BASE)
    state = load_state()
    if state['mode'] == 'STOPPED':
        LOG.error('PERMANENT STOP: a previous simulated trade lost')
        return
    if args.paper:
        if not args.persist_to_github or not os.getenv('GITHUB_ACTIONS'):
            raise RuntimeError('Paper mode requires GitHub Actions and --persist-to-github')
        # Persist a brand-new initial state before any paper entry is possible.
        if not STATE_FILE.exists():
            persist(state, push=True)
    cf = KalshiCFPrices()
    started = time.monotonic()
    LOG.warning('PAPER ONLY: no real orders. Fee reserve is an estimate, not a Kalshi quote.')
    while time.monotonic() - started < args.duration:
        tick = time.monotonic()
        if state['mode'] == 'WAITING_FOR_SETTLEMENT':
            if settle(state, client):
                persist(state, push=args.persist_to_github)
                if state['mode'] == 'STOPPED':
                    return
        else:
            markets = discover(client)
            candidates = []
            for m in markets:
                close = parse_time(m.get('close_time'))
                if close is None or not 0 < close - time.time() <= 60:
                    continue
                if any(t.get('round_id') == m['close_time'] for t in state['history']):
                    continue  # Only one paper trade per 15-minute round.
                try:
                    series = m.get('series_ticker') or m['ticker'].split('-')[0]
px, ts = cf.price(series)
                    fresh = client.market(m['ticker'])
                    book = client.orderbook(m['ticker'])
                    options = evaluate_market(fresh, px, time.time(), book, state['balance'])
                    for option in options:
                        option['index_time'] = ts
                    candidates.extend(options)
                except Exception as exc:
                    LOG.warning('CANNOT EVALUATE %s: %s', m['ticker'], exc)
            selected = choose(candidates)
            if selected:
                LOG.info('SELECT %s %s distance=%.5f%% ask=%.3f gross=%.3fx total=%.2f',
                         selected['ticker'], selected['side'], selected['distance_pct'],
                         selected['ask'], selected['gross_payout'], selected['cost'])
                if args.paper:
                    state['trade'] = dict(selected, entered_at=time.time())
                    state['balance'] = round(state['balance'] - selected['cost'], 8)
                    state['mode'] = 'WAITING_FOR_SETTLEMENT'
                    persist(state, push=True)  # No subsequent entry without a durable state.
                    LOG.warning('PAPER ENTRY SAVED %s', selected['ticker'])
                else:
                    LOG.info('READ-ONLY MODE: no simulated entry')
            else:
                LOG.info('SCAN supported_crypto_markets=%d candidates=0 mode=%s',
         len(markets), state['mode'])
        time.sleep(max(0, args.interval - (time.monotonic() - tick)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--interval', type=float, default=2)
    p.add_argument('--duration', type=int, default=240)
    p.add_argument('--paper', action='store_true', help='Enable simulated entries only')
    p.add_argument('--persist-to-github', action='store_true')
    args = p.parse_args()
    if args.interval < 2 or not 1 <= args.duration <= 300:
        p.error('interval must be >= 2; duration must be 1-300 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    loop(args)


if __name__ == '__main__':
    main()
