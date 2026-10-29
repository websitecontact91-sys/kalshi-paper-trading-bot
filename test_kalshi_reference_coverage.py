"""Offline regression tests for Kalshi-only reference integration."""
import time
import pytest
from kalshi_pyth import KalshiPythPrices, PYTH_INDEX_IDS
from paper_runner import CF_INDEX_IDS, candidate_is_fresh

def test_exact_coverage_and_exclusions():
    assert len(CF_INDEX_IDS) == 11
    assert len(PYTH_INDEX_IDS) == 19
    assert not set(CF_INDEX_IDS) & set(PYTH_INDEX_IDS)
    for name in ("KXTON15M","KXCOFFEE15M","KXINX15M","KXNDQ15M","KXCRYPTOCOMP15M"):
        assert name not in CF_INDEX_IDS
        assert name not in PYTH_INDEX_IDS

def test_pyth_freshness_and_missing_fail_closed():
    feed = object.__new__(KalshiPythPrices)
    import threading
    feed.lock = threading.Lock()
    feed.values = {}
    with pytest.raises(ValueError, match="No Kalshi Pyth observation"):
        feed.price("KXGOLD15M")
    key = PYTH_INDEX_IDS["KXGOLD15M"]
    feed.values[key] = (2500.0, time.time()-20)
    with pytest.raises(ValueError, match="Stale"):
        feed.price("KXGOLD15M")
    feed.values[key] = (2500.0, time.time())
    price, ts = feed.price("KXGOLD15M")
    assert price == 2500.0
    assert candidate_is_fresh({"index_time":ts},time.time())
