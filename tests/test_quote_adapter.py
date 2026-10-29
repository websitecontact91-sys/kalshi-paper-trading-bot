"""Parser tests use synthetic captures, not claimed live Kalshi quote evidence."""

import hashlib
import json
import copy
from decimal import Decimal as D
from pathlib import Path

import pytest

from payout_quote import PayoutQuote
from kalshi_client import ask_and_depth
from quote_adapter import (NORMALIZED_SCHEMA, QuoteUnavailable, build_modeled_quote,
                           build_quote, documented_taker_cost, effective_fee_terms)


def metadata():
    return ({"fee_type": "quadratic", "fee_multiplier": "1"},
            {"fee_type_override": None, "fee_multiplier_override": None},
            {"market_type": "binary", "notional_value_dollars": "1.0000",
             "settlement_bounds_type": "default", "fee_waiver_expiration_time": None})


def modeled(*, ask="0.87", depth="100", precision="0.01", budget="5.00", changes=None):
    series, event, market = metadata()
    if changes:
        for obj, values in zip((series, event, market), changes):
            obj.update(values)
    return build_modeled_quote(ask, depth, series, event, market, budget=budget,
                               balance_precision=precision, now=1793304000)


def synthetic_normalized_capture():
    # Artificial schema for parser testing only; no production API field is inferred.
    values = {"debit": "5.00", "payout": "5.69", "quantity": "5.69",
              "fees": "0.05", "principal": "4.95"}
    text = json.dumps({"example_test_values": values}, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"schema": NORMALIZED_SCHEMA, "budget": "5.00", **values,
            "provenance": {"kind": "captured_kalshi_response", "adapter_verified": True,
                           "adapter_id": "synthetic-parser-test-only", "request_method": "GET",
                           "source_url": "https://external-api.kalshi.com/",
                           "captured_at": "2026-10-29T16:38:00Z",
                           "raw_response": text, "raw_response_sha256": digest,
                           "field_paths": {k: ["example_test_values", k] for k in values}}}


def test_screenshot_arithmetic_fixture_does_not_claim_live_capture():
    path = Path(__file__).parent / "fixtures" / "sol_5usd_screenshot_arithmetic.json"
    raw = json.loads(path.read_text())
    assert raw["provenance"]["kind"] == "screenshot_accounting_example"
    assert raw["provenance"]["captured_response"] is False
    q = PayoutQuote.from_kalshi(raw["budget"], raw["debit"], raw["payout"])
    assert q.payout == D("5.69") and q.displayed_multiplier == D("1.14")
    with pytest.raises(QuoteUnavailable):
        build_quote(raw)


def test_normalized_parser_matches_observed_fields_but_does_not_infer_ui_parity():
    q = build_quote(synthetic_normalized_capture())
    assert q.quote.payout == D("5.69")
    assert q.quote.displayed_multiplier == D("1.14")
    assert q.source == "kalshi_response"
    assert q.ui_parity_verified is False
    assert q.ledger_fields()["fees"] == "0.05"


@pytest.mark.parametrize("change", [
    lambda p: p.update(adapter_verified=False),
    lambda p: p.update(kind="screenshot_accounting_example"),
    lambda p: p.update(request_method="POST"),
    lambda p: p.update(source_url="https://kalshi.com.evil.example/"),
    lambda p: p.update(source_url="http://external-api.kalshi.com/"),
    lambda p: p.update(raw_response_sha256="0" * 64),
    lambda p: p.update(captured_at="2026-10-29T16:38:00"),
    lambda p: p.update(field_paths={}),
])
def test_unverified_or_inconsistent_capture_rejected(change):
    raw = synthetic_normalized_capture()
    change(raw["provenance"])
    with pytest.raises(QuoteUnavailable):
        build_quote(raw)


def test_normalized_payout_cannot_be_changed_to_rounded_display_product():
    raw = synthetic_normalized_capture()
    raw["payout"] = "5.70"
    with pytest.raises(QuoteUnavailable, match="differs"):
        build_quote(raw)


def test_unknown_account_precision_is_a_blocker():
    with pytest.raises(QuoteUnavailable, match="precision is unknown"):
        modeled(precision=None)


