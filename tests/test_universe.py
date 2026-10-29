"""Offline coverage diagnostics use the same compatibility/selection code."""
import csv
from decimal import Decimal
import io
import json

import pytest

from payout_quote import PayoutQuote
from quote_adapter import QuoteDetails
from universe import (
    CF_INDEX_IDS, EXCLUDED_FAMILIES, PYTH_INDEX_IDS, UNIVERSE,
    check_compatibility, series_of, validate_configuration,
)
from validate_universe import build_report, csv_text, main, quote_parity_status, write_report

D = Decimal
NOW = 1000.0


def market(series="KXBTC15M", **overrides):
    value = {
        "ticker": series + "-TEST", "series_ticker": series, "market_type": "binary",
        "notional_value_dollars": "1.0000", "strike_type": "greater_or_equal",
        "floor_strike": "100", "status": "open", "open_time": 500, "close_time": 1400,
    }
    value.update(overrides)
    return value


def metadata(series="KXBTC15M"):
    name, url = ("CF Benchmarks", "https://www.cfbenchmarks.com") if series in CF_INDEX_IDS else (
        "Pyth Network", "https://pyth.network"
    )
    return {"ticker": series, "settlement_sources": [{"name": name, "url": url}]}


def test_exact_universe_and_actual_excluded_aliases():
    assert validate_configuration()
    assert len(CF_INDEX_IDS) == 11
    assert len(PYTH_INDEX_IDS) == 19
    assert len(UNIVERSE) == 30
    assert not (CF_INDEX_IDS.keys() & PYTH_INDEX_IDS.keys())
    assert len(EXCLUDED_FAMILIES) == 5
    excluded = {ticker for aliases in EXCLUDED_FAMILIES.values() for ticker in aliases}
    assert excluded == {"KXTON15M", "KXCOFFEE15M", "KXINX15M", "KXNDQ15M", "KXCRYPTOCOMP15M"}
    assert not excluded & UNIVERSE.keys()
    for ticker, spec in UNIVERSE.items():
        assert spec["ticker"] == ticker
        assert all(spec[key] for key in ("instrument", "source_type", "source_id", "units", "target_rule", "direction_rule"))


def test_known_crypto_and_pyth_contracts():
    assert check_compatibility(market(), D("101"), metadata())[0]
    assert check_compatibility(market("KXGOLD15M"), D("101"), metadata("KXGOLD15M"))[0]
    assert check_compatibility(market(), D("101"), {"series": metadata()})[0]


@pytest.mark.parametrize("overrides,reason", [
    ({"market_type": "scalar"}, "market_type_missing_or_not_binary"),
    ({"notional_value_dollars": "0.5"}, "notional_value_not_one_dollar"),
    ({"notional_value_dollars": "NaN"}, "notional_value_missing_or_invalid"),
    ({"notional_value_dollars": None}, "notional_value_missing_or_invalid"),
    ({"strike_type": "less"}, "unsupported_or_missing_above_direction"),
    ({"strike_type": None}, "unsupported_or_missing_above_direction"),
    ({"floor_strike": "0"}, "target_or_reference_not_positive"),
    ({"floor_strike": "-1"}, "target_or_reference_not_positive"),
    ({"floor_strike": "Infinity"}, "target_or_reference_missing_or_invalid"),
    ({"floor_strike": 100.1}, "target_or_reference_missing_or_invalid"),
])
def test_unknown_contract_facts_fail_closed(overrides, reason):
    assert check_compatibility(market(**overrides), D("101"), metadata()) == (False, reason)


@pytest.mark.parametrize("current", ["NaN", "Infinity", "0", "-1", None, True, 100.0])
def test_invalid_reference_is_not_compatible(current):
    assert not check_compatibility(market(), current, metadata())[0]


def test_unknown_or_inconsistent_series():
    assert series_of(market(series_ticker="KXSOL15M")) == ""
    assert not check_compatibility(market(series_ticker="KXSOL15M"), D("101"), metadata())[0]
    assert not check_compatibility(market("KXTON15M"), D("101"), metadata())[0]


def test_official_vendor_metadata_required_and_must_match():
    assert check_compatibility(market(), D("101"), {}) == (False, "official_settlement_source_unavailable")
    wrong = {"settlement_sources": [{"name": "Unverified", "url": "https://notcfbenchmarks.com"}]}
    assert not check_compatibility(market(), D("101"), wrong)[0]
    assert not check_compatibility(market(), D("101"), metadata("KXGOLD15M"))[0]


