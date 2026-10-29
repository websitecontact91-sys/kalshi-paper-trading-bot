"""Read-only quote normalization and explicitly modeled Kalshi taker purchases.

No undocumented retail quote endpoint is assumed. ``build_quote`` consumes an
internally normalized, audited capture and verifies values against the preserved
raw JSON. ``build_modeled_quote`` is a distinct documented-fee simulation; it never
claims retail UI parity. See tests/fixtures/QUOTE_PROVENANCE.md.
"""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlparse

from payout_quote import PayoutQuote, as_decimal

NORMALIZED_SCHEMA = "kalshi-paper.normalized-quote.v1"
QUANTITY_STEP = Decimal("0.01")
FEE_STEP = Decimal("0.000001")
FEE_SCHEDULE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
FEE_ROUNDING_URL = "https://docs.kalshi.com/getting_started/fee_rounding"
FIXED_POINT_URL = "https://docs.kalshi.com/getting_started/fixed_point_migration"


class QuoteUnavailable(ValueError):
    """Necessary pricing or provenance evidence is absent or unsupported."""


@dataclass(frozen=True)
class QuoteDetails:
    quote: PayoutQuote
    quantity: Decimal
    fees: Decimal
    principal: Decimal
    source: str
    ui_parity_verified: bool = False
    evidence: dict = field(default_factory=dict)

    def ledger_fields(self):
        return {
            "budget": str(self.quote.budget), "debit": str(self.quote.debit),
            "payout": str(self.quote.payout), "quantity": str(self.quantity),
            "fees": str(self.fees), "principal": str(self.principal),
            "unused_cash": str(self.quote.unused_cash),
            "exact_multiplier": str(self.quote.exact_multiplier),
            "displayed_multiplier": str(self.quote.displayed_multiplier),
            "quote_source": self.source, "ui_parity_verified": self.ui_parity_verified,
            "quote_evidence": dict(self.evidence),
        }


def _utc_timestamp(value):
    if not isinstance(value, str):
        raise QuoteUnavailable("timestamp must be a timezone-aware ISO string")
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuoteUnavailable("invalid timestamp") from exc
    if stamp.tzinfo is None:
        raise QuoteUnavailable("timestamp must include a timezone")
    return stamp.timestamp()


def _number(value, label):
    try:
        return as_decimal(value)
    except (TypeError, ValueError) as exc:
        raise QuoteUnavailable(f"invalid {label}: {exc}") from exc


def _path_value(payload, path):
    if not isinstance(path, list) or not path:
        raise QuoteUnavailable("an audited source field path is required")
    try:
        for part in path:
            if isinstance(part, bool) or not isinstance(part, (str, int)):
                raise QuoteUnavailable("invalid source field path")
            payload = payload[part]
        return payload
    except (KeyError, IndexError, TypeError) as exc:
        raise QuoteUnavailable("source field path does not resolve") from exc


