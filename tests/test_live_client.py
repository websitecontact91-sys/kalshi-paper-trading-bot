"""Offline adapter tests. Synthetic policy records are never live quote evidence."""

import copy
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

import pytest

import live_client
from live_client import LiveKalshiClient, quote_policy
from quote_adapter import NORMALIZED_SCHEMA, build_modeled_quote
from state_store import canonical_round_key
from timing import round_key


class Clock:
    def __init__(self, now=1793304000):
        self.now = now

    def __call__(self):
        return self.now


def market(clock, ticker="KXSOL15M-26OCT291730"):
    return {"ticker": ticker, "series_ticker": "KXSOL15M", "event_ticker": "KXSOL15M-26OCT291730",
            "status": "active", "open_time": clock() - 870, "close_time": clock() + 30,
            "yes_ask_dollars": "0.87", "no_ask_dollars": "0.14",
            "market_type": "binary", "notional_value_dollars": "1.0000",
            "settlement_bounds_type": "default", "fee_waiver_expiration_time": None}


class API:
    def __init__(self, clock):
        self.current = market(clock)
        self.book = {"orderbook_fp": {"no_dollars": [["0.13", "100"]],
                                       "yes_dollars": [["0.86", "100"]]}}
        self.series_data = {"fee_type": "quadratic", "fee_multiplier": "1"}
        self.event_data = {"fee_type_override": None, "fee_multiplier_override": None}
        self.calls = Counter()
        self.book_requests = []
        self.missing = False
        self.book_error = False

    def series(self, ticker):
        self.calls["series"] += 1
        return copy.deepcopy(self.series_data)

    def event(self, ticker):
        self.calls["event"] += 1
        return copy.deepcopy(self.event_data)

    def market(self, ticker):
        self.calls["market"] += 1
        return copy.deepcopy(self.current)

    def orderbook(self, ticker):
        self.calls["orderbook"] += 1
        return copy.deepcopy(self.book)

    def refresh_markets(self, tickers):
        self.calls["refresh_markets"] += 1
        return {} if self.missing else {self.current["ticker"]: copy.deepcopy(self.current)}

    def orderbooks(self, tickers):
        self.calls["orderbooks"] += 1
        self.book_requests.append(list(tickers))
        if self.book_error:
            raise TimeoutError("synthetic timeout")
        return {ticker: copy.deepcopy(self.book) for ticker in tickers}


class References:
    def __init__(self, clock):
        self.clock, self.age, self.unavailable = clock, 0, False
        self.calls = 0
        self.closed = False

    def price(self, series):
        self.calls += 1
        if self.unavailable:
            raise ValueError("synthetic missing observation")
        return D("100.25"), self.clock() - self.age

    def close(self):
        self.closed = True


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    clock = Clock()
    api, refs = API(clock), References(clock)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"balance_precision": "0.01", "evidence_file": None}))
    monkeypatch.setattr(live_client, "check_compatibility", lambda *args: (True, "compatible"))
    client = LiveKalshiClient(policy, api=api, references=refs, clock=clock)
    return client, api, refs, clock


def test_missing_policy_or_self_declared_verified_flag_cannot_unlock(tmp_path):
    assert quote_policy(tmp_path / "missing.json")["verified"] is False
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"verified": True, "balance_precision": "0.01"}))
    result = quote_policy(policy)
    assert result["verified"] is False
    assert result["reason"]


@pytest.mark.parametrize("precision", [None, "0.1", "0.001", 0.01, "NaN"])
def test_account_precision_requires_an_explicit_supported_exact_value(tmp_path, precision):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"verified": True, "balance_precision": precision}))
    result = quote_policy(policy)
    assert result["verified"] is False
    assert result["balance_precision"] is None


def test_arithmetic_screenshot_fixture_cannot_unlock_live_entries(tmp_path):
    original = Path(__file__).parent / "fixtures" / "sol_5usd_screenshot_arithmetic.json"
    (tmp_path / "synthetic.json").write_text(original.read_text())
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"verified": True, "balance_precision": "0.01",
                                  "evidence_file": "synthetic.json"}))
    assert quote_policy(policy)["verified"] is False