# These settlement-source records were captured from Kalshi's live inventory
# in validation run 40930802503 on 06e0d04.  They are metadata regression cases,
# not price observations, UI payout evidence, or calls to an external feed.
CAPTURED_PYTH_SOURCES = [
    ("KXGOLD15M", "Pyth - Gold", "https://app.pyth.com/explore/Metal.Index.1OZGOLD%2FUSD?returnUrl=%2Fexplore%2Fall%3Fsearch%3D3712%26state%3Dstable"),
    ("KXSILVER15M", "Pyth - Silver", "https://app.pyth.com/explore/Metal.Index.SILVER%2FUSD?returnUrl=%2Fexplore%3Fstate%3Dstable"),
    ("KXBRENT15M", "Pyth - Brent", "https://app.pyth.com/explore/Commodities.Index.BRENT%2FUSD?returnUrl=%2Fexplore%3Fsearch%3Dbrent%2Bindex%26state%3Dstable"),
    ("KXCOPPER15M", "Pyth - Copper", "https://app.pyth.com/explore/Commodities.Index.CU%2FUSD?returnUrl=%2Fexplore%3Fsearch%3Dcopper%26state%3Dstable"),
    ("KXNATGAS15M", "Pyth - NATGAS", "https://app.pyth.com/explore/Commodities.Index.NATGAS%2FUSD?returnUrl=%2Fexplore%3Fsearch%3Dgas%26state%3Dstable"),
    ("KXPALLADIUM15M", "Pyth - Palladium", "https://app.pyth.com/explore/Metal.XPD%2FUSD?returnUrl=%2Fexplore%3Fsearch%3Dpalladium%26state%3Dstable"),
    ("KXPLATINUM15M", "Pyth - Platinum", "https://app.pyth.com/explore/Metal.XPT%2FUSD?returnUrl=%2Fexplore%3Fsearch%3DPLATINUM%26state%3Dstable"),
    ("KXWTI15M", "Pyth - WTI", "https://app.pyth.com/explore/Commodities.Index.PYTHOIL%2FUSD?returnUrl=%2Fexplore%3Fsearch%3DPythOil%26state%3Dstable"),
    ("KXGBPUSD15M", "Pyth - GBPUSD", "https://app.pyth.com/explore/FX.GBP%2FUSD?returnUrl=%2Fexplore%3Fsearch%3Dgbpusd%26state%3Dstable"),
    ("KX2YRRATE15M", "Pyth - 2Y UST", "https://app.pyth.com/explore/InterestRate.US2Y"),
    ("KX5YRRATE15M", "Pyth - 5Y UST", "https://app.pyth.com/explore/InterestRate.US5Y"),
    ("KX10YRRATE15M", "Pyth - 10Y UST", "https://app.pyth.com/explore/InterestRate.US10Y"),
    ("KX30YRRATE15M", "Pyth - 30Y UST", "https://app.pyth.com/explore/InterestRate.US30Y"),
]


@pytest.mark.parametrize("series,name,url", CAPTURED_PYTH_SOURCES)
def test_captured_kalshi_pyth_explorer_metadata_matches_configured_feed(series, name, url):
    official = {"ticker": series, "settlement_sources": [{"name": name, "url": url}]}
    assert check_compatibility(market(series), D("101"), official)[0]


def test_wti_mapping_uses_actual_official_pythoil_identifier():
    assert PYTH_INDEX_IDS["KXWTI15M"] == "Commodities.Index.PYTHOIL/USD"
    assert UNIVERSE["KXWTI15M"]["source_id"] == "Commodities.Index.PYTHOIL/USD"
    old_identifier = {"settlement_sources": [{
        "name": "Pyth - WTI", "url": "https://app.pyth.com/explore/Commodities.Index.WTI1M%2FUSD",
    }]}
    assert check_compatibility(market("KXWTI15M"), D("101"), old_identifier) == (
        False, "official_reference_identifier_mismatch"
    )


@pytest.mark.parametrize("series", ["KXEURUSD15M", "KXAUDUSD15M", "KXUSDCAD15M", "KXUSDJPY15M"])
def test_captured_generic_legacy_pyth_source_stays_supported(series):
    official = {"ticker": series, "settlement_sources": [{"name": "Pyth", "url": "https://pythdata.app/explore"}]}
    assert check_compatibility(market(series), D("101"), official)[0]


