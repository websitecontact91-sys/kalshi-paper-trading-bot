
"""Read-only inspection of Kalshi 15-minute crypto market contracts.
No real orders, simulated entries, or changes to saved state.
"""
import json
import time
from bot import Kalshi

BASE = 'https://api.elections.kalshi.com/trade-api/v2'

SERIES = (
    'KXBTC15M',
    'KXETH15M',
    'KXSOL15M',
    'KXHYPE15M',
)

FIELDS = (
    'ticker',
    'series_ticker',
    'title',
    'subtitle',
    'yes_sub_title',
    'no_sub_title',
    'rules_primary',
    'rules_secondary',
    'status',
    'strike_type',
    'floor_strike',
    'cap_strike',
    'open_time',
    'close_time',
    'expiration_time',
    'expected_expiration_time',
    'yes_ask_dollars',
    'no_ask_dollars',
    'result',
)


def main():
    client = Kalshi(BASE)

    print('READ-ONLY MARKET RULES AUDIT')
    print('No orders or paper entries will be created.')

    total = 0

    for series in SERIES:
        print('\n' + '=' * 65)
        print('SERIES:', series)

        try:
            data = client.get('/markets', {
                'series_ticker': series,
                'status': 'open',
                'limit': 20,
            })

            markets = data.get('markets', [])
            print('OPEN MARKETS RETURNED:', len(markets))

            if not markets:
                print('No open market available to inspect.')
                continue

            markets.sort(key=lambda m: m.get('close_time', ''))

            for market in markets[:2]:
                ticker = market['ticker']
                detail = client.market(ticker)

                print('\nMARKET DETAILS:')
                selected = {
                    field: detail.get(field)
                    for field in FIELDS
                    if field in detail
                }
                print(json.dumps(selected, indent=2, default=str))

                print('ADDITIONAL FIELD NAMES:')
                print(', '.join(sorted(detail.keys())))

                total += 1

        except Exception as exc:
            print('AUDIT ERROR:', type(exc).__name__, str(exc))

        time.sleep(0.2)

    print('\nAUDIT COMPLETE. Markets inspected:', total)


if __name__ == '__main__':
    main()