def synthetic_identity_policy(tmp_path):
    """Artificial parser input, deliberately confined to a temporary test path."""
    clock = Clock()
    m = market(clock)
    series = {"fee_type": "quadratic", "fee_multiplier": "1"}
    event = {"fee_type_override": None, "fee_multiplier_override": None}
    details = build_modeled_quote("0.87", "100", series, event, m,
                                  balance_precision="0.01", now=clock())
    fields = {key: str(value) for key, value in {
        "debit": details.quote.debit, "payout": details.quote.payout,
        "quantity": details.quantity, "fees": details.fees, "principal": details.principal,
    }.items()}
    payload = {**fields, "market_ticker": m["ticker"], "side": "YES"}
    raw = json.dumps({"synthetic_test_only": payload}, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    evidence = {"schema": NORMALIZED_SCHEMA, "budget": "5.00", **fields,
        "provenance": {"kind": "captured_kalshi_response", "adapter_verified": True,
            "adapter_id": "synthetic-policy-parser-test-only", "request_method": "GET",
            "source_url": "https://external-api.kalshi.com/", "captured_at": "2026-10-29T16:00:00Z",
            "raw_response": raw, "raw_response_sha256": digest,
            "field_paths": {key: ["synthetic_test_only", key] for key in payload},
            "ui_validation": {"verified": True, "raw_response_sha256": digest,
                "displayed_multiplier": str(details.quote.displayed_multiplier),
                "debit": str(details.quote.debit), "payout": str(details.quote.payout)}},
        "model_validation_inputs": {"ask": "0.87", "available_quantity": "100",
            "series": series, "event": event, "market": m, "side": "YES",
            "observed_at": "2026-10-29T16:00:00Z"}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"balance_precision": "0.01", "evidence_file": "evidence.json"}))
    return path, evidence


def test_policy_parser_accepts_matching_identity_and_values_in_a_synthetic_unit_fixture(tmp_path):
    path, evidence = synthetic_identity_policy(tmp_path)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    assert quote_policy(path)["verified"] is True


