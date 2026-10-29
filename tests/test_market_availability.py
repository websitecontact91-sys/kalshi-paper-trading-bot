"""Markets may appear or disappear without changing the configured universe."""
import json
from decimal import Decimal
from pathlib import Path

from live_client import LiveKalshiClient
from payout_quote import PayoutQuote
from quote_adapter import QuoteDetails
from universe import UNIVERSE, check_compatibility
from validate_universe import build_report


def contract(series, now=1000):
    return {
        "ticker": series + "-EXAMPLE", "series_ticker": series,
        "status": "open", "open_time": now - 10, "close_time": now + 300,
        "market_type": "binary", "notional_value_dollars": "1.0000",
        "strike_type": "greater_or_equal", "floor_strike": "100",
    }


def test_live_discovery_rechecks_absent_series_and_picks_up_new_market(tmp_path):
    class API:
        available = {"KXBTC15M"}

        def __init__(self):
            self.calls = []

        def open_markets(self, series, now):
            self.calls.append(series)
            return [contract(series, now)] if series in self.available else []

    now = [1000]
    api = API()
    client = LiveKalshiClient(tmp_path / "missing.json", api=api, references=object(), clock=lambda: now[0])
    assert {m["series_ticker"] for m in client.discover()} == {"KXBTC15M"}
    assert set(api.calls) == set(UNIVERSE) and len(api.calls) == 30
    api.available = {"KXSOL15M", "KXDJIA15M"}
    now[0] += 59
    assert {m["series_ticker"] for m in client.discover()} == {"KXBTC15M"}
    now[0] += 1
    assert {m["series_ticker"] for m in client.discover()} == {"KXSOL15M", "KXDJIA15M"}
    assert len(api.calls) == 60
    assert set(api.calls[30:]) == set(UNIVERSE)
    assert len(UNIVERSE) == 30 and not client.discovery_errors


def test_api_paper_mode_does_not_need_ui_proof_or_all_markets_open():
    class Client:
        policy = {
            "mode": "documented_api", "ready": True, "verified": False,
            "ui_parity_verified": False, "reason": None,
            "balance_precision": "0.0001", "paper_balance_model": "direct_member",
        }
        readiness_reasons = []
        discovery_errors = {}

        def discover(self, now=None):
            return [contract("KXBTC15M")]

        def series_metadata(self, series):
            return {"ticker": series, "settlement_sources": [{"name": "CF Benchmarks"}]}

        def snapshot(self, market, **kwargs):
            detail = QuoteDetails(
                quote=PayoutQuote.from_kalshi("5", "5", "5.69"),
                quantity=Decimal("5.69"), fees=Decimal("0.0497"), principal=Decimal("4.9503"),
                source="synthetic_test_only", ui_parity_verified=False,
            )
            return {
                "market": market, "current": Decimal("101"), "observation_time": 999,
                "quotes": {"YES": detail}, "asks": {"YES": "0.87", "NO": "0.13"},
                "rejections": {"NO": "ask_out_of_band"}, "quote_ready": True,
            }

    result = build_report(Client(), now=1000, source_sha="a" * 40)
    assert result["ready"]
    assert result["readiness_blockers"] == []
    assert result["counts"]["configured"] == 30
    assert result["counts"]["available"] == result["counts"]["eligible"] == 1
    assert result["quote_policy"]["ready"] and not result["quote_policy"]["verified"]
    assert not result["quote_parity"]["required"]
    assert not result["quote_parity"]["verified"]
    missing = [row for row in result["rows"] if not row["available"]]
    assert len(missing) == 29
    assert all(row["configured"] and row["reason"] == "no_current_market" for row in missing)
    assert not any(row["ui_parity_verified"] for row in result["rows"])


def test_captured_dow_catalog_absence_does_not_establish_a_live_contract():
    path = Path(__file__).parent / "fixtures/kalshi_dow_catalog_20261029.json"
    capture = json.loads(path.read_text())
    responses = capture["responses"]
    assert all(response["status"] == 200 for response in responses.values())
    assert responses["all_markets"]["data"]["markets"] == []
    assert responses["open_markets"]["data"]["markets"] == []
    official = responses["series"]["data"]["series"]
    assert official["ticker"] == "KXDJIA15M"
    # An actual catalog entry does not make its absent contract tradable or
    # make an unrelated catalog source link the configured Pyth observation.
    hypothetical = contract("KXDJIA15M")
    assert check_compatibility(hypothetical, Decimal("101"), official) == (
        False, "official_settlement_source_mismatch_or_unverified",
    )
