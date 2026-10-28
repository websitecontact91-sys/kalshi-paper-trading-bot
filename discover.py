"""Read-only Kalshi 15-minute market candidate discovery.

CLI: python discover.py [--max-pages 25] [--limit 200]
Candidates are NOT verified to settle using any particular reference feed.
"""
import argparse
import datetime as dt
import re
import sys
import time

ASSET_SERIES = re.compile(r'15M(?:IN(?:UTE)?)?(?:-|$)', re.I)
CATEGORY_HINTS = re.compile(r'crypto|bitcoin|ethereum|solana|commodity|gold|silver|oil|brent|forex|currency|fx|exchange rate|hyperliquid|hype|gbp/usd|eur/usd', re.I)
KNOWN_PREFIXES = ('KXBTC', 'KXETH', 'KXSOL', 'KXGOLD', 'KXBRENT', 'KXGBP', 'KXEUR', 'KXHYPE')


def parse_utc(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()


def looks_eligible(m, series_categories=None):
    """Conservative ticker/label candidate filter, not duration proof."""
    series = m.get('series_ticker') or m.get('ticker', '').split('-')[0]
    label = ' '.join(str(m.get(k) or '') for k in ('title', 'subtitle', 'category'))
    category = (series_categories or {}).get(series, '')
    if not ASSET_SERIES.search(series):
        return False
    if not (CATEGORY_HINTS.search(label + ' ' + category) or series.upper().startswith(KNOWN_PREFIXES)):
        return False
    closed = parse_utc(m.get('close_time'))
    return bool(closed and m.get('status') in ('open', 'active'))


def discover_open(kalshi, max_pages=25, page_size=200, clock=time.time, stats=None):
    """Paginate open markets and return likely 15-minute candidates.

    max_pages bounds API usage. stats indicates when discovery is incomplete.
    """
    cursor = None
    found = {}
    pages = 0
    total = 0
    exhausted = False
    for _ in range(max_pages):
        params = {'status': 'open', 'limit': page_size}
        if cursor:
            params['cursor'] = cursor
        data = kalshi.get('/markets', params)
        if not isinstance(data, dict) or not isinstance(data.get('markets'), list):
            raise ValueError('Unexpected Kalshi markets response')
        pages += 1
        markets = data['markets']
        total += len(markets)
        for m in markets:
            if looks_eligible(m) and parse_utc(m.get('close_time')) > clock():
                found[m['ticker']] = m
        next_cursor = data.get('cursor')
        if not next_cursor or next_cursor == cursor:
            exhausted = True
            break
        cursor = next_cursor
    if stats is not None:
        stats.update(pages=pages, markets_scanned=total, candidates=len(found),
                     complete_pagination=exhausted)
    return list(found.values())


def run_cli(argv=None):
    parser = argparse.ArgumentParser(description='Print live Kalshi 15-minute market candidates (read-only)')
    parser.add_argument('--max-pages', type=int, default=25)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--show', type=int, default=50)
    args = parser.parse_args(argv)
    if not (1 <= args.max_pages <= 500 and 1 <= args.limit <= 200 and 0 <= args.show <= 500):
        parser.error('Invalid pagination or display limit')
    from bot import Kalshi
    client = Kalshi('https://api.elections.kalshi.com/trade-api/v2')
    stats = {}
    print('KALSHI PUBLIC MARKET DISCOVERY - READ ONLY', flush=True)
    print('Candidates are unverified; no simulated or real trades are placed.', flush=True)
    try:
        markets = discover_open(client, max_pages=args.max_pages, page_size=args.limit, stats=stats)
    except Exception as exc:
        print(f'ERROR: Could not fetch Kalshi markets: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        return 1
    print(f"Scanned {stats['markets_scanned']} open markets across {stats['pages']} API pages.")
    print(f"Potential 15-minute crypto/commodity/FX markets: {len(markets)}")
    if not stats['complete_pagination']:
        print('WARNING: Pagination limit reached; this is NOT a complete scan of all markets.')
    if not markets:
        print('No candidates found. This does not prove no eligible markets exist; filtering or pagination may miss them.')
    for m in sorted(markets, key=lambda x: x.get('close_time') or '')[:args.show]:
        print(f"MARKET {m.get('ticker')} | closes={m.get('close_time')} | "
              f"YES bid/ask={m.get('yes_bid_dollars', m.get('yes_bid', '?'))}/"
              f"{m.get('yes_ask_dollars', m.get('yes_ask', '?'))} | "
              f"NO bid/ask={m.get('no_bid_dollars', m.get('no_bid', '?'))}/"
              f"{m.get('no_ask_dollars', m.get('no_ask', '?'))} | "
              f"threshold={m.get('floor_strike', m.get('functional_strike', 'UNVERIFIED'))} | "
              f"title={m.get('title', '')}")
    print('NOTE: Displayed thresholds may be absent or unsuitable for settlement. Verify official market rules.')
    return 0


if __name__ == '__main__':
    sys.exit(run_cli())
