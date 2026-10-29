
"""Read-only diagnostics for Kalshi 15-minute markets. No orders."""
import base64
import collections
import math
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from bot import Kalshi
from discover import discover_series, discover_open

BASE = 'https://api.elections.kalshi.com/trade-api/v2'

CF = {
    'KXBTC15M': 'BRTI',
    'KXETH15M': 'ETHUSD_RTI',
    'KXSOL15M': 'SOLUSD_RTI',
    'KXHYPE15M': 'HYPEUSD_RTI',
}

PYTH = {
    'KXGOLD15M': 'Metal.Index.1OZGOLD/USD',
    'KXSILVER15M': 'Metal.Index.SILVER/USD',
    'KXBRENT15M': 'Commodities.Index.BRENT/USD',
    'KXEURUSD15M': 'FX.EUR/USD',
    'KXGBPUSD15M': 'FX.GBP/USD',
}


def cf_probe(session, key_id, key, series, index_id):
    path = '/trade-api/v2/cfbenchmarks/values'
    stamp = str(int(time.time() * 1000))
    message = (stamp + 'GET' + path).encode()

    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size
            ),
            hashes.SHA256()
        )

    response = session.get(
        'https://external-api.kalshi.com' + path,
        params={'id': index_id},
        headers={
            'KALSHI-ACCESS-KEY': key_id,
            'KALSHI-ACCESS-TIMESTAMP': stamp,
            'KALSHI-ACCESS-SIGNATURE':
                base64.b64encode(signature).decode()
        },
        timeout=12
    )

    if not response.ok:
        print(
            f'CF {series} id={index_id} UNAVAILABLE '
            f'HTTP={response.status_code} '
            f'detail={response.text[:150]!r}',
            flush=True
        )
        return

    payload = response.json().get('data', {}).get('payload', [])
    values = []

    for point in payload if isinstance(payload, list) else []:
        try:
            price = float(point['value'])
            timestamp = float(point['time'])
            if timestamp > 1e11:
                timestamp /= 1000
            if math.isfinite(price) and price > 0:
                values.append((timestamp, price))
        except (KeyError, ValueError, TypeError):
            pass

    if not values:
        print(f'CF {series} UNAVAILABLE no valid prices', flush=True)
        return

    timestamp, price = max(values)
    age = time.time() - timestamp
    status = 'FRESH' if -5 <= age <= 15 else 'STALE'

    print(
        f'CF {series} id={index_id} {status} '
        f'price={price:.8f} age={age:.1f}s',
        flush=True
    )


def pyth_probe(session, series, symbol):
    catalog = session.get(
        'https://hermes.pyth.network/v2/price_feeds',
        params={'query': symbol},
        timeout=12
    )

    if not catalog.ok:
        print(
            f'PYTH {series} UNAVAILABLE '
            f'catalog_HTTP={catalog.status_code}',
            flush=True
        )
        return

    matches = [
        item for item in catalog.json()
        if item.get('attributes', {}).get('symbol') == symbol
    ]

    if len(matches) != 1:
        print(
            f'PYTH {series} UNAVAILABLE '
            f'exact_matches={len(matches)}',
            flush=True
        )
        return

    feed_id = matches[0]['id']

    response = session.get(
        'https://hermes.pyth.network/v2/updates/price/latest',
        params={'ids[]': feed_id},
        timeout=12
    )

    if not response.ok:
        print(
            f'PYTH {series} UNAVAILABLE '
            f'latest_HTTP={response.status_code}',
            flush=True
        )
        return

    parsed = response.json().get('parsed', [])
    matches = [
        item for item in parsed
        if item.get('id', '').lower().removeprefix('0x')
        == feed_id.lower().removeprefix('0x')
    ]

    if len(matches) != 1:
        print(
            f'PYTH {series} UNAVAILABLE '
            f'matching_prices={len(matches)}',
            flush=True
        )
        return

    value = matches[0]['price']
    price = float(value['price']) * 10 ** int(value['expo'])
    age = time.time() - float(value['publish_time'])

    status = (
        'FRESH' if math.isfinite(price)
        and price > 0 and -5 <= age <= 15
        else 'STALE_OR_INVALID'
    )

    print(
        f'PYTH {series} {status} '
        f'price={price:.8f} age={age:.1f}s',
        flush=True
    )
    print(
        f'NOTICE {series}: live Pyth price is not yet '
        f'verified as the official settlement candle close',
        flush=True
    )


def main():
    print('READ ONLY: NO PAPER OR REAL ORDERS', flush=True)

    client = Kalshi(BASE)
    series, catalog = discover_series(client, max_pages=10)

    print(
        f'DISCOVERY series={len(series)} '
        f'catalog_complete={catalog["series_complete"]} '
        f'error={catalog["series_error"]}',
        flush=True
    )

    stats = {}
    markets = discover_open(
        client,
        series_tickers=series,
        max_pages=10,
        stats=stats
    )

    counts = collections.Counter(
        m.get('series_ticker')
        or m.get('ticker', '').split('-')[0]
        for m in markets
    )

    print(
        f'DISCOVERY markets={len(markets)} '
        f'failures={stats["failed_series"]} '
        f'truncated={stats["truncated_series"]}',
        flush=True
    )

    for name, count in sorted(counts.items()):
        feed = (
            'CF' if name in CF
            else 'PYTH' if name in PYTH
            else 'UNMAPPED'
        )
        print(
            f'MARKETS {name} count={count} feed={feed}',
            flush=True
        )

    session = requests.Session()
    key_id = os.environ.get('KALSHI_API_KEY_ID', '')
    pem = os.environ.get('KALSHI_PRIVATE_KEY', '')

    if key_id and pem:
        key = serialization.load_pem_private_key(
            pem.encode(), password=None
        )

        for name, index_id in CF.items():
            try:
                cf_probe(session, key_id, key, name, index_id)
            except Exception as exc:
                print(
                    f'CF {name} ERROR '
                    f'{type(exc).__name__}: {str(exc)[:150]}',
                    flush=True
                )
    else:
        print('CF SKIPPED: GitHub secrets missing', flush=True)

    for name, symbol in PYTH.items():
        try:
            pyth_probe(session, name, symbol)
        except Exception as exc:
            print(
                f'PYTH {name} ERROR '
                f'{type(exc).__name__}: {str(exc)[:150]}',
                flush=True
            )

    print('DIAGNOSTICS COMPLETE', flush=True)


if __name__ == '__main__':
    main()
