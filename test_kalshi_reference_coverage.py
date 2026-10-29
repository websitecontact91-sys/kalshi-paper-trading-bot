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

def test_all_30_mapped_series_reach_entry_evaluation():
    """Each mapped series can produce a paper candidate with valid Kalshi market inputs."""
    from unittest.mock import patch
    from paper_runner import evaluate_market
    now = time.time()
    for series in list(CF_INDEX_IDS) + list(PYTH_INDEX_IDS):
        market = {
            "ticker": series + "-TEST", "series_ticker": series,
            "status": "open", "strike_type": "greater_or_equal",
            "floor_strike": 100.0, "close_time": now + 45,
            "yes_ask_dollars": "0.85", "no_ask_dollars": "0.15",
        }
        with patch("paper_runner.best_bid_depth", return_value=20):
            candidates = evaluate_market(market, 105.0, now, {}, 1000.0)
        assert len(candidates) == 1, series
        assert candidates[0]["side"] == "YES", series
        assert candidates[0]["cost"] <= 5, series
