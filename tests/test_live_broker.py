"""Offline account transport tests. Every HTTP response is a synthetic fixture."""
import base64
import json
from decimal import Decimal as D

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from live_broker import (AmbiguousOrderError, BrokerError, CredentialsError, DEMO_BASE,
                         LiveBroker, OrderRejected, TradingDisabled,
                         max_affordable_quantity, worst_case_debit)
from quote_adapter import documented_taker_cost


class Response:
    def __init__(self, data=None, *, text=None, status=200, headers=None):
        self.text = text if text is not None else json.dumps(data or {})
        self.status_code, self.headers = status, headers or {}


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def _call(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected account request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        return self._call("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, kwargs)


@pytest.fixture(autouse=True)
def no_real_network_or_credentials(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("A live broker test attempted real network access")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def broker(session=None, **kwargs):
    key = kwargs.pop("private_key", ed25519.Ed25519PrivateKey.generate())
    return LiveBroker(session=Session() if session is None else session,
                      api_key_id=kwargs.pop("api_key_id", "test-key"), private_key=key,
                      clock=lambda: 1793307000.123, **kwargs)


def request(**overrides):
    values = dict(ticker="KXBTC15M-26OCT291700-00", side="YES", quantity="1.00",
                  price="0.89", client_order_id="stable-client-id", expected_debit="0.90",
                  fee_multiplier="1", balance_precision="0.01")
    values.update(overrides)
    return values


def ack(**overrides):
    data = {"order_id": "order-1", "client_order_id": "stable-client-id",
            "fill_count": "1.00", "remaining_count": "0.00", "ts_ms": 1793307000125}
    data.update(overrides)
    return Response(data, status=201)


def test_writes_are_off_by_default_even_with_valid_credentials():
    session = Session()
    with pytest.raises(TradingDisabled):
        broker(session).submit_limit_buy(**request())
    assert not session.calls


@pytest.mark.parametrize("key", [ed25519.Ed25519PrivateKey.generate(),
                                   rsa.generate_private_key(public_exponent=65537, key_size=2048)])
def test_signature_covers_method_and_path_without_query(key):
    session = Session(Response({"balance_dollars": "19.8499", "portfolio_value": 9,
                                "updated_ts": 1793307000}))
    client = broker(session, private_key=key)
    assert client.balance()["available_balance_usd"] == "19.8499"
    method, url, options = session.calls[0]
    assert method == "GET" and url.endswith("/portfolio/balance")
    assert options["params"] == {"subaccount": 0}
    headers = options["headers"]
    message = b"1793307000123GET/trade-api/v2/portfolio/balance"
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    if isinstance(key, ed25519.Ed25519PrivateKey):
        key.public_key().verify(signature, message)
    else:
        key.public_key().verify(signature, message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())
    assert options["allow_redirects"] is False


@pytest.mark.parametrize("side,book_side,yes_price", [("YES", "bid", "0.8900"), ("NO", "ask", "0.1100")])
def test_v2_maps_selected_leg_to_yes_scale_and_fok(side, book_side, yes_price):
    session = Session(ack())
    key = ed25519.Ed25519PrivateKey.generate()
    result = broker(session, allow_trading=True, private_key=key).submit_limit_buy(**request(side=side))
    assert result["order_id"] == "order-1"
    method, url, options = session.calls[0]
    assert method == "POST" and url.endswith("/portfolio/events/orders")
    assert options["json"] == {"ticker": "KXBTC15M-26OCT291700-00", "side": book_side,
        "count": "1.00", "price": yes_price, "client_order_id": "stable-client-id",
        "time_in_force": "fill_or_kill", "self_trade_prevention_type": "taker_at_cross",
        "post_only": False, "reduce_only": False, "cancel_order_on_pause": True, "subaccount": 0}
    assert "buy_max_cost" not in options["json"]
    assert options["allow_redirects"] is False
    key.public_key().verify(base64.b64decode(options["headers"]["KALSHI-ACCESS-SIGNATURE"]),
        b"1793307000123POST/trade-api/v2/portfolio/events/orders")


@pytest.mark.parametrize("status", [301, 302, 307, 408, 409, 500, 503])
def test_ambiguous_http_outcome_never_retries_or_follows_redirect(status):
    session = Session(Response(status=status, headers={"Location": "https://attacker.example"}))
    with pytest.raises(AmbiguousOrderError):
        broker(session, allow_trading=True).submit_limit_buy(**request())
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429])
def test_explicit_order_rejection_never_retries(status):
    session = Session(Response({"message": "sensitive details"}, status=status))
    with pytest.raises(OrderRejected) as caught:
        broker(session, allow_trading=True).submit_limit_buy(**request())
    assert caught.value.status_code == status
    assert "sensitive" not in str(caught.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("error", [requests.Timeout("secret request context"),
                                   requests.ConnectionError("secret request context")])
def test_lost_acknowledgement_never_retries_and_sanitizes_error(error):
    session = Session(error)
    with pytest.raises(AmbiguousOrderError) as caught:
        broker(session, allow_trading=True).submit_limit_buy(**request())
    assert "secret" not in str(caught.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("response", [Response(text="not JSON", status=201),
    Response(text='{"number":NaN}', status=201), ack(client_order_id="other-client"),
    ack(fill_count="2.00"), ack(fill_count="0.001"), ack(fill_count=1),
    ack(fill_count="1.00", remaining_count="1.00"), ack(ts_ms=True), ack(order_id="../other")])
def test_malformed_success_acknowledgement_is_ambiguous(response):
    session = Session(response)
    with pytest.raises(AmbiguousOrderError):
        broker(session, allow_trading=True).submit_limit_buy(**request())
    assert len(session.calls) == 1


def test_partial_ack_is_returned_for_owned_position_reconciliation_not_retried():
    session = Session(ack(fill_count="0.40", remaining_count="0.00"))
    assert broker(session, allow_trading=True).submit_limit_buy(**request())["fill_count"] == "0.40"
    assert len(session.calls) == 1


def test_optional_ack_client_id_is_normalized_to_the_submitted_identity():
    data = json.loads(ack().text)
    del data["client_order_id"]
    session = Session(Response(data, status=201))
    received = broker(session, allow_trading=True).submit_limit_buy(**request())
    assert received["client_order_id"] == "stable-client-id"
    assert received["order_id"] == "order-1"
    assert received["fill_count"] == "1.00"
    assert len(session.calls) == 1


@pytest.mark.parametrize("echoed", [None, "another-client-id"])
def test_explicitly_wrong_or_null_ack_client_id_is_not_normalized(echoed):
    session = Session(ack(client_order_id=echoed))
    with pytest.raises(AmbiguousOrderError):
        broker(session, allow_trading=True).submit_limit_buy(**request())
    assert len(session.calls) == 1


@pytest.mark.parametrize("changes", [
    {"price": "0.79"}, {"price": "0.91"}, {"price": 0.89}, {"price": "NaN"},
    {"quantity": "1.001"}, {"quantity": "0"}, {"quantity": True},
    {"expected_debit": "1.01"}, {"expected_debit": "0.89"}, {"expected_debit": 0.90},
    {"fee_multiplier": "-1"}, {"balance_precision": "0.001"},
    {"ticker": "X/../../account"}, {"client_order_id": "id\nsecret"}, {"side": "SELL"},
])
def test_invalid_order_parameters_fail_before_any_write(changes):
    session = Session()
    with pytest.raises(BrokerError):
        broker(session, allow_trading=True).submit_limit_buy(**request(**changes))
    assert not session.calls


def test_rounding_guard_rejects_quote_that_single_fill_model_would_allow():
    session = Session()
    # 1.10 * .90 plus one-fill fees is .9970, but worst fragmented direct-member
    # debit is 110 * .0091 = 1.0010. The submitted quantity must be reduced.
    assert documented_taker_cost(D(".90"), D("1.10"), D(1), D(".0001"))[2] == D(".9970")
    assert worst_case_debit(".90", "1.10", "1", ".0001") == D("1.0010")
    with pytest.raises(BrokerError, match="Worst-case"):
        broker(session, allow_trading=True).submit_limit_buy(**request(price=".90", quantity="1.10",
            expected_debit=".9970", balance_precision=".0001"))
    assert not session.calls
    assert max_affordable_quantity(".90", "100", "1", ".0001") == D("1.09")
    assert max_affordable_quantity(".90", "100", "1") == D("1.00")


def test_payout_band_is_rechecked_at_submission():
    with pytest.raises(BrokerError, match="payout"):
        broker(allow_trading=True).submit_limit_buy(**request(price=".80", expected_debit=".82"))


def test_cost_bound_covers_price_improvement_with_high_fee_multiplier():
    # With a large multiplier, greatest debit can be below the stated limit.
    bound = worst_case_debit(".90", "1.00", "50", ".0001")
    for price in (D(".10"), D(".50"), D(".6429"), D(".80"), D(".90")):
        atomic_debit = documented_taker_cost(price, D(".01"), D(50), D(".0001"))[2]
        assert bound >= atomic_debit * 100
    assert bound > documented_taker_cost(D(".9"), D(".01"), D(50), D(".0001"))[2] * 100


@pytest.mark.parametrize("args", [(".89", "1.001", "1", ".01"),
                                  (".89", "1", "Infinity", ".01"),
                                  (".89", "1", "1", ".001"),
                                  (".89001", "1", "1", ".01")])
def test_cost_bound_rejects_invalid_money_inputs(args):
    with pytest.raises(BrokerError):
        worst_case_debit(*args)


def test_fractional_depth_and_budget_limits_are_respected():
    assert max_affordable_quantity(".89", ".129", "1") == D(".12")
    assert max_affordable_quantity(".89", ".001", "1") == D(0)
    assert max_affordable_quantity(".89", "10", "1", budget=".50") == D(".50")
    assert max_affordable_quantity(".89", "100", "1", budget="5.00") == D("5.00")
    assert max_affordable_quantity(".89", "100", "1", budget="10.00") == D("10.00")
    for amount in ("10.01", "0", "-1"):
        with pytest.raises(BrokerError):
            max_affordable_quantity(".89", "10", "1", budget=amount)


def test_find_order_paginates_to_completion_and_matches_client_id_locally():
    session = Session(Response({"orders": [{"ticker": "X", "client_order_id": "other"}], "cursor": "next"}),
        Response({"orders": [{"ticker": "X", "client_order_id": "wanted", "order_id": "id"}], "cursor": ""}))
    assert broker(session).find_order("wanted", "X")["order_id"] == "id"
    assert len(session.calls) == 2
    assert session.calls[1][2]["params"]["cursor"] == "next"
    assert all("client_order_id" not in call[2]["params"] for call in session.calls)
    assert all(call[2]["params"]["subaccount"] == 0 for call in session.calls)


def test_find_order_refuses_duplicate_identity_and_incomplete_pagination():
    row = {"ticker": "X", "client_order_id": "same"}
    with pytest.raises(BrokerError, match="not unique"):
        broker(Session(Response({"orders": [row, row]}))).find_order("same", "X")
    with pytest.raises(BrokerError, match="completeness"):
        broker(Session(Response({"orders": [row], "cursor": "more"})), max_pages=1).find_order("same", "X")
    with pytest.raises(BrokerError, match="Repeated"):
        broker(Session(Response({"orders": [], "cursor": "same"}),
                       Response({"orders": [], "cursor": "same"}))).orders()


def test_empty_complete_search_is_none_not_a_write_permission():
    session = Session(Response({"orders": [], "cursor": ""}))
    client = broker(session)
    assert client.find_order("unknown", "X") is None
    assert not client.writes_enabled


def test_account_get_retry_is_bounded_and_auth_is_not_retried():
    delays = []
    session = Session(Response(status=429, headers={"Retry-After": "999"}),
                      Response({"orders": []}))
    assert broker(session, sleep=delays.append).orders() == []
    assert delays == [2.0]
    session = Session(Response(status=401))
    with pytest.raises(BrokerError, match="HTTP 401"):
        broker(session).orders()
    assert len(session.calls) == 1


def test_redirected_read_does_not_expose_credentials_elsewhere():
    session = Session(Response(status=302, headers={"Location": "https://attacker.example"}))
    with pytest.raises(BrokerError, match="redirect"):
        broker(session).orders()
    assert len(session.calls) == 1 and session.calls[0][2]["allow_redirects"] is False


def test_balance_units_and_account_scope_are_explicit():
    client = broker(Session(Response({"balance": 1984, "portfolio_value": 9, "updated_ts": 100})))
    result = client.balance()
    assert result["available_balance_usd"] == "19.84"
    assert result["portfolio_value_usd"] == "0.09"
    assert result["account_scope"].startswith("production:0:")
    assert client.account_scope != broker(api_key_id="other-account-key").account_scope
    assert client.account_scope != broker(subaccount=1).account_scope
    assert client.account_scope != broker(base_url=DEMO_BASE).account_scope


@pytest.mark.parametrize("data", [{"balance_dollars": "NaN", "portfolio_value": 0, "updated_ts": 1},
    {"balance": True, "portfolio_value": 0, "updated_ts": 1},
    {"balance": 100, "portfolio_value": "0", "updated_ts": 1},
    {"balance": 100, "portfolio_value": 0, "updated_ts": True}])
def test_balance_refuses_missing_or_ambiguous_units(data):
    with pytest.raises(BrokerError):
        broker(Session(Response(data))).balance()


def test_positions_fills_settlements_and_order_reads_preserve_account_scope():
    session = Session(Response({"market_positions": [{"ticker": "X", "position_fp": "1.00"}]}),
        Response({"fills": [{"ticker": "X", "order_id": "order-1", "fee_cost": ".007"}]}),
        Response({"settlements": [{"ticker": "X", "revenue": 100, "fee_cost": ".007"}]}),
        Response({"order": {"order_id": "order-1", "subaccount_number": 2}}))
    client = broker(session, subaccount=2)
    assert client.positions("X")[0]["position_fp"] == "1.00"
    assert client.fills("order-1", "X")[0]["fee_cost"] == ".007"
    assert client.settlements("X")[0]["revenue"] == 100
    assert client.get_order("order-1")["subaccount_number"] == 2
    assert all(call[2]["params"]["subaccount"] == 2 for call in session.calls[:3])


def test_scoped_response_identity_mismatch_fails_closed():
    cases = [
        (Response({"orders": [{"ticker": "OTHER"}]}), lambda c: c.orders("X")),
        (Response({"order": {"order_id": "other"}}), lambda c: c.get_order("order-1")),
        (Response({"fills": [{"ticker": "X", "order_id": "other"}]}), lambda c: c.fills("order-1", "X")),
        (Response({"positions": []}), lambda c: c.positions("X")),
        (Response({"settlements": [{"ticker": "OTHER"}]}), lambda c: c.settlements("X")),
        (Response({"orders": [{"ticker": "X", "subaccount_number": 1}]}), lambda c: c.orders("X")),
    ]
    for response, call in cases:
        with pytest.raises(BrokerError):
            call(broker(Session(response)))


@pytest.mark.parametrize("url", ["http://external-api.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com.attacker.example/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2?key=secret",
    "https://user@external-api.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com:443/trade-api/v2"])
def test_unapproved_origin_is_rejected(url):
    with pytest.raises(BrokerError):
        broker(base_url=url)


def test_missing_credentials_fail_without_environment_values_in_errors():
    with pytest.raises(CredentialsError):
        LiveBroker(session=Session())
    with pytest.raises(CredentialsError) as caught:
        LiveBroker(session=Session(), api_key_id="test", private_key="secret PEM text")
    assert "secret PEM" not in str(caught.value)


def test_read_allowlist_has_no_general_write_or_account_configuration_path():
    client = broker()
    for path in ("/portfolio/events/orders", "/portfolio/orders/../balance", "/account",
                 "/portfolio/transfers", "/portfolio/orders/id?secret=true"):
        with pytest.raises(BrokerError):
            client._read(path)


@pytest.mark.parametrize("budget", ["5.00", "10.00"])
def test_broker_accepts_only_configured_live_stake_tiers_when_explicit(budget):
    session = Session(ack())
    result = broker(session, allow_trading=True).submit_limit_buy(**request(budget=budget))
    assert result["order_id"] == "order-1"


@pytest.mark.parametrize("budget", ["1.00", "7.50", "10.01"])
def test_broker_rejects_non_tier_live_budget_when_explicit(budget):
    session = Session()
    with pytest.raises(BrokerError):
        broker(session, allow_trading=True).submit_limit_buy(**request(budget=budget))
    assert not session.calls