@pytest.mark.parametrize("precision", ["0.1", "0.001", "0", "NaN"])
def test_unsupported_account_precision_rejected(precision):
    with pytest.raises(QuoteUnavailable):
        modeled(precision=precision)


def test_documented_fee_model_can_reproduce_numeric_example_without_ui_parity_claim():
    # Synthetic ask/account inputs, not the missing historical SOL raw quote.
    details = modeled(ask="0.87", precision="0.01")
    assert details.quantity == D("5.69")
    assert details.quote.debit == D("5.00")
    assert details.quote.payout == D("5.69")
    assert details.quote.displayed_multiplier == D("1.14")
    assert details.source == "documented_fee_model"
    assert details.ui_parity_verified is False
    assert details.principal + details.fees == details.quote.debit


def test_direct_member_rounding_is_not_silently_whole_cents():
    direct = modeled(precision="0.0001")
    indirect = modeled(precision="0.01")
    assert direct.quote.debit < indirect.quote.debit
    assert direct.quote.debit % D("0.0001") == 0
    assert direct.quote.debit % D("0.01") != 0
    assert direct.quote.debit <= D("5.00")


def test_official_fee_rounding_worked_example():
    # 0.07 * 2.1 * 0.1 * 0.55 * 0.45 = official example's 0.00363825 model fee.
    principal, fees, debit = documented_taker_cost("0.55", "0.10", "2.1", "0.01")
    assert principal == D("0.055")
    assert fees == D("0.005")
    assert debit == D("0.06")


def test_same_official_example_with_documented_direct_member_paper_precision():
    # Same observed formula inputs as the official FCM example, with the
    # separately documented direct-member $0.0001 balance grid.
    principal, fees, debit = documented_taker_cost("0.55", "0.10", "2.1", "0.0001")
    assert principal == D("0.055")
    assert debit == D("0.0587")
    assert fees == D("0.0037")
    assert principal + fees == debit


def test_event_override_takes_precedence_and_zero_multiplier_is_not_missing():
    q = modeled(changes=({}, {"fee_multiplier_override": "0"}, {}))
    assert q.evidence["fee_multiplier"] == "0"
    assert q.quantity == D("5.74")
    assert q.quote.debit <= D("5")


@pytest.mark.parametrize("change", [
    ({"fee_type": "unknown"}, {}, {}),
    ({}, {"fee_type_override": "flat"}, {}),
    ({"fee_multiplier": "-1"}, {}, {}),
    ({"fee_multiplier": "NaN"}, {}, {}),
    ({}, {}, {"market_type": "scalar"}),
    ({}, {}, {"notional_value_dollars": "0.50"}),
    ({}, {}, {"settlement_bounds_type": "bounded"}),
])
def test_unknown_or_inapplicable_model_is_rejected(change):
    with pytest.raises(QuoteUnavailable):
        modeled(changes=change)


def test_missing_event_or_series_fee_metadata_is_not_defaulted():
    series, event, market = metadata()
    del series["fee_multiplier"]
    with pytest.raises(QuoteUnavailable, match="series"):
        build_modeled_quote("0.87", "100", series, event, market, balance_precision="0.01")
    series, event, market = metadata()
    del event["fee_multiplier_override"]
    with pytest.raises(QuoteUnavailable, match="event"):
        build_modeled_quote("0.87", "100", series, event, market, balance_precision="0.01")


def captured_public_case(series="KXSOL15M"):
    path = Path(__file__).parent / "fixtures" / "live_public_schema_20261029.json"
    return json.loads(path.read_text())["cases"][series]


