"""Owner-supplied payout examples and monetary validation regressions."""

from decimal import Decimal as D

import pytest

from payout_quote import PayoutQuote, ask_in_band, payout_eligible, strike_cushion_meets_minimum


def Q(debit, payout, budget="5.00"):
    return PayoutQuote.from_kalshi(budget, debit, payout)


def test_screenshot_5_69_is_1_14x():
    q = Q("5.00", "5.69")
    assert q.payout == D("5.69")
    assert q.exact_multiplier == D("1.138")
    assert q.displayed_multiplier == D("1.14")
    assert payout_eligible(q)


def test_regression_displayed_times_budget_is_the_bug():
    q = Q("5.00", "5.69")
    assert q.displayed_multiplier * q.budget == D("5.70")
    assert q.payout != q.displayed_multiplier * q.budget


@pytest.mark.parametrize("payout,ok", [
    ("5.4749", False), ("5.50", True), ("5.475", True),
    ("6.00", True), ("6.024", True), ("6.025", False), ("5.00", False),
])
def test_payout_boundaries(payout, ok):
    assert payout_eligible(Q("5.00", payout)) is ok


def test_old_090_ask_plus_010_reserve_cannot_qualify():
    q = Q("5.00", "5.00")
    assert q.exact_multiplier == D("1")
    assert not payout_eligible(q)


@pytest.mark.parametrize("ask,ok", [("0.79", False), ("0.80", True),
                                    ("0.90", True), ("0.91", False)])
def test_ask_band_inclusive(ask, ok):
    assert ask_in_band(ask) is ok


def test_no_overspend_and_unused_cash():
    assert Q("4.80", "5.50").unused_cash == D("0.20")
    with pytest.raises(ValueError):
        Q("5.01", "5.70")


def test_pnl_and_floats_rejected():
    q = Q("5.00", "5.69")
    assert q.pnl(True) == D("0.69") and q.pnl(False) == D("-5.00")
    with pytest.raises(TypeError):
        PayoutQuote.from_kalshi(5.0, 5.0, 5.69)


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", "not-a-number"])
def test_non_finite_or_invalid_money_rejected(bad):
    with pytest.raises(ValueError):
        Q("5.00", bad)


@pytest.mark.parametrize("debit,payout,budget", [
    ("0", "5.69", "5"), ("-1", "5.69", "5"),
    ("5", "0", "5"), ("5", "-1", "5"), ("1", "1", "0"),
])
def test_invalid_money_ranges_rejected(debit, payout, budget):
    with pytest.raises(ValueError):
        Q(debit, payout, budget)


def test_direct_dataclass_construction_cannot_bypass_validation():
    with pytest.raises(ValueError):
        PayoutQuote(D("5"), D("6"), D("6.50"))


def test_true_is_not_a_monetary_amount_or_a_settlement_string():
    with pytest.raises(TypeError):
        Q(True, "5.69")
    with pytest.raises(TypeError):
        Q("5", "5.69").pnl("yes")


def test_multiplier_uses_actual_debit_not_unused_budget():
    q = Q("4.80", "5.50")
    assert q.displayed_multiplier == D("1.15")
    assert q.pnl(True) == D("0.70")



@pytest.mark.parametrize("current", ["100012", "99988"])
def test_strike_cushion_exact_boundary_passes_both_directions(current):
    assert strike_cushion_meets_minimum(current, "100000")


@pytest.mark.parametrize("current", ["100011.99", "99988.01", "100000"])
def test_strike_cushion_below_minimum_is_rejected(current):
    assert not strike_cushion_meets_minimum(current, "100000")


def test_strike_cushion_clearly_above_minimum_passes():
    assert strike_cushion_meets_minimum("100500", "100000")


def test_strike_cushion_is_symmetric_around_target():
    assert strike_cushion_meets_minimum("100020", "100000")
    assert strike_cushion_meets_minimum("99980", "100000")


@pytest.mark.parametrize("bad", [None, 0, -1, "NaN", "Infinity", "abc", ""])
def test_strike_cushion_fails_closed_on_invalid_current(bad):
    assert not strike_cushion_meets_minimum(bad, "100000")


@pytest.mark.parametrize("bad", [None, 0, -1, "NaN", "Infinity", "abc", ""])
def test_strike_cushion_fails_closed_on_invalid_target(bad):
    assert not strike_cushion_meets_minimum("100020", bad)
