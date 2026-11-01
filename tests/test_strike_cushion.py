from decimal import Decimal

import pytest

from strike_cushion import (
    MIN_STRIKE_CUSHION_PCT,
    StrikeCushionError,
    meets_min_strike_cushion,
    strike_cushion_pct,
)


def current_for_pct(strike, pct, *, above=True):
    strike = Decimal(str(strike))
    pct = Decimal(str(pct))
    delta = strike * pct / Decimal("100")
    return strike + delta if above else strike - delta


@pytest.mark.parametrize("above", [True, False])
def test_just_below_threshold_is_rejected(above):
    strike = Decimal("100")
    current = current_for_pct(strike, "0.01199", above=above)
    assert meets_min_strike_cushion(current, strike) is False


@pytest.mark.parametrize("above", [True, False])
def test_exact_threshold_is_accepted(above):
    strike = Decimal("100")
    current = current_for_pct(strike, MIN_STRIKE_CUSHION_PCT, above=above)
    assert strike_cushion_pct(current, strike) == MIN_STRIKE_CUSHION_PCT
    assert meets_min_strike_cushion(current, strike) is True


@pytest.mark.parametrize("above", [True, False])
def test_above_threshold_is_accepted(above):
    strike = Decimal("100")
    current = current_for_pct(strike, "0.01201", above=above)
    assert meets_min_strike_cushion(current, strike) is True


def test_recent_btc_loss_example_would_fail_reference_gate():
    assert meets_min_strike_cushion("77665.08", "77670.98") is False


def test_nonzero_strike_required():
    with pytest.raises(StrikeCushionError):
        meets_min_strike_cushion("1", "0")
