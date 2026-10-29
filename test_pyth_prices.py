"""Tests for the authenticated Pyth adapter; no network or secrets needed."""
from unittest.mock import patch
import pytest
from pyth_prices import PythPrices

ID = "a" * 64


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"parsed": [{"id": ID, "price": {
            "price": "234567", "expo": -3, "publish_time": 1000}}]}


class Session:
    def get(self, *args, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["params"]["ids[]"] == ID
        return Response()


def test_authenticated_reference_price():
    with patch("pyth_prices.time.time", return_value=1002):
        assert PythPrices({"KXGOLD15M": ID}, "test-key", Session()).price(
            "KXGOLD15M") == (234.567, 1000.0)


def test_unknown_feed_fails_closed():
    with pytest.raises(ValueError, match="verified Pyth feed"):
        PythPrices({}, "test-key", Session()).price("KXUNKNOWN15M")


def test_missing_key_fails_closed():
    with pytest.raises(RuntimeError, match="PYTH_API_KEY"):
        PythPrices({"KXGOLD15M": ID}, "", Session()).price("KXGOLD15M")


def test_stale_quote_fails_closed():
    with patch("pyth_prices.time.time", return_value=1030):
        with pytest.raises(ValueError, match="Stale"):
            PythPrices({"KXGOLD15M": ID}, "test-key", Session()).price(
                "KXGOLD15M")
