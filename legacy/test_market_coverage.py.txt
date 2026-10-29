"""Offline checks for Kalshi-only reference mapping coverage."""
from market_coverage import CF, classification
from kalshi_pyth import PYTH_INDEX_IDS

def test_known_cf_markets_mapped():
    assert len(CF) == 11
    for name in CF:
        assert classification(name) == "KALSHI_CF_INDEX_MAPPED"

def test_pyth_mapped_without_claiming_contract_validation():
    assert len(PYTH_INDEX_IDS) == 19
    for name in PYTH_INDEX_IDS:
        assert classification(name) == "KALSHI_PYTH_STREAM_MAPPED_UNVERIFIED_CONTRACT"

def test_unknown_market_not_silently_enabled():
    assert classification("KXUNSEEN15M") == "NO_VERIFIED_SETTLEMENT_FEED"