def test_vendor_name_without_identifier_still_requires_pyth_name():
    official = {"settlement_sources": [{"name": "Pyth - Gold"}]}
    assert check_compatibility(market("KXGOLD15M"), D("101"), official)[0]
    official["settlement_sources"][0]["name"] = "Pyth-like provider"
    assert not check_compatibility(market("KXGOLD15M"), D("101"), official)[0]


@pytest.mark.parametrize("url", [
    "https://app.pyth.com.evil.example/explore/Metal.Index.1OZGOLD%2FUSD",
    "https://app-pyth.com/explore/Metal.Index.1OZGOLD%2FUSD",
    "https://app.pyth.com@evil.example/explore/Metal.Index.1OZGOLD%2FUSD",
    "https://evil.example/?returnUrl=https://app.pyth.com/explore/Metal.Index.1OZGOLD%2FUSD",
])
def test_pyth_display_name_cannot_override_misleading_url_host(url):
    official = {"settlement_sources": [{"name": "Pyth - Gold", "url": url}]}
    assert check_compatibility(market("KXGOLD15M"), D("101"), official) == (
        False, "official_settlement_source_mismatch_or_unverified"
    )


def test_explorer_identifier_mismatch_is_not_hidden_by_name_or_return_url():
    official = {"settlement_sources": [{
        "name": "Pyth - Gold",
        "url": "https://app.pyth.com/explore/Metal.Index.SILVER%2FUSD?returnUrl=%2Fexplore%2FMetal.Index.1OZGOLD%2FUSD",
    }]}
    assert check_compatibility(market("KXGOLD15M"), D("101"), official) == (
        False, "official_reference_identifier_mismatch"
    )


def test_conflicting_official_explorer_identifiers_fail_closed():
    official = {"settlement_sources": [
        {"name": "Pyth - Gold", "url": "https://app.pyth.com/explore/Metal.Index.1OZGOLD%2FUSD"},
        {"name": "Pyth - Silver", "url": "https://app.pyth.com/explore/Metal.Index.SILVER%2FUSD"},
    ]}
    assert check_compatibility(market("KXGOLD15M"), D("101"), official) == (
        False, "official_reference_identifier_mismatch"
    )


def test_recognized_dow_feed_link_does_not_establish_matching_live_instrument():
    official = {"settlement_sources": [{"name": "Pyth - US30", "url": "https://app.pyth.com/explore/Equity.Index.US30%2FUSD"}]}
    assert check_compatibility(market("KXDJIA15M"), D("101"), official) == (
        False, "dow_instrument_units_unverified"
    )


@pytest.mark.parametrize("current", ["0.01", "1", "100", "1000000"])
def test_no_arbitrary_ordinary_market_distance_gate(current):
    assert check_compatibility(market(), D(current), metadata())[0]


def test_dow_name_or_fresh_price_does_not_prove_instrument_match():
    good_scale = market("KXDJIA15M", floor_strike="43000")
    assert check_compatibility(good_scale, D("43001"), metadata("KXDJIA15M")) == (
        False, "dow_instrument_units_unverified"
    )
    compatible, reason = check_compatibility(good_scale, D("430"), metadata("KXDJIA15M"))
    assert not compatible
    assert reason.startswith("dow_scale_mismatch")


class FakeClient:
    def __init__(self, **kwargs):
        # An injected policy stub exercises the report interface, not real UI
        # evidence.  Production obtains this flag only from quote_policy().
        self.policy = {"verified": True, "balance_precision": "0.01", "reason": None}
        self.readiness_reasons = []
        self.discovery_errors = {}
        self.markets = [market(series) for series in UNIVERSE]
        self.snapshot_calls = []
        self.old = set()
        self.missing_quotes = set()
        self.snapshot_errors = {}
        self.metadata_missing = set()
        self.closed = False

    def discover(self, now=None):
        return self.markets

    def series_metadata(self, series):
        return {} if series in self.metadata_missing else metadata(series)

    def snapshot(self, item, now=None, include_quotes=True):
        series = series_of(item)
        self.snapshot_calls.append((series, include_quotes))
        if series in self.snapshot_errors:
            raise self.snapshot_errors[series]
        detail = QuoteDetails(
            quote=PayoutQuote.from_kalshi("5", "5", "5.69"), quantity=D("5.69"),
            fees=D("0.0497"), principal=D("4.9503"), source="synthetic_test_only",
        )
        quote_map = {} if series in self.missing_quotes else {"YES": detail}
        rejections = {"NO": "ask_out_of_band"}
        if series in self.missing_quotes:
            rejections["YES"] = "account_balance_precision_unverified"
        return {
            "market": item, "current": D("100"),
            "observation_time": now - (16 if series in self.old else 1),
            "quotes": quote_map, "asks": {"YES": D("0.87"), "NO": D("0.13")},
            "rejections": rejections,
        }

    def close(self):
        self.closed = True