@pytest.mark.parametrize("series_id", ["KXBTC15M", "KXSOL15M", "KXWTI15M"])
def test_real_public_event_metadata_omits_overrides_and_inherits_observed_series_fees(series_id):
    case = captured_public_case(series_id)
    series, event, market = case["series"], case["event"], case["market"]
    assert "fee_type_override" not in event and "fee_multiplier_override" not in event
    assert effective_fee_terms(series, event, market) == ("quadratic", D("1"), False)
    explicit_null = dict(event, fee_type_override=None, fee_multiplier_override=None)
    assert effective_fee_terms(series, explicit_null, market) == effective_fee_terms(series, event, market)
    # These are actual public book levels, not captured retail payout quotes.
    for side, (ask, expected_depth) in case["expected_executable_levels"].items():
        price, depth = ask_and_depth(case["book"], side, ask)
        assert price == D(ask) and depth == D(expected_depth)
        for precision in ("0.01", "0.0001"):
            q = build_modeled_quote(price, depth, series, event, market, balance_precision=precision)
            assert D("0") < q.quote.debit <= D("5.00")
            assert q.principal + q.fees == q.quote.debit
            assert q.quantity <= depth and q.quantity % D("0.01") == 0
            assert q.quote.payout == q.quantity
            assert q.ui_parity_verified is False


@pytest.mark.parametrize("change", [
    lambda s, e, m: e.clear(),
    lambda s, e, m: e.pop("event_ticker"),
    lambda s, e, m: e.pop("series_ticker"),
    lambda s, e, m: e.update(event_ticker="KXSOL15M-WRONG-EVENT"),
    lambda s, e, m: e.update(series_ticker="KXBTC15M"),
    lambda s, e, m: s.update(ticker="KXBTC15M"),
    lambda s, e, m: m.update(ticker="KXBTC15M-WRONG-MARKET"),
    lambda s, e, m: m.update(series_ticker="KXBTC15M"),
    lambda s, e, m: m.pop("event_ticker"),
])
def test_omitted_optional_fees_require_a_matching_fetched_event(change):
    case = captured_public_case()
    series, event, market = case["series"], case["event"], case["market"]
    change(series, event, market)
    with pytest.raises(QuoteUnavailable, match="event"):
        effective_fee_terms(series, event, market)


@pytest.mark.parametrize("partial", [{"fee_type_override": "quadratic"},
                                     {"fee_multiplier_override": "0"}])
def test_incomplete_nonnull_event_override_is_not_silently_inherited(partial):
    case = captured_public_case()
    case["event"].update(partial)
    with pytest.raises(QuoteUnavailable, match="incomplete"):
        effective_fee_terms(case["series"], case["event"], case["market"])


@pytest.mark.parametrize("field", ["fee_type_override", "fee_multiplier_override"])
def test_one_explicit_null_and_one_omitted_override_both_inherit(field):
    case = captured_public_case()
    case["event"][field] = None
    assert effective_fee_terms(case["series"], case["event"], case["market"]) == (
        "quadratic", D("1"), False)


def test_observed_event_override_still_wins_over_series_after_omission_fix():
    case = captured_public_case()
    event = copy.deepcopy(case["event"])
    event.update(fee_type_override="quadratic", fee_multiplier_override="0")
    assert effective_fee_terms(case["series"], event, case["market"]) == (
        "quadratic", D("0"), False)


def test_fee_waiver_expires_at_the_documented_time():
    series, event, market = metadata()
    market["fee_waiver_expiration_time"] = "2026-10-29T12:00:00Z"
    assert effective_fee_terms(series, event, market, now=1793275199) == ("quadratic", D("0"), True)
    assert effective_fee_terms(series, event, market, now=1793275200) == ("quadratic", D("1"), False)


def test_sizing_is_maximal_at_the_observed_price_within_budget_and_depth():
    q = modeled(depth="3.451", precision="0.0001")
    assert q.quantity == D("3.45")
    assert q.quantity <= D("3.451")
    assert q.quote.debit <= D("5")
    full = modeled(precision="0.0001")
    _, _, next_debit = documented_taker_cost("0.87", full.quantity + D("0.01"), "1", "0.0001")
    assert next_debit > D("5")


@pytest.mark.parametrize("kwargs", [{"depth": "0.009"}, {"depth": "NaN"},
                                    {"budget": "5.01"}, {"ask": "0"}, {"ask": "1"}])
def test_no_phantom_depth_or_overspend(kwargs):
    with pytest.raises(QuoteUnavailable):
        modeled(**kwargs)
