"""Bounded continuous read-only market monitor; NOT the paper-trading engine."""
import argparse
import logging
import time
from bot import Kalshi
from discover import BASE, discover_open, discover_series

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('monitor')

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--once',action='store_true')
    p.add_argument('--interval',type=float,default=2)
    p.add_argument('--duration',type=int,default=300,help='Maximum monitoring seconds, 0 for unlimited')
    p.add_argument('--catalog-refresh',type=int,default=300)
    args=p.parse_args(argv)
    if args.interval < 2 or args.duration < 0 or args.catalog_refresh < 30:
        p.error('interval must be >=2 seconds, duration >=0, catalog-refresh >=30')
    client=Kalshi(BASE)
    started=time.monotonic()
    next_catalog=0
    series=[]
    iteration=0
    while True:
        if args.duration and time.monotonic()-started >= args.duration and iteration:
            break
        tick=time.monotonic()
        if tick>=next_catalog:
            series, catalog=discover_series(client,max_pages=10)
            log.info('SERIES count=%d catalog_complete=%s catalog_error=%s',len(series),catalog['series_complete'],catalog['series_error'])
            next_catalog=tick+args.catalog_refresh
        stats={}
        try:
            markets=discover_open(client,max_pages=5,series_tickers=series,stats=stats)
            log.info('SCAN %d candidate_count=%d series_checked=%d markets_scanned=%d failed_series=%d truncated=%d (READ ONLY)',iteration+1,len(markets),stats['series_checked'],stats['markets_scanned'],len(stats['failed_series']),len(stats['truncated_series']))
            for m in sorted(markets,key=lambda x:x.get('close_time') or '')[:20]:
                remaining=(__import__('discover').parse_utc(m.get('close_time')) or 0)-time.time()
                log.info('CANDIDATE %s seconds_left=%.1f yes_ask=%s no_ask=%s (NO ENTRY)',m.get('ticker'),remaining,m.get('yes_ask_dollars',m.get('yes_ask')),m.get('no_ask_dollars',m.get('no_ask')))
        except Exception:
            log.exception('SCAN ERROR; no trades')
        iteration+=1
        if args.once:
            break
        elapsed=time.monotonic()-tick
        if elapsed>args.interval:
            log.warning('Scan took %.1fs, longer than %.1fs target; polling cannot be guaranteed',elapsed,args.interval)
        time.sleep(max(0,args.interval-elapsed))
    log.info('MONITOR COMPLETE scans=%d elapsed=%.1fs',iteration,time.monotonic()-started)

if __name__=='__main__':
    main()
