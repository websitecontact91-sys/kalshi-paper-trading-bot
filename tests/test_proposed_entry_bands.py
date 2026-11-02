"""Review-only specification for proposed live entry-band expansion.

This file intentionally does not modify production trading constants.
It documents and tests the requested target bands:
- ask: 0.80 through 0.95 inclusive
- payout multiple: 1.05 through 1.20 inclusive
"""

from decimal import Decimal as D

import pytest


PROPOSED_MIN_ASK = D("0.80")
PROPOSED_MAX_ASK = D("0.95")
PROPOSED_MIN_MULT = D("1.05")
PROPOSED_MAX_MULT = D("1.20")


def proposed_ask_in_band(value):
    v = D(str(value))
    return PROPOSED_MIN_ASK <= v <= PROPOSED_MAX_ASK


def proposed_multiplier_in_band(value):
    v = D(str(value))
    return PROPOSED_MIN_MULT <= v <= PROPOSED_MAX_MULT


@pytest.mark.parametrize(
    "ask,ok",
    [
        ("0.79", False),
        ("0.80", True),
        ("0.90", True),
        ("0.91", True),
        ("0.95", True),
        ("0.951", False),
        ("0.96", False),
    ],
)
def test_proposed_ask_band_is_080_through_095_inclusive(ask, ok):
    assert proposed_ask_in_band(ask) is ok


@pytest.mark.parametrize(
    "multiplier,ok",
    [
        ("1.04", False),
        ("1.049", False),
        ("1.05", True),
        ("1.10", True),
        ("1.20", True),
        ("1.201", False),
        ("1.21", False),
    ],
)
def test_proposed_payout_band_is_105_through_120_inclusive(multiplier, ok):
    assert proposed_multiplier_in_band(multiplier) is ok


def test_only_requested_band_edges_change():
    assert PROPOSED_MIN_ASK == D("0.80")
    assert PROPOSED_MAX_ASK == D("0.95")
    assert PROPOSED_MIN_MULT == D("1.05")
    assert PROPOSED_MAX_MULT == D("1.20")
