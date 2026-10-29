"""Read-only Kalshi market-data client. No account or trading endpoints."""
import base64
import json
import os
import re
import threading
import time
from decimal import Decimal
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

BASE = 'https://external-api.kalshi.com/trade-api/v2'
READ_PATH = re.compile(r'^/(?:markets(?:/[A-Za-z0-9_-]+(?:/orderbook)?)?|series(?:/[A-Za-z0-9_-]+)?|events/[A-Za-z0-9_-]+|cfbenchmarks/values)$')


def decimal_value(value):
    if isinstance(value, (float, bool)) or value is None:
        raise ValueError('price/quantity must be an exact string or integer')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('nonfinite number')
    return number


def implied_ask(market, side):
    if side not in ('YES', 'NO'):
        raise ValueError('unknown side')
    key = side.lower() + '_ask_dollars'
    if key in market:
        return decimal_value(market[key])
    value = market.get(side.lower() + '_ask')
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('missing fixed-point ask')
    return Decimal(value) / 100


def ask_and_depth(book, side, reported_ask=None):
    """Derive the executable ask and depth from ONE opposite-bid price level.

    An explicit reported_ask optionally checks a caller's same-snapshot
    expectation. Live pricing omits it because separately fetched market
    summaries and order books need not describe the same price snapshot.
    """
    opposite = 'no' if side == 'YES' else 'yes' if side == 'NO' else None
    if opposite is None:
        raise ValueError('unknown side')
    fp = book.get('orderbook_fp')
    if fp is not None:
        levels = [(decimal_value(p), decimal_value(q)) for p, q in fp.get(opposite + '_dollars', [])]
    else:
        old = book.get('orderbook', book)
        if opposite + '_dollars' in old:
            levels = [(decimal_value(p), decimal_value(q)) for p, q in old[opposite + '_dollars']]
        else:
            levels = [(decimal_value(p) / 100, decimal_value(q)) for p, q in old.get(opposite, [])]
    levels = [(p, q) for p, q in levels if 0 < p < 1 and q > 0]
    if not levels:
        raise ValueError('no_depth')
    best = max(p for p, _ in levels)
    ask = Decimal('1') - best
    if reported_ask is not None and ask != decimal_value(reported_ask):
        raise ValueError('quote_changed')
    return ask, sum((q for p, q in levels if p == best), Decimal('0'))


class Kalshi:
    def __init__(self, base_url=BASE, *, session=None, sleep=time.sleep, timeout=5):
        parsed = urlparse(base_url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('external-api.kalshi.com', 'api.elections.kalshi.com')
                or parsed.path.rstrip('/') != '/trade-api/v2' or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError('only the official Kalshi market API is supported')
        self.base_url, self.sleep, self.timeout = base_url.rstrip('/'), sleep, timeout
        self._session, self._local = session, threading.local()
        self._key_id = os.environ.get('KALSHI_API_KEY_ID', '')
        raw = os.environ.get('KALSHI_PRIVATE_KEY', '')
        self._key = serialization.load_pem_private_key(raw.encode(), password=None) if raw else None

    def _headers(self, path):
        headers = {'Accept': 'application/json'}
        if not (self._key and self._key_id):
            return headers
        stamp = str(int(time.time() * 1000))
        message = (stamp + 'GET/trade-api/v2' + path).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                       salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        headers.update({'KALSHI-ACCESS-KEY': self._key_id, 'KALSHI-ACCESS-TIMESTAMP': stamp,
                        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()})
        return headers

    def get(self, path, params=None):
        if not READ_PATH.fullmatch(path):
            raise ValueError('endpoint is outside the read-only allowlist')
        session = self._session
        if session is None:
            if not hasattr(self._local, 'session'):
                self._local.session = requests.Session()
            session = self._local.session
        for attempt in range(2):
            response = session.get(self.base_url + path, params=params, headers=self._headers(path),
                                   timeout=self.timeout, allow_redirects=False)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    try:
                        delay = min(2.0, max(0.25, float(response.headers.get('Retry-After', '1'))))
                    except ValueError:
                        delay = 1.0
                    self.sleep(delay)
                    continue
            if 300 <= response.status_code < 400:
                raise ValueError('market API redirect refused')
            response.raise_for_status()
            return json.loads(response.text, parse_float=str,
                              parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
        raise RuntimeError('unreachable retry state')

    def market(self, ticker):
        return self.get('/markets/' + ticker)['market']

    def orderbook(self, ticker):
        return self.get('/markets/' + ticker + '/orderbook')

    def series(self, ticker):
        return self.get('/series/' + ticker)['series']

    def event(self, ticker):
        return self.get('/events/' + ticker)['event']

    def refresh_markets(self, tickers):
        if not tickers:
            return {}
        data = self.get('/markets', {'tickers': ','.join(tickers), 'limit': 1000})
        if data.get('cursor'):
            raise ValueError('batch market response truncated')
        return {m['ticker']: m for m in data.get('markets', [])}

    def orderbooks(self, tickers):
        if not tickers:
            return {}
        data = self.get('/markets/orderbooks', {'tickers': list(tickers)})
        return {b['ticker']: b for b in data.get('orderbooks', [])}

    def open_markets(self, series, now):
        from timing import timestamp
        result, cursor, seen = [], None, set()
        for _ in range(10):
            params = {'series_ticker': series, 'status': 'open', 'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            data = self.get('/markets', params)
            for market in data.get('markets', []):
                if market.get('status') in ('open', 'active') and timestamp(market['close_time']) > now:
                    result.append(dict(market, series_ticker=series))
            cursor = data.get('cursor')
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError('repeated market pagination cursor')
            seen.add(cursor)
        raise ValueError('market pagination exceeded safety bound')