def report(client, **kwargs):
    return build_report(client, now=NOW, source_sha="a" * 40, **kwargs)


def test_every_available_series_is_evaluated_outside_entry_window():
    client = FakeClient()
    result = report(client)
    assert len(result["rows"]) == 30
    assert {series for series, _ in client.snapshot_calls} == set(UNIVERSE)
    assert all(include_quotes for _, include_quotes in client.snapshot_calls)
    assert result["counts"] == {
        "configured": 30, "available": 30, "fresh": 30, "compatible": 29,
        "evaluated": 30, "eligible": 29,
    }
    assert all(not row["entry_window_open"] for row in result["rows"])
    assert result["diagnostic_exit_code"] == 0
    assert not result["ready"]
    assert result["quote_parity"]["verified"]  # injected policy stub only
    assert len(result["exclusions"]) == 5
    assert not result["dow_check"]["compatible"]


def test_missing_stale_and_unverified_rows_never_disappear():
    client = FakeClient()
    client.markets = [item for item in client.markets if series_of(item) != "KXSOL15M"]
    client.old.add("KXETH15M")
    client.metadata_missing.add("KXGOLD15M")
    client.missing_quotes.add("KXBTC15M")
    result = report(client)
    rows = {row["ticker"]: row for row in result["rows"]}
    assert len(rows) == 30
    assert rows["KXSOL15M"]["configured"] and not rows["KXSOL15M"]["available"]
    assert rows["KXSOL15M"]["current"] == ""
    assert rows["KXETH15M"]["available"] and not rows["KXETH15M"]["fresh"]
    assert rows["KXGOLD15M"]["fresh"] and not rows["KXGOLD15M"]["compatible"]
    assert rows["KXBTC15M"]["evaluated"] and not rows["KXBTC15M"]["eligible"]
    assert rows["KXBTC15M"]["rejections"]["YES"] == "account_balance_precision_unverified"


def test_expired_and_future_markets_are_not_current():
    client = FakeClient()
    client.markets = [market(close_time=NOW), market("KXSOL15M", open_time=NOW + 1)]
    result = report(client)
    assert result["counts"]["available"] == 0
    assert len(result["rows"]) == 30
    assert not client.snapshot_calls


def test_missing_credentials_are_reported_without_secret_trace():
    client = FakeClient()
    client.snapshot_errors["KXBTC15M"] = RuntimeError("Missing KALSHI_PRIVATE_KEY secret-value-not-for-output")
    result = report(client)
    bitcoin = next(row for row in result["rows"] if row["ticker"] == "KXBTC15M")
    assert bitcoin["reason"] == "kalshi_reference_credentials_unavailable"
    assert "secret-value" not in json.dumps(result)
    assert result["diagnostic_exit_code"] == 0


def test_all_public_series_failures_exit_one_but_keep_rows():
    client = FakeClient()
    client.markets = []
    client.discovery_errors = dict.fromkeys(UNIVERSE, "request_failed")
    result = report(client)
    assert result["diagnostic_exit_code"] == 1
    assert result["all_public_discovery_failed"]
    assert len(result["rows"]) == 30


def test_partial_discovery_failure_is_a_diagnostic_not_a_ready_pass():
    client = FakeClient()
    client.markets = [market()]
    client.discovery_errors = dict.fromkeys(set(UNIVERSE) - {"KXBTC15M"}, "request_failed")
    result = report(client)
    assert result["diagnostic_exit_code"] == 0
    assert not result["ready"]
    assert len(result["rows"]) == 30


def test_quote_fixture_cannot_be_screenshot_or_synthetic(tmp_path):
    fixture = tmp_path / "quote.json"
    fixture.write_text(json.dumps({"budget": "5", "debit": "5", "payout": "5.69", "provenance": {"captured_response": False}}))
    assert not quote_parity_status(fixture)["verified"]
    assert not quote_parity_status(tmp_path / "absent.json")["verified"]


def test_csv_and_json_keep_flags_exact_tickers_and_source_sha(tmp_path):
    result = report(FakeClient())
    output = tmp_path / "universe_report.csv"
    json_output = tmp_path / "universe_report.json"
    write_report(result, output, json_output)
    rows = list(csv.DictReader(io.StringIO(csv_text(result))))
    assert len(rows) == 30
    assert {row["ticker"] for row in rows} == set(UNIVERSE)
    assert rows[0]["configured"] == "True"
    saved = json.loads(json_output.read_text())
    assert saved["source_sha"] == "a" * 40
    assert len(saved["exclusions"]) == 5


