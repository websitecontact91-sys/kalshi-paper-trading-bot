
"""Kalshi 15-minute multi-market paper simulator; NEVER places real orders.
All discovered series are scanned and classified. Only series with verified
settlement-reference feeds can be evaluated; unsupported ones fail closed.
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
from market_coverage import classification
from kalshi_pyth import KalshiPythPrices, PYTH_INDEX_IDS

LOG = logging.getLogger('paper_runner')
BASE = 'https://api.elections.kalshi.com/trade-api/v2'
STATE_FILE = Path('paper_state.json')
CF_INDEX_IDS = {
    'KXBTC15M': 'BRTI',
    'KXETH15M': 'ETHUSD_RTI',
    'KXSOL15M': 'SOLUSD_RTI',
    'KXHYPE15M': 'HYPEUSD_RTI',
    'KXADA15M': 'ADAUSD_RTI',
    'KXBCH15M': 'BCHUSD_RTI',
    'KXBNB15M': 'BNBUSD_RTI',
    'KXDOGE15M': 'DOGEUSD_RTI',
    'KXNEAR15M': 'NEARUSD_RTI',
    'KXXRP15M': 'XRPUSD_RTI',
    'KXZEC15M': 'ZECUSD_RTI',
}
TRADE_NOTIONAL = 5.0  # Exact dollar-denominated paper stake; never places real orders.


def parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def default_state():
    return {'mode': 'SCANNING', 'balance': 1000.0, 'trade': None,
            'history': [], 'version': 1}


def load_state(path=STATE_FILE):
    if not path.exists():
        return default_state()
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('mode') not in ('SCANNING', 'WAITING_FOR_SETTLEMENT', 'STOPPED'):
        raise ValueError('Invalid saved mode')
    if state['mode'] == 'WAITING_FOR_SETTLEMENT' and not state.get('trade'):
        raise ValueError('Pending paper trade missing')
    if state['mode'] != 'WAITING_FOR_SETTLEMENT' and state.get('trade'):
        raise ValueError('Unexpected pending paper trade')
    if not isinstance(state.get('history'), list):
        raise ValueError('Invalid history')
    if any(t.get('won') is False for t in state['history']) and state['mode'] != 'STOPPED':
        raise ValueError('Prior loss requires permanent STOPPED mode')
    balance = state.get('balance')
    if not isinstance(balance, (float, int)) or not math.isfinite(balance) or balance < 0:
        raise ValueError('Invalid balance')
    return state


def persist(state, path=STATE_FILE, push=False):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)
    if push:
        subprocess.run(['git', 'add', '--', str(path)], check=True)
        diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], check=False)
        if diff.returncode == 1:
            subprocess.run(['git', '-c', 'user.name=Kalshi Paper Bot',
                            '-c', 'user.email=paper-bot@users.noreply.github.com',
                            'commit', '-m', 'Save simulated Kalshi trading state'], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=True)
        elif diff.returncode != 0:
            raise RuntimeError('Cannot verify saved state')


def discover(client):
    series, catalog = discover_series(client, max_pages=10)
    if not catalog.get('series_complete'):
        raise RuntimeError('Incomplete series catalog: ' + str(catalog))
    stats = {}
    markets = discover_open(client, max_pages=10, page_size=200,
                            series_tickers=series, stats=stats)
    if stats.get('failed_series') or stats.get('truncated_series'):
        raise RuntimeError('Incomplete market discovery: ' + str(stats))
    groups = {}
    for m in markets:
        name = m.get('series_ticker') or m.get('ticker', '').split('-')[0]
        groups.setdefault(classification(name), set()).add(name)
    LOG.info('DISCOVERY series=%d markets=%d', len(series), len(markets))
    for kind, names in sorted(groups.items()):
        LOG.info('COVERAGE %s series=%d names=%s',
                 kind, len(names), ', '.join(sorted(names)))
    return markets


class KalshiCFPrices:
    def __init__(self):
        key_id = os.environ.get('KALSHI_API_KEY_ID', '')
        pem = os.environ.get('KALSHI_PRIVATE_KEY', '')
        if not key_id or not pem:
            raise RuntimeError('Kalshi price-feed GitHub secrets missing')
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)
        self.session = requests.Session()
        self.cache = {}

    def price(self, series):
        index_id = CF_INDEX_IDS[series]
        cached = self.cache.get(index_id)
        if cached and -5 <= time.time() - cached[1] <= 8:
            return cached
        path = '/trade-api/v2/cfbenchmarks/values'
        stamp = str(int(time.time() * 1000))
        message = (stamp + 'GET' + path).encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            signature = self.key.sign(message)
        else:
            signature = self.key.sign(message, padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        response = self.session.get('https://external-api.kalshi.com' + path,
                                    params={'id': index_id}, headers={
                                        'KALSHI-ACCESS-KEY': self.key_id,
                                        'KALSHI-ACCESS-TIMESTAMP': stamp,
                                        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode(),
                                    }, timeout=12)
        response.raise_for_status()
        payload = response.json().get('data', {}).get('payload')
        if not isinstance(payload, list):
            raise ValueError('Invalid CF response for ' + index_id)
        valid = []
        for point in payload:
            try:
                px = float(point['value'])
                raw_ts = float(point['time'])
                ts = raw_ts / 1000 if raw_ts > 1e11 else raw_ts
                if math.isfinite(px) and px > 0 and math.isfinite(ts):
                    valid.append((ts, px))
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
        if not valid:
            raise ValueError('No valid CF observations for ' + index_id)
        ts, px = max(valid)
        if not -5 <= time.time() - ts <= 15:
            raise ValueError('Stale CF observation for ' + index_id)
        self.cache[index_id] = (px, ts)
        return px, ts


def round_id(close_time):
    """Canonical UTC 15-minute closing bucket across all market series.

    A market closing exactly on a quarter-hour belongs to that quarter-hour;
    nearby close timestamps in the same bucket share one global trade slot.
    """
    stamp = parse_time(close_time)
    if stamp is None or not math.isfinite(stamp):
        raise ValueError('Invalid round close time')
    return str(math.ceil(stamp / 900))


def prior_round_matches(trade, close_time):
    """Support both legacy ISO close_time and new numeric round identifiers.

    Older saved paper_state.json entries used raw ISO timestamps as round_id.
    Never allow that older format to bypass the one-trade-per-round rule.
    """
    raw = trade.get('round_id') or trade.get('close_time')
    if raw is None:
        return False
    try:
        return round_id(raw) == round_id(close_time) if isinstance(raw, str) and ('T' in raw or '-' in raw) else str(raw) == round_id(close_time)
    except (ValueError, TypeError, OverflowError):
        return False


def candidate_is_fresh(candidate, now, max_age=15):
    """Reject stale reference quotes before simulated execution."""
    ts = candidate.get('index_time')
    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return False
    return -5 <= now - ts <= max_age


def choose(candidates):
    if not candidates:
        return None
    return sorted(candidates, key=lambda c: (-c['distance_pct'],
                  -c['gross_payout'], c['ticker'], c['side']))[0]


def fee_inclusive_quote(ask, fee_multiplier):
    """Estimate $5 dollar-ticket payout from published Kalshi taker fee formula.

    Fractional dollar exposure is PAPER ONLY. Round fees to cents per ticket.
    A Kalshi on-screen quote may differ due to rounding, spread or fee rules.
    """
    if not (0 < ask < 1) or not math.isfinite(fee_multiplier) or fee_multiplier < 0:
        raise ValueError('Invalid quote inputs')
    # Solve the whole-ticket budget constraint with fee rounded UP to cents.
    # Round down exposure to cents of contract payout, never spend > $5.
    for payout_cents in range(int(TRADE_NOTIONAL / ask * 100), 0, -1):
        payout = payout_cents / 100
        fee = math.ceil((0.07 * fee_multiplier * payout * ask * (1 - ask)) * 100 - 1e-10) / 100
        if payout * ask + fee <= TRADE_NOTIONAL + 1e-9:
            return payout, fee
    raise ValueError('No affordable paper payout')


def series_fee_multiplier(client, series):
    """Fail closed if Kalshi does not return fee parameters for this series."""
    from urllib.parse import quote
    payload = client.get('/series/' + quote(series, safe=''))
    data = payload.get('series', {})
    raw = data.get('fee_multiplier')
    if raw is None:
        raise ValueError('Missing live Kalshi fee_multiplier for ' + series)
    multiplier = float(raw)
    if not math.isfinite(multiplier) or multiplier < 0:
        raise ValueError('Invalid live Kalshi fee multiplier for ' + series)
    return multiplier


def evaluate_market(m, px, current_time, book, balance):
    try:
        target = float(m['floor_strike'])
        close = parse_time(m['close_time'])
        if close is None or not 0 < close - current_time <= 60:
            return []
        if target <= 0 or not math.isfinite(px) or px <= 0 or m.get('status') not in ('open', 'active'):
            return []
        if m.get('strike_type') != 'greater_or_equal':
            return []
        options = []
        for side in ('YES', 'NO'):
            ask = implied_ask(m, side)
            if ask is None or not 0.80 <= ask <= 0.90:
                continue
            fee_multiplier = m.get('_kalshi_series_fee_multiplier')
            if fee_multiplier is None:
                continue
            payout, fee = fee_inclusive_quote(ask, float(fee_multiplier))
            gross = payout / TRADE_NOTIONAL
            if not 1.10 <= gross <= 1.20:
                continue
            depth = best_bid_depth(book, side, ask)
            if depth < math.ceil(payout):
                continue
            # Fixed $5 paper notional with a conservative, fee-inclusive quote.
            # This is a simulation, not an executable order or UI guarantee.
            if balance + 1e-8 < TRADE_NOTIONAL:
                continue
            count = payout
            cost = TRADE_NOTIONAL
            signed = px - target if side == 'YES' else target - px
            if signed <= 0:
                continue  # A negative distance means this side is currently losing.
            distance = 100 * signed / target
            options.append({'ticker': m['ticker'], 'side': side, 'ask': ask,
                            'payout_source': 'kalshi_published_fee_estimate',
                            'gross_payout': gross, 'distance_pct': distance,
                            'target': target, 'underlying': px, 'count': count,
                            'cost': cost, 'fee_reserve': fee,
                            'fee_multiplier': float(fee_multiplier),
                            'close_time': m['close_time'], 'round_id': round_id(m['close_time'])})
        return options
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return []


def settle(state, client):
    trade = state['trade']
    if not trade:
        raise RuntimeError('Missing pending paper trade')
    market = client.market(trade['ticker'])
    # Never infer settlement from a price quote or a closed trading window.
    # The official market result is the sole authority for simulated P&L.
    result = str(market.get('result') or '').strip().lower()
    if result not in ('yes', 'no'):
        LOG.info('WAITING FOR OFFICIAL RESULT %s status=%s result=%r',
                 trade['ticker'], market.get('status'), result)
        return False
    won = result == trade['side'].lower()
    if won and trade.get('payout_source') and trade['payout_source'] != 'kalshi_published_fee_estimate':
        raise ValueError('Unverified paper payout source')
    payout = round(trade['cost'] * trade['gross_payout'], 8) if won and trade.get('payout_source') == 'kalshi_published_fee_estimate' else (round(trade['count'], 8) if won and 'payout_source' not in trade else 0.0)
    trade.update(result=result, won=won,
                 pnl=round(payout - trade['cost'], 8), settled_at=time.time())
    state['balance'] = round(state['balance'] + payout, 8)
    state['history'].append(trade)
    state['trade'] = None
    state['mode'] = 'SCANNING' if won else 'STOPPED'
    LOG.warning('SETTLED %s won=%s mode=%s', trade['ticker'], won, state['mode'])
    return True


def loop(args):
    client = Kalshi(BASE)
    state = load_state()
    if state['mode'] == 'STOPPED':
        LOG.error('PERMANENT STOP: previous simulated loss')
        return
    if args.paper:
        if not args.persist_to_github or not os.getenv('GITHUB_ACTIONS'):
            raise RuntimeError('Paper mode requires GitHub Actions and state persistence')
        if not STATE_FILE.exists():
            persist(state, push=True)
    cf = KalshiCFPrices()
    pyth = KalshiPythPrices()
    started = time.monotonic()
    cached_markets = []
    next_discovery = 0.0
    fee_cache = {}
    LOG.warning('REFERENCE COVERAGE mapped_cf=%d mapped_pyth=%d excluded_unmapped=5', len(CF_INDEX_IDS), len(PYTH_INDEX_IDS))
    LOG.warning('NO REAL ORDERS. Mode=%s; fixed $%.2f paper notional; dollar-denominated simulated exposure; published taker fee estimate included',
                'PAPER' if args.paper else 'READ ONLY', TRADE_NOTIONAL)
    while time.monotonic() - started < args.duration:
        tick = time.monotonic()
        if state['mode'] == 'WAITING_FOR_SETTLEMENT':
            if args.paper:
                if settle(state, client):
                    persist(state, push=True)
                    if state['mode'] == 'STOPPED':
                        return
            else:
                LOG.info('READ ONLY: pending paper trade exists; settlement not changed')
        else:
            # A full /series + per-series market crawl is too slow to repeat
            # every two seconds. Cache discovery for 60s, but refresh market
            # details and orderbooks immediately before considering an entry.
            if time.monotonic() >= next_discovery:
                cached_markets = discover(client)
                next_discovery = time.monotonic() + 60
            markets = cached_markets
            candidates = []
            price_cache = {}
            unsupported = {}
            scan_counts = {'final_minute': 0, 'mapped': 0, 'reference_ok': 0, 'evaluated': 0, 'qualifying': 0}
            for m in markets:
                close = parse_time(m.get('close_time'))
                if close is None or not 0 < close - time.time() <= 60:
                    continue
                scan_counts['final_minute'] += 1
                if any(prior_round_matches(t, m['close_time']) for t in state['history']):
                    continue
                if state.get('trade') and str(state['trade'].get('round_id')) == round_id(m['close_time']):
                    continue
                series = m.get('series_ticker') or m['ticker'].split('-')[0]
                if series not in CF_INDEX_IDS and series not in PYTH_INDEX_IDS:
                    unsupported[series] = classification(series)
                    continue
                scan_counts['mapped'] += 1
                try:
                    if series not in price_cache:
                        price_cache[series] = (cf.price(series) if series in CF_INDEX_IDS else pyth.price(series))
                    px, ts = price_cache[series]
                    LOG.debug('REFERENCE_OK series=%s value=%s age=%.2fs', series, px, time.time()-ts)
                    scan_counts['reference_ok'] += 1
                    fresh = client.market(m['ticker'])
                    book = client.orderbook(m['ticker'])
                    scan_counts['evaluated'] += 1
                    if series not in fee_cache:
                        fee_cache[series] = series_fee_multiplier(client, series)
                    fresh['_kalshi_series_fee_multiplier'] = fee_cache[series]
                    options = evaluate_market(fresh, px, time.time(), book, state['balance'])
                    scan_counts['qualifying'] += len(options)
                    if not options:
                        LOG.info('ENTRY_FILTERED series=%s ticker=%s status=%s strike_type=%s strike=%s yes_ask=%s no_ask=%s seconds_left=%.1f', series, m.get('ticker'), fresh.get('status'), fresh.get('strike_type'), fresh.get('floor_strike'), fresh.get('yes_ask_dollars'), fresh.get('no_ask_dollars'), (parse_time(fresh.get('close_time')) or 0)-time.time())
                    for candidate in options:
                        candidate['index_time'] = ts
                        candidates.append(candidate)
                except Exception as exc:
                    LOG.warning('CANNOT EVALUATE %s: %s', m.get('ticker'), exc)
            if unsupported:
                LOG.info('FINAL-MINUTE UNSUPPORTED %s',
                         ', '.join('%s:%s' % pair for pair in sorted(unsupported.items())))
            candidates = [c for c in candidates if candidate_is_fresh(c, time.time())]
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
                    LOG.warning('PAPER ENTRY SAVED %s', selected['ticker'])
                else:
                    LOG.info('READ ONLY: simulated entry disabled')
            else:
                LOG.info('SCAN discovered_markets=%d candidates=0 mode=%s stages=%s',
                         len(markets), state['mode'], scan_counts)
        time.sleep(max(0, args.interval - (time.monotonic() - tick)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--duration', type=int, default=240)
    parser.add_argument('--paper', action='store_true')
    parser.add_argument('--persist-to-github', action='store_true')
    args = parser.parse_args()
    if args.interval < 2 or not 1 <= args.duration <= 600:
        parser.error('interval must be >= 2; duration must be 1-600 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    loop(args)


if __name__ == '__main__':
    main()
