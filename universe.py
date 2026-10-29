"""The owner's exact 30-series universe and fail-closed contract checks.

An identifier is a mapping, not evidence that an observed price is fresh or that
its instrument and units match a live contract.  The report keeps those facts
separate.  All observations are acquired from Kalshi by the live client.
"""
from decimal import Decimal
from urllib.parse import unquote, urlparse

from payout_quote import as_decimal

CF_INDEX_IDS = {
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

PYTH_INDEX_IDS = {
    "KXGOLD15M": "Metal.Index.1OZGOLD/USD",
    "KXSILVER15M": "Metal.Index.SILVER/USD",
    "KXBRENT15M": "Commodities.Index.BRENT/USD",
    "KXCOPPER15M": "Commodities.Index.CU/USD",
    "KXNATGAS15M": "Commodities.Index.NATGAS/USD",
    "KXPALLADIUM15M": "Metal.XPD/USD",
    "KXPLATINUM15M": "Metal.XPT/USD",
    # Kalshi's captured series settlement-source URL identifies PYTHOIL, not
    # WTI1M (live validation run 40930802503, source commit 06e0d04).
    "KXWTI15M": "Commodities.Index.PYTHOIL/USD",
    "KXEURUSD15M": "FX.EUR/USD",
    "KXGBPUSD15M": "FX.GBP/USD",
    "KXAUDUSD15M": "FX.AUD/USD",
    "KXUSDCAD15M": "FX.USD/CAD",
    "KXUSDJPY15M": "FX.USD/JPY",
    "KXUSDMXN15M": "FX.USD/MXN",
    "KX2YRRATE15M": "InterestRate.US2Y",
    "KX5YRRATE15M": "InterestRate.US5Y",
    "KX10YRRATE15M": "InterestRate.US10Y",
    "KX30YRRATE15M": "InterestRate.US30Y",
    "KXDJIA15M": "Equity.Index.US30/USD",
}

# These are the exclusions in the pre-existing coverage regression test.  No
# additional aliases or series have been inferred from an instrument's name.
EXCLUDED_FAMILIES = {
    "TON": ("KXTON15M",),
    "coffee": ("KXCOFFEE15M",),
    "S&P 500": ("KXINX15M",),
    "Nasdaq-100": ("KXNDQ15M",),
    "crypto comparative / highest-return": ("KXCRYPTOCOMP15M",),
}

_INSTRUMENTS_AND_UNITS = {
    "KXBTC15M": ("Bitcoin", "USD per BTC"),
    "KXETH15M": ("Ether", "USD per ETH"),
    "KXSOL15M": ("Solana", "USD per SOL"),
    "KXHYPE15M": ("Hyperliquid", "USD per HYPE"),
    "KXADA15M": ("Cardano", "USD per ADA"),
    "KXBCH15M": ("Bitcoin Cash", "USD per BCH"),
    "KXBNB15M": ("BNB", "USD per BNB"),
    "KXDOGE15M": ("Dogecoin", "USD per DOGE"),
    "KXNEAR15M": ("NEAR Protocol", "USD per NEAR"),
    "KXXRP15M": ("XRP", "USD per XRP"),
    "KXZEC15M": ("Zcash", "USD per ZEC"),
    "KXGOLD15M": ("Gold", "USD per troy ounce"),
    "KXSILVER15M": ("Silver", "USD per troy ounce"),
    "KXBRENT15M": ("Brent crude", "USD per barrel"),
    "KXCOPPER15M": ("Copper", "USD per pound"),
    "KXNATGAS15M": ("Natural gas", "USD per MMBtu"),
    "KXPALLADIUM15M": ("Palladium", "USD per troy ounce"),
    "KXPLATINUM15M": ("Platinum", "USD per troy ounce"),
    "KXWTI15M": ("WTI crude", "USD per barrel"),
    "KXEURUSD15M": ("EUR/USD", "USD per EUR"),
    "KXGBPUSD15M": ("GBP/USD", "USD per GBP"),
    "KXAUDUSD15M": ("AUD/USD", "USD per AUD"),
    "KXUSDCAD15M": ("USD/CAD", "CAD per USD"),
    "KXUSDJPY15M": ("USD/JPY", "JPY per USD"),
    "KXUSDMXN15M": ("USD/MXN", "MXN per USD"),
    "KX2YRRATE15M": ("US 2-year Treasury yield", "percentage points"),
    "KX5YRRATE15M": ("US 5-year Treasury yield", "percentage points"),
    "KX10YRRATE15M": ("US 10-year Treasury yield", "percentage points"),
    "KX30YRRATE15M": ("US 30-year Treasury yield", "percentage points"),
    "KXDJIA15M": ("Dow Jones Industrial Average", "native US30 index quote; no rescaling"),
}

UNIVERSE = {
    ticker: {
        "ticker": ticker,
        "instrument": _INSTRUMENTS_AND_UNITS[ticker][0],
        "source_type": source_type,
        "source_id": source_id,
        "units": _INSTRUMENTS_AND_UNITS[ticker][1],
        "target_rule": "positive floor_strike; same native units; no rescaling",
        "direction_rule": "YES above target; use official greater/greater_or_equal equality rule",
    }
    for source_type, mappings in (
        ("CF Benchmarks", CF_INDEX_IDS),
        ("Kalshi-hosted Pyth", PYTH_INDEX_IDS),
    )
    for ticker, source_id in mappings.items()
}


def validate_configuration():
    if len(CF_INDEX_IDS) != 11 or len(PYTH_INDEX_IDS) != 19 or len(UNIVERSE) != 30:
        raise ValueError("Universe must contain exactly 11 CF and 19 Pyth series")
    if set(CF_INDEX_IDS) & set(PYTH_INDEX_IDS):
        raise ValueError("Reference mappings overlap")
    excluded = {ticker for aliases in EXCLUDED_FAMILIES.values() for ticker in aliases}
    if set(UNIVERSE) & excluded:
        raise ValueError("An explicitly excluded series is configured")
    if len(EXCLUDED_FAMILIES) != 5:
        raise ValueError("The report must retain the five excluded families")
    if set(UNIVERSE) != set(CF_INDEX_IDS) | set(PYTH_INDEX_IDS):
        raise ValueError("Universe and reference mappings disagree")
    return True


def series_of(market):
    if not isinstance(market, dict):
        return ""
    explicit = market.get("series_ticker")
    inferred = str(market.get("ticker") or "").split("-", 1)[0]
    if explicit and inferred and explicit != inferred:
        return ""
    return str(explicit or inferred)


def _pyth_source(source):
    """Return vendor recognition and an explorer feed ID, if explicitly given.

    These are links in Kalshi's own series metadata, never requests to Pyth.
    Generic legacy explorer links identify the vendor only.  The modern
    app.pyth.com/explore/<ID> links also let us validate the configured feed.
    """
    name = str(source.get("name") or "").strip().casefold()
    url = str(source.get("url") or "").strip()
    named_pyth = name in {"pyth", "pyth network"} or name.startswith("pyth - ")
    if not url:
        return named_pyth, None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        valid_url = (
            parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False, None
    recognized_host = (
        host in {"app.pyth.com", "pythdata.app", "pyth.network"}
        or host.endswith(".pyth.network")
    )
    if not valid_url or not recognized_host:
        return False, None
    if host == "app.pyth.com" and parsed.path in {"/explore", "/explore/"}:
        return True, None
    if host == "app.pyth.com" and parsed.path.startswith("/explore/"):
        # Decode exactly once, preserving the feed ID's case.  Query-string
        # returnUrl values must not be mistaken for the source identifier.
        return True, unquote(parsed.path[len("/explore/"):]).rstrip("/")
    return True, None


def _source_vendor(metadata):
    """Read vendor evidence from official series settlement-source metadata."""
    if not isinstance(metadata, dict):
        return set()
    if isinstance(metadata.get("series"), dict):
        metadata = metadata["series"]
    vendors = set()
    for source in metadata.get("settlement_sources") or []:
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or "").strip().casefold()
        try:
            host = (urlparse(str(source.get("url") or "")).hostname or "").casefold()
        except ValueError:
            host = ""
        if ("cf benchmarks" in name or "cfbenchmarks" in name
                or host == "cfbenchmarks.com" or host.endswith(".cfbenchmarks.com")):
            vendors.add("CF Benchmarks")
        if _pyth_source(source)[0]:
            vendors.add("Kalshi-hosted Pyth")
    return vendors


def check_compatibility(market, current, series_metadata):
    """Return (compatible, reason); unknown contract facts never imply a pass.

    Kalshi documents Equity.Index.US30/USD as the Dow Jones Industrial Average
    feed at https://docs.kalshi.com/websockets/pyth-value.  A live Dow contract
    may therefore pass when its own official source identifies that feed and
    its native strike is consistent with the observation.  This is a per-market
    check: a missing/mismatched Dow contract never blocks other series.  No
    external source is queried and no value is rescaled to manufacture a match.
    """
    series = series_of(market)
    if series not in UNIVERSE:
        return False, "unknown_or_inconsistent_series"
    if market.get("market_type") != "binary":
        return False, "market_type_missing_or_not_binary"
    try:
        notional = as_decimal(market["notional_value_dollars"])
    except (KeyError, ValueError, TypeError):
        return False, "notional_value_missing_or_invalid"
    if notional != Decimal("1"):
        return False, "notional_value_not_one_dollar"
    if market.get("strike_type") not in {"greater", "greater_or_equal"}:
        return False, "unsupported_or_missing_above_direction"
    try:
        target = as_decimal(market["floor_strike"])
        reference = as_decimal(current)
    except (KeyError, ValueError, TypeError):
        return False, "target_or_reference_missing_or_invalid"
    if target <= 0 or reference <= 0:
        return False, "target_or_reference_not_positive"
    if series == "KXDJIA15M":
        ratio = reference / target
        if ratio <= Decimal("0.1") or ratio >= Decimal("10"):
            return False, "dow_scale_mismatch: reference/target=" + str(ratio)
    metadata = series_metadata
    if isinstance(metadata, dict) and isinstance(metadata.get("series"), dict):
        metadata = metadata["series"]
    if not isinstance(metadata, dict) or not metadata.get("settlement_sources"):
        return False, "official_settlement_source_unavailable"
    if metadata.get("ticker") not in (None, series):
        return False, "series_metadata_ticker_mismatch"
    expected = UNIVERSE[series]["source_type"]
    vendors = _source_vendor(metadata)
    if expected not in vendors:
        return False, "official_settlement_source_mismatch_or_unverified"
    if expected == "Kalshi-hosted Pyth":
        source_ids = {
            source_id
            for source in metadata.get("settlement_sources") or []
            if isinstance(source, dict)
            for recognized, source_id in [_pyth_source(source)]
            if recognized and source_id is not None
        }
        if source_ids and source_ids != {UNIVERSE[series]["source_id"]}:
            return False, "official_reference_identifier_mismatch"
        if series == "KXDJIA15M" and not source_ids:
            return False, "dow_official_reference_identifier_unavailable"
    return True, "verified_binary_above_contract_and_official_source"


validate_configuration()
