"""Separate account transport; production writes are disabled by default.

The paper runner must never import this module.  The only implemented write is
one V2 fill-or-kill order, and it is never retried.  A timeout, duplicate-ID
response, unreadable acknowledgement or server failure is an UNKNOWN outcome:
the caller must reconcile the durable client ID before doing anything else.

Current API: https://docs.kalshi.com/api-reference/orders/create-order-v2
Direction: https://docs.kalshi.com/getting_started/order_direction
Rounding: https://docs.kalshi.com/getting_started/fee_rounding

V2 does not document a server-side total-cost cap.  The cost guard below is a
conservative bound under the supplied, currently observed quadratic fee terms;
it is not a promise about a fee-policy change during execution.  Actual order
and fill accounting remains authoritative and any discrepancy must halt entry.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from live_stakes import MAX_LIVE_BUDGET, NEW_LIVE_BUDGETS
from payout_quote import PayoutQuote, as_decimal, ask_in_band, payout_eligible
from quote_adapter import documented_taker_cost
from trade_config import TRADE_BUDGET

PRODUCTION_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
CREATE_PATH = "/portfolio/events/orders"
QUANTITY_STEP = Decimal("0.01")
PRICE_STEP = Decimal("0.0001")
FEE_STEP = Decimal("0.000001")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_READ_PATHS = frozenset({"/portfolio/balance", "/portfolio/orders", "/portfolio/fills",
                         "/portfolio/settlements", "/portfolio/positions", "/exchange/status"})


class BrokerError(RuntimeError):
    """Sanitized, fail-closed account read or local validation failure."""


class CredentialsError(BrokerError):
    pass


class TradingDisabled(BrokerError):
    pass


class OrderRejected(BrokerError):
    """An explicit HTTP client error; no automatic retry is permitted."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Kalshi rejected the order request (HTTP {status_code})")


class AmbiguousOrderError(BrokerError):
    """The request may have reached the exchange. Reconcile; never resubmit."""


def _decimal(value, name):
    try:
        return as_decimal(value)
    except (TypeError, ValueError) as exc:
        raise BrokerError(f"Invalid {name}") from None


def _identifier(value, name, pattern=_IDENTIFIER):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BrokerError(f"Invalid {name}")
    return value


def _cost_inputs(price, quantity, fee_multiplier, balance_precision):
    price = _decimal(price, "price")
    quantity = _decimal(quantity, "quantity")
    multiplier = _decimal(fee_multiplier, "fee multiplier")
    precision = _decimal(balance_precision, "balance precision")
    if not 0 < price < 1 or price % PRICE_STEP:
        raise BrokerError("Price must use a positive fixed-point dollar increment below $1")
    if quantity < 0 or quantity % QUANTITY_STEP:
        raise BrokerError("Quantity must use nonnegative 0.01-contract increments")
    if multiplier < 0:
        raise BrokerError("Fee multiplier cannot be negative")
    if precision not in (Decimal("0.01"), Decimal("0.0001")):
        raise BrokerError("Unsupported account balance precision")
    return price, quantity, multiplier, precision


def worst_case_debit(price, quantity, fee_multiplier, balance_precision="0.01"):
    """Upper-bound fee-inclusive cost even if every 0.01 contract fills alone.

    A buy can execute at any improved leg price <= its limit.  Maximize the
    concave principal-plus-quadratic-fee curve over that interval, then apply
    upward fee/balance rounding to each minimum contract increment separately.
    This intentionally ignores possible rounding rebates.  The default cent
    precision is conservative for either documented account type.
    """
    price, quantity, multiplier, precision = _cost_inputs(
        price, quantity, fee_multiplier, balance_precision)
    if quantity == 0:
        return Decimal("0")
    coefficient = Decimal("0.07") * multiplier
    peak_price = min(price, (1 + coefficient) / (2 * coefficient)) if coefficient else price
    unit_total = QUANTITY_STEP * (peak_price + coefficient * peak_price * (1 - peak_price))
    # A price has four decimal places and a minimum quantity has two, so the
    # principal is already on the 6dp grid. Rounding their sum up bounds the
    # separately rounded model fee without introducing binary floats.
    unit_total = unit_total.quantize(FEE_STEP, rounding=ROUND_CEILING)
    unit_debit = unit_total.quantize(precision, rounding=ROUND_CEILING)
    return (quantity / QUANTITY_STEP) * unit_debit