def test_cli_closes_client_and_prints_report(tmp_path, capsys):
    client = FakeClient()
    # NOW is fixed through the injected instance's market/observation times.
    client.markets = []
    exit_code = main([
        "--output", str(tmp_path / "out.csv"), "--json-output", str(tmp_path / "out.json"), "--wait", "0",
    ], client_factory=lambda **kwargs: client)
    assert exit_code == 0
    assert client.closed
    assert "KXDJIA15M" in capsys.readouterr().out
    assert len(json.loads((tmp_path / "out.json").read_text())["rows"]) == 30


def test_invalid_wait_fails_without_running_network_or_sleep():
    with pytest.raises(ValueError):
        report(FakeClient(), wait=61)
    with pytest.raises(ValueError):
        report(FakeClient(), wait=float("nan"))


def test_wait_allows_reference_startup_and_uses_shared_freshness_limit():
    sleeps = []
    result = report(FakeClient(), wait=10, sleeper=sleeps.append)
    assert sleeps == [10]
    assert result["max_observation_age_seconds"] == 15


def test_live_inventory_rechecks_freshness_after_slow_snapshot():
    current_time = [NOW]

    class SlowClient(FakeClient):
        def snapshot(self, item, now=None, include_quotes=True):
            result = super().snapshot(item, now=now, include_quotes=include_quotes)
            current_time[0] += 20
            return result

    result = build_report(SlowClient(), clock=lambda: current_time[0], source_sha="a" * 40)
    bitcoin = next(row for row in result["rows"] if row["ticker"] == "KXBTC15M")
    assert bitcoin["observation_age_seconds"] == 21
    assert not bitcoin["fresh"]
    assert not bitcoin["eligible"]


def test_client_policy_cannot_be_bypassed_by_an_eligible_modeled_quote():
    client = FakeClient()
    client.policy = {"verified": False, "balance_precision": "0.01", "reason": "quote_evidence_unverified"}
    client.readiness_reasons = ["quote_evidence_unverified"]
    result = report(client)
    assert result["counts"]["eligible"] == 0
    assert result["counts"]["evaluated"] == 30
    assert not result["quote_parity"]["verified"]
    assert result["quote_policy"]["balance_precision"] == "0.01"
    assert result["rows"][0]["reason"] == "quote_evidence_unverified"


def test_report_uses_refreshed_market_not_discovery_target_or_status():
    class RefreshedClient(FakeClient):
        def snapshot(self, item, now=None, include_quotes=True):
            result = super().snapshot(item, now=now, include_quotes=include_quotes)
            result["market"] = dict(item, status="closed", floor_strike="101")
            return result

    result = report(RefreshedClient())
    assert result["counts"]["available"] == 0
    assert result["counts"]["eligible"] == 0
    assert result["rows"][0]["target"] == "101"
    assert result["rows"][0]["reason"] == "closed_during_evaluation"


def test_actual_live_adapter_import_and_missing_evidence_policy(tmp_path):
    from live_client import LiveKalshiClient

    class API:
        def open_markets(self, series, now):
            return [market(series, event_ticker=series + "-EVENT", yes_ask_dollars="0.87", no_ask_dollars="0.13")]

        def series(self, series):
            return dict(metadata(series), fee_type="quadratic", fee_multiplier="1")

        def market(self, ticker):
            return self.open_markets(ticker.split("-", 1)[0], NOW)[0]

        def event(self, ticker):
            return {"fee_type_override": None, "fee_multiplier_override": None}

        def orderbook(self, ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.87", "100"]], "no_dollars": [["0.13", "100"]]}}

    class References:
        def price(self, series):
            return D("100"), NOW - 1

        def close(self):
            pass

    client = LiveKalshiClient(
        policy_path=tmp_path / "missing-policy.json", api=API(), references=References(), clock=lambda: NOW,
    )
    result = report(client)
    assert len(result["rows"]) == 30
    assert result["counts"]["available"] == 30
    assert result["counts"]["fresh"] == 30
    assert result["counts"]["compatible"] == 29
    assert result["counts"]["eligible"] == 0
    assert result["quote_policy"]["readiness_reasons"] == client.readiness_reasons
    assert not result["ready"]
