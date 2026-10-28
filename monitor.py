
"""Kalshi 15-minute monitor with settlement-source diagnostics.
READ ONLY. Never places paper or real trades.
"""

import argparse
import json
import logging
import time
from urllib.parse import quote

from bot import Kalshi, implied_ask
from discover import BASE, discover_open, discover_series, parse_utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("monitor")


def eligible_market(m):
    """Identify simple price-direction markets with a target."""
    series = str(
        m.get("series_ticker")
        or m.get("ticker", "").split("-")[0]
    ).upper()

    if "LEAD" in series:
        return False

    allowed = (
        "KXBTC15M",
        "KXETH15M",
        "KXSOL15M",
        "KXGOLD15M",
        "KXBRENT15M",
        "KXGBPUSD15M",
        "KXEURUSD15M",
        "KXSILVER15M",
        "KXHYPE15M",
    )

    if series not in allowed:
        return False

    try:
        return float(m["floor_strike"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def screen(markets, now=None):
    """Screen contract prices without making entries."""
    t = time.time() if now is None else now
    results = []

    for m in markets:
        if not eligible_market(m):
            continue

        remaining = (parse_utc(m.get("close_time")) or 0) - t

        if not 0 < remaining <= 900:
            continue

        for side in ("YES", "NO"):
            try:
                ask = implied_ask(m, side)
            except (ValueError, TypeError):
                continue

            if ask is None or not 0.80 <= ask <= 0.90:
                continue

            gross = 1.0 / ask

            if not 1.10 <= gross <= 1.20:
                continue

            results.append({
                "ticker": m["ticker"],
                "side": side,
                "ask": ask,
                "gross": gross,
                "target": float(m["floor_strike"]),
                "seconds_left": remaining,
            })

    return results


def inspect_settlement_sources(client, markets):
    """Retrieve official Kalshi metadata once per series."""
    examples = {}

    for m in markets:
        if not eligible_market(m):
            continue

        series = str(
            m.get("series_ticker")
            or m.get("ticker", "").split("-")[0]
        ).upper()

        if series not in examples:
            examples[series] = m["ticker"]

    for series, ticker in sorted(examples.items()):
        try:
            series_data = client.get(
                "/series/" + quote(series, safe="")
            ).get("series", {})

            market_data = client.market(ticker)

            sources = series_data.get("settlement_sources")
            primary = market_data.get("rules_primary")
            secondary = market_data.get("rules_secondary")
            strike_type = market_data.get("strike_type")
            target = market_data.get("floor_strike")

            log.info(
                "SOURCE CHECK %s ticker=%s",
                series, ticker
            )
            log.info(
                "SETTLEMENT SOURCES %s %s",
                series,
                json.dumps(sources, ensure_ascii=False)
            )
            log.info(
                "MARKET RULES %s strike_type=%s target=%s",
                series, strike_type, target
            )
            log.info(
                "PRIMARY RULE %s %s",
                series, str(primary)[:1000]
            )
            log.info(
                "SECONDARY RULE %s %s",
                series, str(secondary)[:1000]
            )

        except Exception as exc:
            log.warning(
                "SOURCE CHECK FAILED %s: %s",
                series, exc
            )


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=2)
    p.add_argument("--duration", type=int, default=300)
    p.add_argument("--catalog-refresh", type=int, default=300)
    args = p.parse_args(argv)

    if args.interval < 2 or args.duration < 0 or args.catalog_refresh < 30:
        p.error("Invalid monitoring settings")

    client = Kalshi(BASE)
    started = time.monotonic()
    next_catalog = 0
    series = []
    iteration = 0
    source_check_done = False

    while True:
        if (
            args.duration
            and time.monotonic() - started >= args.duration
            and iteration
        ):
            break

        tick = time.monotonic()

        try:
            if tick >= next_catalog:
                series, catalog = discover_series(
                    client, max_pages=10
                )
                log.info(
                    "SERIES count=%d complete=%s error=%s",
                    len(series),
                    catalog["series_complete"],
                    catalog["series_error"]
                )
                next_catalog = tick + args.catalog_refresh

            stats = {}

            markets = discover_open(
                client,
                max_pages=5,
                series_tickers=series,
                stats=stats
            )

            eligible = [
                m for m in markets if eligible_market(m)
            ]

            # Inspect official rules once during this run.
            if eligible and not source_check_done:
                inspect_settlement_sources(client, eligible)
                source_check_done = True

            screened = screen(eligible)

            log.info(
                "SCAN %d discovered=%d eligible=%d "
                "price_matches=%d failures=%d READ_ONLY",
                iteration + 1,
                len(markets),
                len(eligible),
                len(screened),
                len(stats["failed_series"])
            )

            for c in screened:
                log.info(
                    "PRICE MATCH %s %s ask=%.3f "
                    "gross=%.3fx target=%.6f "
                    "seconds_left=%.1f NO ENTRY",
                    c["ticker"],
                    c["side"],
                    c["ask"],
                    c["gross"],
                    c["target"],
                    c["seconds_left"]
                )

        except Exception:
            log.exception("SCAN ERROR; no trades")

        iteration += 1

        if args.once:
            break

        elapsed = time.monotonic() - tick

        if elapsed > args.interval:
            log.warning(
                "Scan took %.1fs; polling is best effort",
                elapsed
            )

        time.sleep(max(0, args.interval - elapsed))

    log.info(
        "MONITOR COMPLETE scans=%d",
        iteration
    )


if __name__ == "__main__":
    main()
