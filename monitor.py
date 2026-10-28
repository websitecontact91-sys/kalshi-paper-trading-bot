"""Safe cloud-ready Kalshi market discovery monitor: NO simulated or live entries."""
import argparse
import logging
import time
from bot import Kalshi
from discover import discover_open

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('monitor')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    client = Kalshi('https://api.elections.kalshi.com/trade-api/v2')
    while True:
        try:
            markets = discover_open(client)
            log.info('DISCOVERY candidate_count=%d (unverified; no orders)', len(markets))
            for m in markets[:25]:
                log.info('CANDIDATE %s closes=%s title=%s', m.get('ticker'),m.get('close_time'),m.get('title'))
        except Exception as exc:
            log.exception('Discovery failed; no trading: %s',exc)
        if args.once:
            break
        time.sleep(max(10,args.interval))

if __name__ == '__main__':
    main()