def max_affordable_quantity(price, available_quantity, fee_multiplier,
                            balance_precision="0.01", budget=TRADE_BUDGET):
    amount = _decimal(budget, "budget")
    depth = _decimal(available_quantity, "available quantity")
    if not 0 < amount <= MAX_LIVE_BUDGET or depth < 0:
        raise BrokerError("Live budget must be positive and at most the configured $10 cap")
    unit_cost = worst_case_debit(price, QUANTITY_STEP, fee_multiplier, balance_precision)
    units = min((depth / QUANTITY_STEP).to_integral_value(rounding=ROUND_FLOOR),
                (amount / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
    return units * QUANTITY_STEP


class LiveBroker:
    def __init__(self, base_url=PRODUCTION_BASE, *, session=None, api_key_id=None,
                 private_key=None, subaccount=0, allow_trading=False,
                 clock=time.time, sleep=time.sleep, timeout=5, max_pages=100):
        parsed = urlparse(base_url)
        if (base_url not in (PRODUCTION_BASE, DEMO_BASE) or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise BrokerError("Only documented Kalshi production or demo API origins are allowed")
        if isinstance(subaccount, bool) or not isinstance(subaccount, int) or not 0 <= subaccount <= 63:
            raise BrokerError("Invalid subaccount")
        if not isinstance(allow_trading, bool):
            raise BrokerError("Trading activation must be an explicit boolean")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 15):
            raise BrokerError("Invalid request timeout")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
            raise BrokerError("Invalid account pagination bound")
        key_id = os.environ.get("KALSHI_API_KEY_ID", "") if api_key_id is None else api_key_id
        if not isinstance(key_id, str) or not key_id or any(c.isspace() for c in key_id):
            raise CredentialsError("Kalshi API key ID is missing or malformed")
        raw = os.environ.get("KALSHI_PRIVATE_KEY", "") if private_key is None else private_key
        try:
            if isinstance(raw, str):
                raw = serialization.load_pem_private_key(raw.encode(), password=None)
            elif isinstance(raw, bytes):
                raw = serialization.load_pem_private_key(raw, password=None)
            if not isinstance(raw, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
                raise ValueError("unsupported key")
        except Exception:
            raise CredentialsError("Kalshi signing key is missing, malformed, or unsupported") from None
        self.base_url, self.subaccount = base_url, subaccount
        self.environment = "production" if base_url == PRODUCTION_BASE else "demo"
        self.account_scope = (f"{self.environment}:{subaccount}:"
                              + hashlib.sha256(key_id.encode()).hexdigest()[:16])
        self._key_id, self._key = key_id, raw
        self._writes_enabled = allow_trading
        self._session = session if session is not None else requests.Session()
        self.clock, self.sleep, self.timeout, self.max_pages = clock, sleep, timeout, max_pages

    @property
    def writes_enabled(self):
        return self._writes_enabled

    def close(self):
        close = getattr(self._session, "close", None)
        if close:
            close()

    def _headers(self, method, path):
        timestamp = str(int(self.clock() * 1000))
        message = (timestamp + method + "/trade-api/v2" + path).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        return {"Accept": "application/json", "KALSHI-ACCESS-KEY": self._key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}

    @staticmethod
    def _decode(response):
        try:
            data = json.loads(response.text, parse_float=str,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid constant")))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (ValueError, TypeError):
            raise BrokerError("Kalshi returned an invalid account response") from None

    def _read(self, path, params=None):
        if path not in _READ_PATHS:
            prefix = "/portfolio/orders/"
            if not path.startswith(prefix):
                raise BrokerError("Account read endpoint is outside the allowlist")
            _identifier(path[len(prefix):], "order ID")
        for attempt in range(2):
            try:
                response = self._session.get(self.base_url + path, params=params,
                    headers=self._headers("GET", path), timeout=self.timeout, allow_redirects=False)
            except requests.RequestException:
                raise BrokerError("Kalshi account read failed at the network boundary") from None
            status = response.status_code
            if (status == 429 or status >= 500) and attempt == 0:
                try:
                    delay = min(2.0, max(0.25, float(response.headers.get("Retry-After", "1"))))
                    if not math.isfinite(delay):
                        delay = 1.0
                except (ValueError, TypeError):
                    delay = 1.0
                self.sleep(delay)
                continue
            if 300 <= status < 400:
                raise BrokerError("Kalshi account redirect refused")
            if status != 200:
                raise BrokerError(f"Kalshi account read failed (HTTP {status})")
            return self._decode(response)
        raise BrokerError("Kalshi account read retry limit reached")

    def _rows(self, path, key, params=None):
        query = {"subaccount": self.subaccount, "limit": 1000, **(params or {})}
        result, seen_cursors = [], set()
        for _ in range(self.max_pages):
            page = self._read(path, query)
            rows = page.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise BrokerError("Invalid paginated account response")
            for row in rows:
                if "subaccount_number" in row and row["subaccount_number"] != self.subaccount:
                    raise BrokerError("Account response contains a different subaccount")
            result.extend(rows)
            cursor = page.get("cursor", "")
            if not isinstance(cursor, str):
                raise BrokerError("Invalid account pagination cursor")
            if not cursor:
                return result
            if cursor in seen_cursors:
                raise BrokerError("Repeated account pagination cursor")
            seen_cursors.add(cursor)
            query = {**query, "cursor": cursor}
        raise BrokerError("Account pagination limit reached; completeness is unknown")

    def balance(self):
        data = self._read("/portfolio/balance", {"subaccount": self.subaccount})
        if isinstance(data.get("balance_dollars"), str):
            available = _decimal(data["balance_dollars"], "available balance")
        elif isinstance(data.get("balance"), int) and not isinstance(data["balance"], bool):
            available = Decimal(data["balance"]) / 100
        else:
            raise BrokerError("Available account balance is missing")
        cents = data.get("portfolio_value")
        updated = data.get("updated_ts")
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise BrokerError("Invalid portfolio value")
        if isinstance(updated, bool) or not isinstance(updated, int) or updated < 0:
            raise BrokerError("Invalid account balance timestamp")
        return {"available_balance_usd": format(available, "f"),
                "portfolio_value_usd": format(Decimal(cents) / 100, "f"),
                "updated_ts": updated, "subaccount": self.subaccount,
                "account_scope": self.account_scope}

    def exchange_status(self):
        data = self._read("/exchange/status")
        if not all(isinstance(data.get(key), bool) for key in ("exchange_active", "trading_active")):
            raise BrokerError("Exchange status flags are missing")
        return data

    def orders(self, ticker=None, status=None):
        params = {}
        if ticker is not None:
            params["ticker"] = _identifier(ticker, "market ticker")
        if status is not None:
            if status not in ("resting", "canceled", "executed"):
                raise BrokerError("Unsupported order status filter")
            params["status"] = status
        rows = self._rows("/portfolio/orders", "orders", params)
        if ticker is not None and any(row.get("ticker") != ticker for row in rows):
            raise BrokerError("Order response market does not match its requested filter")
        return rows

    def get_order(self, order_id):
        order_id = _identifier(order_id, "order ID")
        row = self._read("/portfolio/orders/" + order_id).get("order")
        if not isinstance(row, dict) or row.get("order_id") != order_id:
            raise BrokerError("Order response identity mismatch")
        if row.get("subaccount_number", self.subaccount) != self.subaccount:
            raise BrokerError("Order belongs to a different subaccount")
        return row

    def find_order(self, client_order_id, ticker):
        client_order_id = _identifier(client_order_id, "client order ID", _CLIENT_ID)
        matches = [row for row in self.orders(ticker)
                   if row.get("client_order_id") == client_order_id]
        if len(matches) > 1:
            raise BrokerError("Client order identity is not unique")
        return matches[0] if matches else None

    def fills(self, order_id, ticker):
        order_id = _identifier(order_id, "order ID")
        ticker = _identifier(ticker, "market ticker")
        rows = self._rows("/portfolio/fills", "fills", {"order_id": order_id, "ticker": ticker})
        for row in rows:
            if row.get("order_id") != order_id or row.get("ticker", row.get("market_ticker")) != ticker:
                raise BrokerError("Fill response identity mismatch")
            if "ticker" in row and "market_ticker" in row and row["ticker"] != row["market_ticker"]:
                raise BrokerError("Fill contains conflicting market identities")
        return rows

    def settlements(self, ticker):
        ticker = _identifier(ticker, "market ticker")
        rows = self._rows("/portfolio/settlements", "settlements", {"ticker": ticker})
        if any(row.get("ticker") != ticker for row in rows):
            raise BrokerError("Settlement response market identity mismatch")
        return rows

    def positions(self, ticker=None):
        params = {"settlement_status": "unsettled"}
        if ticker is not None:
            params["ticker"] = _identifier(ticker, "market ticker")
        rows = self._rows("/portfolio/positions", "market_positions", params)
        if ticker is not None and any(row.get("ticker") != ticker for row in rows):
            raise BrokerError("Position response market identity mismatch")
        return rows

    def submit_limit_buy(self, *, ticker, side, quantity, price, client_order_id,
                         expected_debit, fee_multiplier, balance_precision="0.01",
                         budget=None):
        if not self.writes_enabled:
            raise TradingDisabled("Live order submission is disabled")
        ticker = _identifier(ticker, "market ticker")
        client_order_id = _identifier(client_order_id, "client order ID", _CLIENT_ID)
        if side not in ("YES", "NO"):
            raise BrokerError("Only a YES or NO outcome purchase is supported")
        price, quantity, multiplier, precision = _cost_inputs(
            price, quantity, fee_multiplier, balance_precision)
        debit = _decimal(expected_debit, "expected debit")
        amount = _decimal(TRADE_BUDGET if budget is None else budget, "budget")
        if budget is not None and amount not in NEW_LIVE_BUDGETS:
            raise BrokerError("Unsupported live stake budget")
        if not 0 < amount <= MAX_LIVE_BUDGET:
            raise BrokerError("Live stake budget exceeds the configured maximum")
        if quantity <= 0 or not ask_in_band(price):
            raise BrokerError("Order quantity or entry price is outside the strategy limits")
        modeled = documented_taker_cost(price, quantity, multiplier, precision)[2]
        if debit != modeled or not 0 < debit <= amount:
            raise BrokerError("Expected debit is inconsistent or above the selected live budget")
        if not payout_eligible(PayoutQuote(amount, debit, quantity)):
            raise BrokerError("Fee-inclusive payout is outside the strategy limits")
        if worst_case_debit(price, quantity, multiplier, precision) > amount:
            raise BrokerError("Worst-case fill and fee rounding exceeds the selected live budget")
        # V2 has a single YES-price book. A NO purchase is an ask at 1-NO-price.
        yes_price = price if side == "YES" else 1 - price
        body = {"ticker": ticker, "client_order_id": client_order_id,
                "side": "bid" if side == "YES" else "ask",
                "count": format(quantity, ".2f"), "price": format(yes_price, ".4f"),
                "time_in_force": "fill_or_kill", "self_trade_prevention_type": "taker_at_cross",
                "post_only": False, "reduce_only": False, "cancel_order_on_pause": True,
                "subaccount": self.subaccount}
        headers = self._headers("POST", CREATE_PATH)
        headers["Content-Type"] = "application/json"
        try:
            response = self._session.post(self.base_url + CREATE_PATH, json=body,
                headers=headers, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise AmbiguousOrderError("Order acknowledgement unavailable; reconcile the saved client ID") from None
        status = response.status_code
        if 400 <= status < 500 and status not in (408, 409):
            raise OrderRejected(status)
        if status != 201:
            raise AmbiguousOrderError(f"Order outcome is unknown (HTTP {status}); reconcile the saved client ID")
        try:
            data = self._decode(response)
            _identifier(data.get("order_id"), "acknowledged order ID")
            if data.get("client_order_id", client_order_id) != client_order_id:
                raise BrokerError("client ID mismatch")
            for name in ("fill_count", "remaining_count"):
                if not isinstance(data.get(name), str):
                    raise BrokerError("missing quantity")
                value = _decimal(data[name], name)
                if not 0 <= value <= quantity or value % QUANTITY_STEP:
                    raise BrokerError("invalid acknowledged quantity")
            if _decimal(data["fill_count"], "filled") + _decimal(data["remaining_count"], "remaining") > quantity:
                raise BrokerError("acknowledged quantity exceeds request")
            if isinstance(data.get("ts_ms"), bool) or not isinstance(data.get("ts_ms"), int) or data["ts_ms"] <= 0:
                raise BrokerError("invalid acknowledgement timestamp")
            # V2 documents the echoed client ID as optional. Preserve the
            # known request identity for the durable execution layer, while an
            # explicitly different (or null) ID above remains ambiguous.
            data.setdefault("client_order_id", client_order_id)
            return data
        except BrokerError:
            raise AmbiguousOrderError("Order acknowledgement is inconsistent; reconcile the saved client ID") from None
