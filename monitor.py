
"""Automatic 15-minute market discovery and price screening. PAPER/READ ONLY."""
import argparse
import logging
import time
from bot import Kalshi, implied_ask
from discover import BASE, discover_open, discover_series, parse_utc

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('monitor')


def screen(markets, now=None):
    """Screen market prices; these are NOT executable trading candidates."""
    t = time.time() if now is None else now
    results = []
    for m in markets:
        remaining = (parse_utc(m.get('close_time')) or 0) - t
        if not 0 < remaining <= 900:
            continue
        for side in ('YES', 'NO'):
            try:
                ask = implied_ask(m, side)
            except (ValueError, TypeError):
                continue
            if ask is None or not 0.80 <= ask <= 0.90:
                continue
            gross = 1.0 / ask
            if not 1.10 <= gross <= 1.20:
                continue
            results.append((m, side, ask, gross, remaining))
    return results


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--once', action='store_true')
    p.add_argument('--interval', type=float, default=2)
    p.add_argument('--duration', type=int, default=300)
    p.add_argument('--catalog-refresh', type=int, default=300)
    args = p.parse_args(argv)
    if args.interval < 2 or args.duration < 0 or args.catalog_refresh < 30:
        p.error('interval >=2, duration >=0, catalog-refresh >=30 required')
    client = Kalshi(BASE)
    started = time.monotonic()
    next_catalog = 0
    series = []
    iteration = 0
    while True:
        if args.duration and time.monotonic() - started >= args.duration and iteration:
            break
        tick = time.monotonic()
        try:
            if tick >= next_catalog:
                series, catalog = discover_series(client, max_pages=10)
                log.info('SERIES count=%d complete=%s error=%s',
                         len(series), catalog['series_complete'], catalog['series_error'])
                next_catalog = tick + args.catalog_refresh
            stats = {}
            markets = discover_open(client, max_pages=5, series_tickers=series, stats=stats)
            screened = screen(markets)
            log.info('SCAN %d markets=%d price_matches=%d failures=%d truncated=%d READ_ONLY',
                     iteration + 1, len(markets), len(screened),
                     len(stats['failed_series']), len(stats['truncated_series']))
            for m, side, ask, gross, remaining in screened:
                log.info('PRICE MATCH %s %s ask=%.3f gross=%.3fx seconds_left=%.1f strike=%s; NO ENTRY (target/source unverified)',
                         m.get('ticker'), side, ask, gross, remaining,
                         m.get('floor_strike', 'UNVERIFIED'))
            if not screened:
                log.info('No markets currently meet the contract-price and gross-payout screen')
        except Exception:
            log.exception('SCAN ERROR; no trades')
        iteration += 1
        if args.once:
            break
        elapsed = time.monotonic() - tick
        if elapsed > args.interval:
            log.warning('Scan took %.1fs; 2-second polling is best effort', elapsed)
        time.sleep(max(0, args.interval - elapsed))
    log.info('MONITOR COMPLETE scans=%d', iteration)


if __name__ == '__main__':
    main()
