"""Read-only coverage report for all discovered Kalshi 15-minute series.

Never submits orders, writes paper state, or assumes unsupported price feeds work.
Run: python -u market_coverage.py
"""
import collections
import json

from bot import Kalshi
from discover import BASE, discover_open, discover_series
from kalshi_pyth import PYTH_INDEX_IDS

CF = {
    "KXBTC15M": "BRTI",
    "KXETH15M": "ETHUSD_RTI",
    "KXSOL15M": "SOLUSD_RTI",
    "KXHYPE15M": "HYPEUSD_RTI",
    "KXADA15M": "ADAUSD_RTI",
    "KXBCH15M": "BCHUSD_RTI",
    "KXBNB15M": "BNBUSD_RTI",
    "KXDOGE15M": "DOGEUSD_RTI",
    "KXNEAR15M": "NEARUSD_RTI",
    "KXXRP15M": "XRPUSD_RTI",
    "KXZEC15M": "ZECUSD_RTI",
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
    if series in PYTH_INDEX_IDS:
        return "KALSHI_PYTH_STREAM_MAPPED_UNVERIFIED_CONTRACT"
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
        details = client.get("/series/" + name).get("series", {})
        sources = details.get("settlement_sources") or []
        item = {
            "series": name,
            "official_settlement_sources": sources,
            "contract_terms_url": details.get("contract_terms_url"),
            "open_markets": len(by_series[name]),
            "classification": classification(name),
            "reference": CF.get(name) or PYTH_INDEX_IDS.get(name),
            "sample_ticker": by_series[name][0].get("ticker") if by_series[name] else None,
        }
        report["series"].append(item)

    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    if not catalog["series_complete"] or stats["failed_series"] or stats["truncated_series"]:
        print("WARNING: Discovery incomplete. Counts are lower bounds, not full coverage.",
              flush=True)


if __name__ == "__main__":
    main()
