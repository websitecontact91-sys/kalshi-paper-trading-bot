"""Read-only targeted Kalshi 15-minute series discovery; never submits orders."""
import argparse
import datetime as dt
import re
import sys
import time

BASE = 'https://api.elections.kalshi.com/trade-api/v2'
SEED_SERIES = ('KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXGOLD15M', 'KXBRENT15M', 'KXGBPUSD15M', 'KXHYPE15M')
ASSET = re.compile(r'crypto|bitcoin|ethereum|solana|commodity|gold|silver|oil|brent|forex|currency|exchange rate|hyperliquid|hype|gbp|eur|btc|eth|sol', re.I)


def parse_utc(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()


def looks_eligible(m, series_categories=None):
    series = m.get('series_ticker') or m.get('ticker', '').split('-')[0]
    if not re.search(r'15M(?:IN(?:UTE)?)?$', series, re.I):
        return False
    text = ' '.join(str(m.get(k) or '') for k in ('title','subtitle','category'))
    text += ' ' + (series_categories or {}).get(series, '')
    return bool((ASSET.search(text) or series.upper() in SEED_SERIES) and m.get('status') in ('open','active') and parse_utc(m.get('close_time')))


def discover_series(client, max_pages=10, limit=200):
    """Catalog discovery is best-effort. Report incomplete pagination explicitly."""
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
        for s in data['series']:
            ticker = str(s.get('ticker') or '')
            label = ' '.join(str(s.get(k) or '') for k in ('title','category','frequency','product_metadata'))
            if re.search(r'15M(?:IN(?:UTE)?)?$', ticker, re.I) and (ASSET.search(ticker + ' ' + label) or ticker in SEED_SERIES):
                series.add(ticker)
        nxt = data.get('cursor')
        if not nxt or nxt == cursor:
            complete = True
            break
        cursor = nxt
    return sorted(series), {'series_pages':pages,'series_complete':complete,'series_error':error}


def discover_open(client, max_pages=10, page_size=200, clock=time.time, stats=None, series_tickers=None):
    """Query each candidate series directly; avoids broad /markets pagination bias."""
    if series_tickers is None:
        series_tickers, catalog = discover_series(client, max_pages=max_pages, limit=page_size)
    else:
        catalog = {'series_pages':0,'series_complete':False,'series_error':None}
    found = {}
    failures = {}
    truncated = []
    total = 0
    for series in series_tickers:
        cursor = None
        for page in range(max_pages):
            params = {'series_ticker':series,'status':'open','limit':page_size}
            if cursor:
                params['cursor'] = cursor
            try:
                data = client.get('/markets',params)
                if not isinstance(data.get('markets'),list):
                    raise ValueError('Unexpected /markets response')
            except Exception as exc:
                failures[series] = f'{type(exc).__name__}: {exc}'
                break
            markets = data['markets']
            total += len(markets)
            for m in markets:
                if looks_eligible(m) and parse_utc(m['close_time']) > clock():
                    found[m['ticker']] = m
            nxt = data.get('cursor')
            if not nxt or nxt == cursor:
                break
            cursor = nxt
            if page == max_pages-1:
                truncated.append(series)
    if stats is not None:
        stats.update(catalog)
        stats.update(series_checked=len(series_tickers),markets_scanned=total,candidates=len(found),failed_series=failures,truncated_series=truncated,
                     complete_pagination=catalog['series_complete'] and not failures and not truncated)
    return list(found.values())


def run_cli(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--max-pages',type=int,default=10)
    p.add_argument('--limit',type=int,default=200)
    p.add_argument('--show',type=int,default=30)
    args=p.parse_args(argv)
    if not 1 <= args.max_pages <= 100 or not 1 <= args.limit <= 200 or not 0 <= args.show <= 500:
        p.error('Invalid limits')
    from bot import Kalshi
    stats={}
    print('KALSHI TARGETED 15-MINUTE DISCOVERY - READ ONLY',flush=True)
    try:
        markets=discover_open(Kalshi(BASE),max_pages=args.max_pages,page_size=args.limit,stats=stats)
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr,flush=True)
        return 1
    print(f"Series checked: {stats['series_checked']} | markets scanned: {stats['markets_scanned']} | candidates: {len(markets)}")
    print(f"Series catalog pages: {stats['series_pages']} | catalog complete: {stats['series_complete']}")
    if stats['series_error']:
        print('WARNING series catalog:',stats['series_error'])
    if stats['failed_series']:
        print('WARNING series API failures:',stats['failed_series'])
    if stats['truncated_series']:
        print('WARNING truncated series:',stats['truncated_series'])
    if not stats['complete_pagination']:
        print('WARNING: Coverage NOT complete; no candidates does not imply no active markets.')
    for m in sorted(markets,key=lambda x:x.get('close_time') or '')[:args.show]:
        print(f"MARKET {m.get('ticker')} closes={m.get('close_time')} YES_ask={m.get('yes_ask_dollars',m.get('yes_ask','?'))} NO_ask={m.get('no_ask_dollars',m.get('no_ask','?'))} strike={m.get('floor_strike','UNVERIFIED')} title={m.get('title','')}")
    print('READ ONLY: settlement references, market duration and price feeds are NOT verified. No paper or real trades.')
    return 1 if stats['failed_series'] and not markets else 0


if __name__ == '__main__':
    sys.exit(run_cli())
