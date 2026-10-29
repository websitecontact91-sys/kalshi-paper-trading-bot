"""Market API boundary tests; every HTTP response here is synthetic and local."""

from decimal import Decimal as D

import pytest
import requests

from kalshi_client import Kalshi, ask_and_depth, decimal_value, implied_ask


class Response:
    def __init__(self, text="{}", status=200, headers=None):
        self.text, self.status_code, self.headers = text, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)


def test_json_fixed_point_values_never_pass_through_binary_floats():
    session = Session(Response('{"price":0.8700000000000000001,"quantity":5.69,"count":7}'))
    data = Kalshi(session=session).get("/markets")
    assert data == {"price": "0.8700000000000000001", "quantity": "5.69", "count": 7}
    assert decimal_value(data["price"]) == D("0.8700000000000000001")
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("value", [0.87, True, None, "NaN", "Infinity", "-Infinity"])
def test_inexact_or_nonfinite_prices_are_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        decimal_value(value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_rejected(constant):
    with pytest.raises(ValueError):
        Kalshi(session=Session(Response('{"price":' + constant + '}'))).get("/markets")


def test_fixed_point_ask_is_preferred_and_legacy_fallback_requires_integer_cents():
    assert implied_ask({"yes_ask_dollars": "0.8751", "yes_ask": 87}, "YES") == D("0.8751")
    assert implied_ask({"no_ask": 14}, "NO") == D("0.14")
    for value in (14.0, "14", True, None):
        with pytest.raises(ValueError):
            implied_ask({"no_ask": value}, "NO")
    with pytest.raises(ValueError):
        implied_ask({"yes_ask": 87}, "MAYBE")


def test_executable_depth_is_only_the_best_opposite_bid_level():
    book = {"orderbook_fp": {
        "no_dollars": [["0.10", "1000"], ["0.13", "1.50"], ["0.13", "2.25"]],
        "yes_dollars": [["0.86", "9.01"]],
    }}
    assert ask_and_depth(book, "YES", "0.87") == (D("0.87"), D("3.75"))
    assert ask_and_depth(book, "NO", "0.14") == (D("0.14"), D("9.01"))
    with pytest.raises(ValueError, match="quote_changed"):
        ask_and_depth(book, "YES", "0.86")


def test_legacy_book_cents_and_empty_or_zero_depth_are_handled_explicitly():
    book = {"orderbook": {"no": [[13, "2.5"], [12, "100"]]}}
    assert ask_and_depth(book, "YES", "0.87") == (D("0.87"), D("2.5"))
    for book in ({}, {"orderbook_fp": {"yes_dollars": [["0.86", "100"]]}},
                 {"orderbook_fp": {"no_dollars": [["0.13", "0"]]}}):
        with pytest.raises(ValueError, match="no_depth"):
            ask_and_depth(book, "YES", "0.87")


@pytest.mark.parametrize("path", [
    "/markets", "/markets/KXSOL15M-26OCT291730", "/markets/KXSOL15M-26OCT291730/orderbook",
    "/markets/orderbooks", "/series", "/series/KXSOL15M", "/events/KXSOL15M-26OCT291730",
    "/cfbenchmarks/values",
])
def test_only_approved_read_endpoints_are_requested(path):
    session = Session(Response())
    assert Kalshi(session=session).get(path) == {}
    assert session.calls[0][0].endswith(path)
    assert session.calls[0][1]["headers"] == {"Accept": "application/json"}


@pytest.mark.parametrize("path", [
    "/account", "/" + "portfolio" + "/" + "orders", "/markets/X/trades",
    "/markets/../../account", "/markets/X?redirect=1", "https://example.com/markets",
    "/markets/X/orderbook/", "/markets/X%2faccount", "/markets/X\n",
])
def test_disallowed_endpoints_fail_before_any_request(path):
    session = Session()
    with pytest.raises(ValueError, match="allowlist"):
        Kalshi(session=session).get(path)
    assert session.calls == []


@pytest.mark.parametrize("url", [
    "http://external-api.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com.evil.example/trade-api/v2",
    "https://user@external-api.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com:8443/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2?key=1",
    "https://external-api.kalshi.com/trade-api/v2#fragment",
    "https://external-api.kalshi.com/not-the-api",
])
def test_unapproved_api_origins_are_rejected(url):
    with pytest.raises(ValueError):
        Kalshi(url, session=Session())


def test_redirect_cannot_escape_the_allowlist():
    session = Session(Response(status=302, headers={"Location": "https://example.com/"}))
    with pytest.raises(ValueError, match="redirect"):
        Kalshi(session=session).get("/markets")
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("header,expected_delay", [("120", 2.0), ("0", 0.25), ("bad-date", 1.0)])
def test_429_retries_once_with_bounded_delay(header, expected_delay):
    session = Session(Response(status=429, headers={"Retry-After": header}), Response('{"ok":true}'))
    delays = []
    assert Kalshi(session=session, sleep=delays.append).get("/markets") == {"ok": True}
    assert delays == [expected_delay]
    assert len(session.calls) == 2


def test_repeated_rate_limit_fails_without_an_unbounded_loop():
    session = Session(Response(status=429), Response(status=429))
    delays = []
    with pytest.raises(requests.HTTPError):
        Kalshi(session=session, sleep=delays.append).get("/markets")
    assert delays == [1.0]
    assert len(session.calls) == 2


def test_server_error_retries_but_auth_failure_does_not():
    session = Session(Response(status=503), Response('{"recovered":true}'))
    assert Kalshi(session=session, sleep=lambda _: None).get("/markets")["recovered"] is True
    denied = Session(Response(status=401))
    with pytest.raises(requests.HTTPError):
        Kalshi(session=denied, sleep=lambda _: pytest.fail("auth error retried")).get("/markets")
    assert len(denied.calls) == 1


def test_discovery_filters_closed_or_expired_markets_and_follows_cursor(monkeypatch):
    client = Kalshi(session=Session())
    requests_seen = []
    pages = [
        {"markets": [{"ticker": "A", "status": "active", "close_time": 1793304090},
                     {"ticker": "EXPIRED", "status": "active", "close_time": 1793304000},
                     {"ticker": "CLOSED", "status": "closed", "close_time": 1793304090}],
         "cursor": "next"},
        {"markets": [{"ticker": "B", "status": "open", "close_time": 1793304100}]},
    ]

    def get(path, params):
        requests_seen.append((path, params.copy()))
        return pages.pop(0)

    monkeypatch.setattr(client, "get", get)
    rows = client.open_markets("KXSOL15M", 1793304000)
    assert [row["ticker"] for row in rows] == ["A", "B"]
    assert all(row["series_ticker"] == "KXSOL15M" for row in rows)
    assert "cursor" not in requests_seen[0][1]
    assert requests_seen[1][1]["cursor"] == "next"


def test_repeated_cursor_and_truncated_batch_are_failures(monkeypatch):
    client = Kalshi(session=Session())
    monkeypatch.setattr(client, "get", lambda *args: {"markets": [], "cursor": "repeat"})
    with pytest.raises(ValueError, match="repeated"):
        client.open_markets("KXSOL15M", 1793304000)
    with pytest.raises(ValueError, match="truncated"):
        client.refresh_markets(["KXSOL15M-EXAMPLE"])


def test_empty_batches_make_no_request():
    session = Session()
    client = Kalshi(session=session)
    assert client.refresh_markets([]) == {}
    assert client.orderbooks([]) == {}
    assert session.calls == []