def build_quote(raw, budget=Decimal("5.00")):
    """Verify an audited normalized capture; never guess Kalshi response fields.

    Required provenance: an HTTPS Kalshi GET capture, raw JSON string + its SHA256,
    adapter_verified=True, and field_paths mapping debit/payout/quantity/fees/
    principal to their exact raw JSON locations. ``adapter_verified`` is an audit
    attestation supplied by a separately verified capture adapter, not something
    this function infers from a URL or a screenshot. Missing evidence fails closed.
    """
    if not isinstance(raw, dict) or raw.get("schema") != NORMALIZED_SCHEMA:
        raise QuoteUnavailable("unsupported normalized quote schema")
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        raise QuoteUnavailable("quote provenance is required")
    if (provenance.get("kind") != "captured_kalshi_response"
            or provenance.get("adapter_verified") is not True
            or provenance.get("request_method") != "GET"):
        raise QuoteUnavailable("an audited read-only Kalshi capture is required")
    parsed = urlparse(str(provenance.get("source_url", "")))
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or not (host == "kalshi.com" or host.endswith(".kalshi.com"))):
        raise QuoteUnavailable("capture source must be HTTPS on a Kalshi host")
    _utc_timestamp(provenance.get("captured_at"))
    response_text = provenance.get("raw_response")
    if not isinstance(response_text, str):
        raise QuoteUnavailable("raw response text is required")
    digest = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    if provenance.get("raw_response_sha256") != digest:
        raise QuoteUnavailable("raw response digest mismatch")
    try:
        response = json.loads(response_text, parse_float=Decimal,
                              parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
    except (ValueError, TypeError) as exc:
        raise QuoteUnavailable("invalid raw response JSON") from exc
    paths = provenance.get("field_paths")
    if not isinstance(paths, dict):
        raise QuoteUnavailable("audited source field paths are required")
    values = {}
    for name in ("debit", "payout", "quantity", "fees", "principal"):
        normalized = _number(raw.get(name), name)
        observed = _number(_path_value(response, paths.get(name)), name)
        if normalized != observed:
            raise QuoteUnavailable(f"{name} differs from the captured response")
        values[name] = normalized
    amount = _number(budget, "budget")
    if _number(raw.get("budget"), "capture budget") != amount:
        raise QuoteUnavailable("capture budget does not match the requested budget")
    if values["quantity"] <= 0 or values["quantity"] % QUANTITY_STEP:
        raise QuoteUnavailable("quantity must use positive 0.01 contract increments")
    if values["fees"] < 0 or values["principal"] <= 0:
        raise QuoteUnavailable("invalid quote cost breakdown")
    if values["principal"] + values["fees"] != values["debit"]:
        raise QuoteUnavailable("principal and fees do not reconcile to debit")
    quote = PayoutQuote.from_kalshi(amount, values["debit"], values["payout"])
    ui = provenance.get("ui_validation", {})
    ui_verified = (isinstance(ui, dict) and ui.get("verified") is True
                   and ui.get("raw_response_sha256") == digest
                   and ui.get("displayed_multiplier") == str(quote.displayed_multiplier)
                   and ui.get("payout") == str(quote.payout)
                   and ui.get("debit") == str(quote.debit))
    return QuoteDetails(quote, values["quantity"], values["fees"], values["principal"],
                        "kalshi_response", ui_verified,
                        {"source_url": provenance["source_url"],
                         "captured_at": provenance["captured_at"],
                         "raw_response_sha256": digest,
                         "adapter_id": provenance.get("adapter_id", "")})


def effective_fee_terms(series, event, market, *, now=None):
    """Resolve observed series terms, event overrides, and a market fee waiver.

    Kalshi's EventData schema makes both overrides optional, nullable, and
    x-omitempty; an absent override inherits the series terms. Accept that API
    representation only when the fetched event's identity matches the market
    and series. An empty/missing event response is not evidence of inheritance.
    Source: https://docs.kalshi.com/openapi.yaml (EventData, EventFeeChange).
    """
    if not isinstance(series, dict) or not isinstance(event, dict) or not isinstance(market, dict):
        raise QuoteUnavailable("series, event, and market metadata are required")
    if "fee_type" not in series or "fee_multiplier" not in series:
        raise QuoteUnavailable("series fee metadata is missing")
    override_names = ("fee_type_override", "fee_multiplier_override")
    if any(name not in event for name in override_names):
        event_id, series_id = event.get("event_ticker"), event.get("series_ticker")
        if (not isinstance(event_id, str) or not event_id or event_id != market.get("event_ticker")
                or not isinstance(series_id, str) or not series_id or series_id != series.get("ticker")
                or market.get("series_ticker", series_id) != series_id
                or str(market.get("ticker", "")).split("-", 1)[0] != series_id):
            raise QuoteUnavailable("event fee override metadata is missing or event identity is unverified")
        # The schema describes these fields as a pair. A non-null override with
        # its companion absent is incomplete metadata, not a no-override case.
        if any(event.get(name) is not None for name in override_names):
            raise QuoteUnavailable("event fee override pair is incomplete")
    fee_type = event.get("fee_type_override")
    multiplier = event.get("fee_multiplier_override")
    fee_type = series["fee_type"] if fee_type is None else fee_type
    multiplier = series["fee_multiplier"] if multiplier is None else multiplier
    multiplier = _number(multiplier, "fee multiplier")
    if fee_type != "quadratic":
        raise QuoteUnavailable(f"unsupported fee_type: {fee_type!r}")
    if multiplier < 0:
        raise QuoteUnavailable("fee multiplier cannot be negative")
    waiver = market.get("fee_waiver_expiration_time")
    waived = False
    if waiver:
        expiry = _utc_timestamp(waiver)
        clock = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
        if isinstance(clock, dt.datetime):
            if clock.tzinfo is None:
                raise QuoteUnavailable("now must be timezone-aware")
            clock = clock.timestamp()
        if not isinstance(clock, (int, float)) or not float("-inf") < clock < float("inf"):
            raise QuoteUnavailable("invalid clock")
        waived = clock < expiry
    return fee_type, Decimal("0") if waived else multiplier, waived


def documented_taker_cost(ask, quantity, multiplier, balance_precision):
    """One equivalent taker fill under the current documented quadratic model.

    Sources: https://kalshi.com/docs/kalshi-fee-schedule.pdf (July 7, 2026)
    and https://docs.kalshi.com/getting_started/fee_rounding.
    Fee model rounds up to 6dp; the total buyer debit then aligns upward to
    the explicitly supplied account precision. Multi-fill UI parity is not claimed.
    """
    p, count, mult = (_number(ask, "ask"), _number(quantity, "quantity"),
                      _number(multiplier, "fee multiplier"))
    if balance_precision is None:
        raise QuoteUnavailable("account balance precision is unknown")
    precision = _number(balance_precision, "balance precision")
    if precision not in (Decimal("0.0001"), Decimal("0.01")):
        raise QuoteUnavailable("balance precision must be 0.0001 (direct) or 0.01 (non-direct)")
    if not 0 < p < 1 or count <= 0 or mult < 0:
        raise QuoteUnavailable("invalid fee inputs")
    principal = count * p
    model_fee = (Decimal("0.07") * mult * count * p * (Decimal("1") - p)).quantize(
        FEE_STEP, rounding=ROUND_CEILING)
    debit = (principal + model_fee).quantize(precision, rounding=ROUND_CEILING)
    return principal, debit - principal, debit


def build_modeled_quote(ask, available_quantity, series, event, market, *,
                        budget=Decimal("5.00"), balance_precision=None, now=None):
    """Size <=$5 at one observed executable price level in 0.01-contract steps.

    The caller must supply that level's actual available quantity, not cumulative
    depth at different prices. This is a documented simulation, never a verified
    retail UI quote. Missing account precision or fee metadata is a blocker.
    """
    price = _number(ask, "ask")
    depth = _number(available_quantity, "available quantity")
    amount = _number(budget, "budget")
    if not 0 < price < 1 or depth <= 0 or not 0 < amount <= Decimal("5.00"):
        raise QuoteUnavailable("invalid ask, depth, or maximum $5 budget")
    if balance_precision is None:
        raise QuoteUnavailable("account balance precision is unknown")
    if market.get("market_type") != "binary":
        raise QuoteUnavailable("only verified binary markets are modeled")
    if _number(market.get("notional_value_dollars"), "contract notional") != Decimal("1"):
        raise QuoteUnavailable("only $1 binary contract payouts are modeled")
    if market.get("settlement_bounds_type", "default") != "default":
        raise QuoteUnavailable("non-default settlement bounds require a verified quote")
    fee_type, multiplier, waived = effective_fee_terms(series, event, market, now=now)
    high = int((min(depth, amount / price) / QUANTITY_STEP).to_integral_value(rounding=ROUND_FLOOR))
    low = 0
    while low < high:
        mid = (low + high + 1) // 2
        _, _, debit = documented_taker_cost(price, QUANTITY_STEP * mid, multiplier, balance_precision)
        if debit <= amount:
            low = mid
        else:
            high = mid - 1
    if low == 0:
        raise QuoteUnavailable("no affordable contract increment at the observed depth")
    quantity = QUANTITY_STEP * low
    principal, fees, debit = documented_taker_cost(price, quantity, multiplier, balance_precision)
    quote = PayoutQuote.from_kalshi(amount, debit, quantity)
    return QuoteDetails(quote, quantity, fees, principal, "documented_fee_model", False,
                        {"fee_type": fee_type, "fee_multiplier": str(multiplier),
                         "fee_waived": waived, "balance_precision": str(balance_precision),
                         "quantity_increment": str(QUANTITY_STEP),
                         "fill_assumption": "single_equivalent_taker_fill_at_one_price_level",
                         "fee_schedule_url": FEE_SCHEDULE_URL,
                         "fee_rounding_url": FEE_ROUNDING_URL,
                         "quantity_source_url": FIXED_POINT_URL})
