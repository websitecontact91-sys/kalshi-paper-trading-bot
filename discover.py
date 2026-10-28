"""Read-only discovery of Kalshi 15-minute markets.

Discovery is intentionally broader than execution eligibility. Never infer
settlement compatibility or a verified oracle from a ticker or title.
"""
import datetime as dt
import re
import time

ASSET_SERIES = re.compile(r'(?:15M|15MIN|15MINUTE)', re.I)
CATEGORY_HINTS = re.compile(r'crypto|bitcoin|ethereum|solana|commodity|gold|silver|oil|brent|forex|currency|fx|exchange rate', re.I)


def parse_utc(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def looks_eligible(m, series_categories=None):
    """Conservative discovery filter; NOT proof of settlement suitability."""
    series = m.get('series_ticker') or m.get('ticker', '').split('-')[0]
    label = ' '.join(str(m.get(k) or '') for k in ('title', 'subtitle', 'category'))
    category = (series_categories or {}).get(series, '')
    if not ASSET_SERIES.search(series):
        return False
    if not (CATEGORY_HINTS.search(label + ' ' + category) or series.upper().startswith(('KXBTC','KXETH','KXSOL','KXGOLD','KXBRENT','KXGBP','KXEUR','KXHYPE'))):
        return False
    opened = parse_utc(m.get('open_time'))
    closed = parse_utc(m.get('close_time'))
    # The Kalshi market open_time can be listing time, not interval start.
    # Thus duration verification must come from authoritative series rules.
    return bool(closed and m.get('status') == 'open')


def discover_open(kalshi, max_pages=25, page_size=200, clock=time.time):
    """Paginate public markets endpoint and return plausible candidates.

    Note: Kalshi does not guarantee a 15m market can be identified from
    open_time/close_time alone; results require separate rule verification.
    """
    cursor = None
    found = {}
    for _ in range(max_pages):
        params = {'status': 'open', 'limit': page_size}
        if cursor:
            params['cursor'] = cursor
        data = kalshi.get('/markets', params)
        for m in data.get('markets', []):
            if looks_eligible(m) and parse_utc(m['close_time']) > clock():
                found[m['ticker']] = m
        next_cursor = data.get('cursor')
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return list(found.values())