@pytest.mark.parametrize("alter", [
    lambda e: e["model_validation_inputs"]["market"].update(ticker="KXBTC15M-EXAMPLE"),
    lambda e: e["model_validation_inputs"].update(side="NO"),
    lambda e: e["model_validation_inputs"].pop("side"),
    lambda e: e["provenance"]["field_paths"].pop("market_ticker"),
    lambda e: e["provenance"]["field_paths"].pop("side"),
    lambda e: e["model_validation_inputs"].update(ask="0.88"),
])
def test_equal_displayed_amounts_cannot_substitute_for_the_required_sol_identity(tmp_path, alter):
    path, evidence = synthetic_identity_policy(tmp_path)
    alter(evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    assert quote_policy(path)["verified"] is False


def test_metadata_caches_expire_and_can_be_forced(adapter):
    client, api, _, clock = adapter
    assert client.series_metadata("KXSOL15M")["fee_multiplier"] == "1"
    client.event_metadata("EVENT")
    api.series_data["fee_multiplier"] = "2"
    api.event_data["fee_multiplier_override"] = "3"
    clock.now += 59
    assert client.series_metadata("KXSOL15M")["fee_multiplier"] == "1"
    assert client.event_metadata("EVENT")["fee_multiplier_override"] is None
    clock.now += 1
    assert client.event_metadata("EVENT")["fee_multiplier_override"] == "3"
    assert api.calls["event"] == 2
    clock.now += 240
    assert client.series_metadata("KXSOL15M")["fee_multiplier"] == "2"
    assert api.calls["series"] == 2
    client.series_metadata("KXSOL15M", force=True)
    client.event_metadata("EVENT", force=True)
    assert api.calls["series"] == 3 and api.calls["event"] == 3


def test_discovery_caches_but_covers_every_configured_series(adapter, monkeypatch):
    client, api, _, clock = adapter
    expected_series = set(live_client.UNIVERSE)
    seen = []

    def open_markets(series, now):
        seen.append(series)
        return [{"ticker": series + "-NEAREST", "close_time": now + 30, "open_time": now - 870},
                {"ticker": series + "-LATER", "close_time": now + 930, "open_time": now - 10},
                {"ticker": series + "-FUTURE", "close_time": now + 15, "open_time": now + 1}]

    monkeypatch.setattr(api, "open_markets", open_markets, raising=False)
    first = client.discover()
    assert len(expected_series) == 30
    assert set(seen) == expected_series and len(seen) == 30
    assert len(first) == 30 and all(m["ticker"].endswith("-NEAREST") for m in first)
    clock.now += 59
    assert client.discover() == first and len(seen) == 30
    clock.now += 1
    assert len(client.discover()) == 30 and len(seen) == 60


def test_final_revalidation_refetches_market_book_reference_and_fee_terms(adapter):
    client, api, refs, _ = adapter
    original = copy.deepcopy(api.current)
    first = client.batch_snapshots([original])[0]
    old_quantity = first["quotes"]["YES"].quantity
    assert first["quote_ready"] is False  # Modeled arithmetic is not proof of UI parity.
    api.current["yes_ask_dollars"] = "0.88"
    api.book["orderbook_fp"]["no_dollars"] = [["0.12", "100"]]
    api.series_data["fee_multiplier"] = "2"
    api.event_data["fee_multiplier_override"] = "3"
    final = client.snapshot(original)
    assert final["asks"]["YES"] == D("0.88")
    assert final["quotes"]["YES"].quantity < old_quantity
    assert final["quotes"]["YES"].evidence["fee_multiplier"] == "3"
    assert api.calls["market"] == 1 and api.calls["orderbook"] == 1
    assert api.calls["series"] == 2 and api.calls["event"] == 2
    assert refs.calls == 2


def test_stale_reference_cannot_produce_an_executable_quote(adapter):
    client, api, refs, _ = adapter
    refs.age = 16
    result = client.snapshot(api.current)
    assert result["quotes"] == {}
    assert result["rejections"] == {"YES": "stale", "NO": "stale"}
    assert api.calls["orderbook"] == 0


def test_missing_batch_market_is_unavailable_not_reused_from_cache(adapter):
    client, api, _, _ = adapter
    original = copy.deepcopy(api.current)
    client.batch_snapshots([original])
    api.missing = True
    result = client.batch_snapshots([original])[0]
    assert result["market"]["status"] == "unavailable"
    assert result["quotes"] == {} and result["quote_ready"] is False
    assert result["compatibility_reason"] == "market_missing_from_refresh"


def test_failed_batch_book_does_not_trigger_thirty_sequential_fallbacks(adapter):
    client, api, _, _ = adapter
    api.book_error = True
    result = client.batch_snapshots([api.current])[0]
    assert result["quotes"] == {}
    assert "batch_orderbook_unavailable" in result["rejections"]["YES"]
    assert api.calls["orderbooks"] == 1 and api.calls["orderbook"] == 0


def test_final_window_quotes_a_recovered_reference_without_waiting_for_status_change(adapter):
    client, api, refs, _ = adapter
    refs.unavailable = True
    first = client.batch_snapshots([api.current], final_window=True)[0]
    assert first["quotes"] == {} and api.current["ticker"] not in client._shortlist
    refs.unavailable = False
    recovered = client.batch_snapshots([api.current], final_window=True)[0]
    assert recovered["market"]["status"] == first["market"]["status"]
    assert "YES" in recovered["quotes"]
    assert api.current["ticker"] in api.book_requests[-1]
    assert api.calls["orderbook"] == 0


def test_official_settlement_uses_a_fresh_market_read_and_close_cleans_up(adapter):
    client, api, refs, _ = adapter
    api.current.update(status="finalized", result="no")
    assert client.official_market(api.current["ticker"])["result"] == "no"
    assert api.calls["market"] == 1
    client.close()
    assert refs.closed is True


@pytest.mark.parametrize("close", [
    "2026-10-29T17:30:00Z", "2026-10-29T17:30:00.001Z", "2026-10-29T13:30:00-04:00",
    1793295000, 1793295000.001, "1793295000.001",
])
def test_saved_round_bucket_matches_the_engine_ceiling_boundary(close):
    assert canonical_round_key(close) == round_key(close)
