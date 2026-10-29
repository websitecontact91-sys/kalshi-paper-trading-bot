"""Offline checks for the all-market coverage classification."""
from market_coverage import CF, PYTH_UNVERIFIED, classification


def test_all_known_cf_markets_mapped():
    assert len(CF) == 4
    for name in CF:
        assert classification(name) == "CF_ENDPOINT_TESTED"


def test_pyth_not_mistaken_for_working():
    assert len(PYTH_UNVERIFIED) == 5
    for name in PYTH_UNVERIFIED:
        assert classification(name) == "PYTH_UNAVAILABLE_OR_UNVERIFIED"


def test_unknown_market_is_not_silently_enabled():
    assert classification("KXUNSEEN15M") == "NO_VERIFIED_SETTLEMENT_FEED"
