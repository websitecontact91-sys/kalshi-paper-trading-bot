from decimal import Decimal as D

import pytest

from payout_quote import PayoutQuote
from selection import Candidate, distance_pct, eligible, rank, rejection_reason, select


def C(ticker="A", side="YES", ask="0.87", current="101", target="100",
      payout="1.138", fresh=True):
    return Candidate(ticker, side, D(ask), D(current), D(target),
                     PayoutQuote.from_kalshi("1.00", "1.00", payout), fresh)


def test_distance_ranks_only_primary_filter_passes():
    winner = C("eligible", current="101")
    bad_ask = C("bad-ask", ask="0.79", current="120")
    bad_payout = C("bad-payout", payout="1.30", current="130")
    assert select([bad_ask, bad_payout, winner]) == winner


@pytest.mark.parametrize("current", ["99", "100"])
def test_single_negative_or_zero_distance_candidate_still_selected(current):
    candidate = C(current=current)
    assert distance_pct(candidate) <= 0
    assert select([candidate]) == candidate


def test_largest_signed_distance_not_payout_is_primary_rank():
    larger_margin = C("margin", current="102", payout="1.10")
    larger_payout = C("payout", current="101", payout="1.20")
    assert select([larger_payout, larger_margin]) == larger_margin


def test_equal_distance_prefers_exact_payout_then_ticker_and_side():
    low_payout = C("A", current="100", payout="1.10")
    high_yes = C("A", current="100", payout="1.138")
    high_no = C("A", side="NO", current="100", payout="1.138")
    high_late_ticker = C("Z", current="100", payout="1.138")
    assert rank([high_late_ticker, high_yes, low_payout, high_no]) == [
        high_no, high_yes, high_late_ticker, low_payout]
    assert select([high_yes, high_no]) is high_no


def test_no_eligible_candidate_has_explicit_reasons():
    stale = C(fresh=False)
    bad_ask = C(ask="0.91")
    bad_payout = C(payout="1.00")
    assert select([stale, bad_ask, bad_payout]) is None
    assert select([]) is None
    assert rejection_reason(stale) == "stale_or_closed"
    assert rejection_reason(bad_ask) == "ask_out_of_band"
    assert rejection_reason(bad_payout) == "payout_out_of_band"


def test_yes_and_no_worked_distances():
    assert distance_pct(C(current="105", target="100")) == D("5")
    assert distance_pct(C(side="NO", current="95", target="100")) == D("5")
    assert distance_pct(C(side="NO", current="105", target="100")) == D("-5")


@pytest.mark.parametrize("kwargs", [
    {"side": "up"}, {"side": ""}, {"target": "0"}, {"target": "-1"},
    {"target": "NaN"}, {"current": "Infinity"}, {"current": "0"},
    {"ask": "0"}, {"ask": "1"}, {"ticker": ""},
])
def test_invalid_candidate_rejected(kwargs):
    with pytest.raises(ValueError):
        C(**kwargs)


def test_only_boolean_freshness_is_accepted():
    with pytest.raises(TypeError):
        C(fresh="true")
