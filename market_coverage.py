"""Read-only coverage report for all discovered Kalshi 15-minute series.

Never submits orders, writes paper state, or assumes unsupported price feeds work.
Run: python -u market_coverage.py
"""
import collections
import json

from bot import Kalshi
from discover import BASE, discover_open, discover_series

CF = {
    "KXBTC15M": "BRTI",
    "KXETH15M": "ETHUSD_RTI",
    "KXSOL15M": "SOLUSD_RTI",
    "KXHYPE15M": "HYPEUSD_RTI",
}
PYTH_UNVERIFIED = {
    "KXGOLD15M": "Metal.Index.1OZGOLD/USD",
    "KXSILVER15M": "Metal.Index.SILVER/USD",
    "KXBRENT15M": "Commodities.Index.BRENT/USD",
    "KXEURUSD15M": "FX.EUR/USD",
    "KXGBPUSD15M": "FX.GBP/USD",
}


def classification(series):
    if series in CF:
        return "CF_ENDPOINT_TESTED"
    if series in PYTH_UNVERIFIED:
        return "PYTH_UNAVAILABLE_OR_UNVERIFIED"
    return "NO_VERIFIED_SETTLEMENT_FEED"


def main():
    client = Kalshi(BASE)
    series, catalog = discover_series(client, max_pages=50)
    stats = {}
    markets = discover_open(client, max_pages=10, page_size=200,
                            series_tickers=series, stats=stats)
    by_series = collections.defaultdict(list)
    for market in markets:
        series_name = market.get("series_ticker") or market["ticker"].split("-")[0]
        by_series[series_name].append(market)

    report = {
        "read_only": True,
        "series_discovered": len(series),
        "series_catalog": catalog,
        "open_markets_discovered": len(markets),
        "market_discovery": {
            "failed_series": stats.get("failed_series"),
            "truncated_series": stats.get("truncated_series"),
        },
        "series": [],
    }
    for name in series:
        item = {
            "series": name,
            "open_markets": len(by_series[name]),
            "classification": classification(name),
            "reference": CF.get(name) or PYTH_UNVERIFIED.get(name),
            "sample_ticker": by_series[name][0].get("ticker") if by_series[name] else None,
        }
        report["series"].append(item)

    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    if not catalog["series_complete"] or stats["failed_series"] or stats["truncated_series"]:
        print("WARNING: Discovery incomplete. Counts are lower bounds, not full coverage.",
              flush=True)


if __name__ == "__main__":
    main()
