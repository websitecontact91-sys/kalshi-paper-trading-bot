
"""Read-only Kalshi 15-minute market discovery. Never submits orders."""
import argparse
import datetime as dt
import re
import sys
import time

BASE = 'https://api.elections.kalshi.com/trade-api/v2'

SEED_SERIES = (
    'KXBTC15M', 'KXETH15M', 'KXSOL15M',
    'KXGOLD15M', 'KXBRENT15M', 'KXGBPUSD15M',
    'KXHYPE15M', 'KXEURUSD15M', 'KXSILVER15M'
)

SERIES_15M = re.compile(r'15M(?:IN(?:UTE)?)?$', re.I)
TITLE_15M = re.compile(r'\b15\s*(?:min|mins|minute|minutes)\b', re.I)
EXCLUDED = {'KXCRYPTOLEAD15M', 'KXSPORT15M'}


def parse_utc(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return dt.datetime.fromisoformat(
        str(value).replace('Z', '+00:00')
    ).timestamp()


def series_is_15m(series):
    ticker = str(series.get('ticker') or '')
    title = str(series.get('title') or '')
    return (
        ticker.upper() not in EXCLUDED
        and (
            bool(SERIES_15M.search(ticker))
            or bool(TITLE_15M.search(title))
        )
    )


def looks_eligible(m, series_categories=None):
    series = str(
        m.get('series_ticker')
        or m.get('ticker', '').split('-')[0]
    )
    title = ' '.join(
        str(m.get(k) or '')
        for k in ('title', 'subtitle')
    )
    return bool(
        series.upper() not in EXCLUDED
        and (
            SERIES_15M.search(series)
            or TITLE_15M.search(title)
        )
        and m.get('status') in ('open', 'active')
        and parse_utc(m.get('close_time'))
    )


def discover_series(client, max_pages=50, limit=200):
    """Find 15-minute series and report catalog completeness."""
    series = set(SEED_SERIES)
    cursor = None
    complete = False
    pages = 0
    error = None

    for _ in range(max_pages):
        params = {'limit': limit}
        if cursor:
            params['cursor'] = cursor

        try:
            data = client.get('/series', params)
            if not isinstance(data.get('series'), list):
                raise ValueError('Unexpected /series response')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            break

        pages += 1

        for item in data['series']:
            if series_is_15m(item):
                series.add(str(item['ticker']))

        nxt = data.get('cursor')
        if not nxt or nxt == cursor:
            complete = True
            break
        cursor = nxt

    return sorted(series), {
        'series_pages': pages,
        'series_complete': complete,
        'series_error': error
    }


def discover_open(
    client, max_pages=10, page_size=200,
    clock=time.time, stats=None, series_tickers=None
):
    """Query candidate series individually for open markets."""
    if series_tickers is None:
        series_tickers, catalog = discover_series(
            client, max_pages=50, limit=page_size
        )
    else:
        catalog = {
            'series_pages': 0,
            'series_complete': False,
            'series_error': None
        }

    found = {}
    failures = {}
    truncated = []
    total = 0

    for series in series_tickers:
        cursor = None

        for page in range(max_pages):
            params = {
                'series_ticker': series,
                'status': 'open',
                'limit': page_size
            }
            if cursor:
                params['cursor'] = cursor

            try:
                data = client.get('/markets', params)
                if not isinstance(data.get('markets'), list):
                    raise ValueError('Unexpected /markets response')
            except Exception as exc:
                failures[series] = (
                    f'{type(exc).__name__}: {exc}'
                )
                break

            markets = data['markets']
            total += len(markets)

            for m in markets:
                if (
                    looks_eligible(m)
                    and parse_utc(m['close_time']) > clock()
                ):
                    found[m['ticker']] = m

            nxt = data.get('cursor')
            if not nxt or nxt == cursor:
                break

            cursor = nxt

            if page == max_pages - 1:
                truncated.append(series)

    if stats is not None:
        stats.update(catalog)
        stats.update(
            series_checked=len(series_tickers),
            markets_scanned=total,
            candidates=len(found),
            failed_series=failures,
            truncated_series=truncated,
            complete_pagination=(
                catalog['series_complete']
                and not failures
                and not truncated
            )
        )

    return list(found.values())


def run_cli(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--limit', type=int, default=200)
    p.add_argument('--show', type=int, default=30)
    args = p.parse_args(argv)

    if not (
        1 <= args.max_pages <= 100
        and 1 <= args.limit <= 200
        and 0 <= args.show <= 500
    ):
        p.error('Invalid limits')

    from bot import Kalshi

    stats = {}
    print('KALSHI 15-MINUTE DISCOVERY - READ ONLY', flush=True)

    try:
        series, catalog = discover_series(
            Kalshi(BASE),
            max_pages=args.max_pages,
            limit=args.limit
        )
        markets = discover_open(
            Kalshi(BASE),
            max_pages=args.max_pages,
            page_size=args.limit,
            series_tickers=series,
            stats=stats
        )
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
        return 1

    print(
        f"Series checked: {stats['series_checked']} "
        f"| markets scanned: {stats['markets_scanned']} "
        f"| candidates: {len(markets)}"
    )

    print(
        f"Catalog pages: {catalog['series_pages']} "
        f"| complete: {catalog['series_complete']}"
    )

    if catalog['series_error']:
        print('WARNING series catalog:', catalog['series_error'])

    if stats['failed_series']:
        print('WARNING series failures:', stats['failed_series'])

    if stats['truncated_series']:
        print('WARNING truncated series:', stats['truncated_series'])

    if not catalog['series_complete']:
        print('WARNING: Catalog incomplete; some series may be missing.')

    for m in sorted(
        markets,
        key=lambda x: x.get('close_time') or ''
    )[:args.show]:
        print(
            f"MARKET {m.get('ticker')} "
            f"closes={m.get('close_time')} "
            f"YES_ask={m.get('yes_ask_dollars', m.get('yes_ask', '?'))} "
            f"NO_ask={m.get('no_ask_dollars', m.get('no_ask', '?'))} "
            f"strike={m.get('floor_strike', 'UNVERIFIED')} "
            f"title={m.get('title', '')}"
        )

    print(
        'READ ONLY: Duration and settlement references '
        'still require verification.'
    )

    return 1 if stats['failed_series'] and not markets else 0


if __name__ == '__main__':
    sys.exit(run_cli())
