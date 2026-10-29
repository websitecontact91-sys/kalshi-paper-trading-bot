"""Separate, durable live-execution journal; never a simulated cash ledger.

Account cash is an observed API value. Realized bot P/L is reconstructed only
from this journal's attributed fills and official account settlements.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import uuid
from decimal import Decimal

from payout_quote import PayoutQuote, as_decimal, ask_in_band, payout_eligible
from timing import MAX_FUTURE_S, in_entry_window, observation_fresh, round_key, timestamp
from trade_config import TRADE_BUDGET

SCHEMA = "kalshi-live.v1"
MODES = {"SCANNING", "RECONCILING", "WAITING_FOR_SETTLEMENT", "HALTED", "STOPPED"}


class LiveStateError(ValueError):
    pass


def money(value, name="money", *, nonnegative=False):
    try:
        result = as_decimal(value)
    except (TypeError, ValueError) as exc:
        raise LiveStateError("Invalid " + name) from exc
    if nonnegative and result < 0:
        raise LiveStateError("Negative " + name)
    return result


def _finite_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise LiveStateError("Nonfinite JSON value")
    if isinstance(value, Decimal):
        raise LiveStateError("Persist money as decimal strings")
    if isinstance(value, dict):
        for child in value.values():
            _finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _finite_json(child)


def new_state(session_id=None, *, account_scope):
    state = {"schema": SCHEMA, "session_id": session_id or str(uuid.uuid4()),
             "account_scope": account_scope, "mode": "SCANNING", "account_cash": None,
             "account_cash_checked_at": None, "realized_pnl": "0.00", "history": [],
             "attempts": [], "attempted_rounds": [], "pending": None, "halt_reason": None}
    return validate(state)


def _identity(record):
    if not isinstance(record, dict):
        raise LiveStateError("Journal record must be an object")
    for field in ("ticker", "round_key", "client_order_id"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise LiveStateError("Missing journal identity: " + field)
    try:
        uuid.UUID(record["client_order_id"])
    except ValueError as exc:
        raise LiveStateError("Client order identity must be a UUID") from exc
    key = record["round_key"]
    if not key.isdigit() or str(int(key)) != key or int(key) <= 0:
        raise LiveStateError("Invalid round identity")
    if record.get("side") not in {"YES", "NO"}:
        raise LiveStateError("Invalid outcome side")
    if round_key(record.get("close_time")) != key:
        raise LiveStateError("Close time and round identity differ")


def _execution(execution):
    if not isinstance(execution, dict):
        raise LiveStateError("Missing attributed execution")
    values = {}
    for key in ("quantity", "principal", "fees", "debit", "payout"):
        if not isinstance(execution.get(key), str):
            raise LiveStateError("Execution money must be decimal strings")
        values[key] = money(execution[key], key, nonnegative=True)
    if values["quantity"] <= 0 or values["quantity"] % Decimal("0.01"):
        raise LiveStateError("Invalid executed quantity")
    if values["debit"] != values["principal"] + values["fees"]:
        raise LiveStateError("Actual debit does not match principal and fees")
    if values["payout"] != values["quantity"]:
        raise LiveStateError("Only $1 binary contracts are supported")
    identities = execution.get("fill_ids")
    if (not isinstance(identities, list) or not identities or
            any(not isinstance(x, str) or not x for x in identities) or len(identities) != len(set(identities))):
        raise LiveStateError("Missing or duplicate execution identities")
    fills = execution.get("fills")
    if not isinstance(fills, list) or len(fills) != len(identities):
        raise LiveStateError("Missing execution fill details")
    seen, times = [], []
    quantity = principal = fees = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict) or not isinstance(fill.get("fill_id"), str):
            raise LiveStateError("Invalid execution fill detail")
        seen.append(fill["fill_id"])
        for field in ("quantity", "price", "fees"):
            if not isinstance(fill.get(field), str):
                raise LiveStateError("Fill amounts must be decimal strings")
        count = money(fill["quantity"], "fill quantity", nonnegative=True)
        price = money(fill["price"], "fill price", nonnegative=True)
        fee = money(fill["fees"], "fill fees", nonnegative=True)
        if count <= 0 or count % Decimal("0.01") or not 0 < price < 1:
            raise LiveStateError("Invalid fill quantity or price")
        if not isinstance(fill.get("is_taker"), bool):
            raise LiveStateError("Missing fill liquidity role")
        try:
            times.append(timestamp(fill["filled_at"]))
            timestamp(fill["observed_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveStateError("Invalid fill timestamps") from exc
        quantity += count
        principal += count * price
        fees += fee
    if len(seen) != len(set(seen)) or set(seen) != set(identities):
        raise LiveStateError("Execution identities disagree with fill details")
    if (quantity != values["quantity"] or principal != values["principal"] or fees != values["fees"]
            or money(execution.get("weighted_price"), "weighted price") != principal / quantity):
        raise LiveStateError("Fill details do not reconcile with execution aggregates")
    if (timestamp(execution.get("first_filled_at")) != min(times)
            or timestamp(execution.get("last_filled_at")) != max(times)):
        raise LiveStateError("Execution timestamp bounds disagree with fills")
    quote = PayoutQuote(max(TRADE_BUDGET, values["debit"]), values["debit"], values["payout"])
    if money(execution.get("displayed_multiplier"), "displayed multiplier") != quote.displayed_multiplier:
        raise LiveStateError("Execution payout display does not reconcile")
    return values


def execution_breaches(record):
    """Derive lasting execution breaches from immutable journal evidence."""
    flags = record.get("risk_flags", [])
    if not isinstance(flags, list) or any(not isinstance(x, str) or not x for x in flags):
        raise LiveStateError("Invalid execution risk flags")
    breaches = set(flags)
    execution = record.get("execution")
    if execution is None:
        return breaches
    actual = _execution(execution)
    if actual["quantity"] != money(record.get("quantity"), "requested quantity"):
        breaches.add("partial_fill_of_fill_or_kill_order")
    if actual["debit"] > min(TRADE_BUDGET, money(record.get("budget"), "budget")):
        breaches.add("actual_debit_exceeds_budget")
    quote = PayoutQuote(max(TRADE_BUDGET, actual["debit"]), actual["debit"], actual["payout"])
    if not payout_eligible(quote):
        breaches.add("actual_payout_multiple_out_of_band")
    earliest = max(timestamp(record.get("prepared_at")),
                   timestamp(record.get("submitted_at", record.get("prepared_at"))))
    for fill in execution["fills"]:
        price, at = money(fill["price"]), timestamp(fill["filled_at"])
        if price > money(record.get("ask")):
            breaches.add("fill_exceeds_limit")
        if not ask_in_band(price):
            breaches.add("fill_price_out_of_band")
        if not in_entry_window(record["close_time"], at):
            breaches.add("fill_outside_final_minute")
        if at < earliest:
            breaches.add("fill_precedes_order_intent_or_submission")
        if not observation_fresh(record.get("observation_time"), at):
            breaches.add("fill_reference_stale")
        if at > timestamp(fill["observed_at"]) + MAX_FUTURE_S:
            breaches.add("fill_time_in_future")
        if fill["is_taker"] is not True:
            breaches.add("unexpected_maker_fill")
    return breaches


def validate(state):
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise LiveStateError("Unsupported live journal; paper ledgers are never imported")
    _finite_json(state)
    for key in ("session_id", "account_scope"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise LiveStateError("Missing live " + key)
    mode = state.get("mode")
    if mode not in MODES:
        raise LiveStateError("Invalid live mode")
    if state.get("account_cash") is not None:
        if not isinstance(state["account_cash"], str):
            raise LiveStateError("Account cash must be a decimal string")
        money(state["account_cash"], "account cash", nonnegative=True)
    if not isinstance(state.get("realized_pnl"), str):
        raise LiveStateError("Realized P/L must be a decimal string")
    recorded_pnl = money(state["realized_pnl"], "realized P/L")
    history, attempts, rounds = (state.get(k) for k in ("history", "attempts", "attempted_rounds"))
    if not all(isinstance(x, list) for x in (history, attempts, rounds)):
        raise LiveStateError("Invalid journal lists")
    if len(rounds) != len(set(rounds)) or any(not isinstance(x, str) for x in rounds):
        raise LiveStateError("Duplicate or invalid attempted rounds")
    pending = state.get("pending")
    if mode in {"SCANNING", "STOPPED"} and pending is not None:
        raise LiveStateError("Terminal/scanning mode cannot hide a pending order")
    if mode in {"RECONCILING", "WAITING_FOR_SETTLEMENT"} and pending is None:
        raise LiveStateError("Pending mode needs an order intent")
    if mode == "HALTED" and not state.get("halt_reason"):
        raise LiveStateError("Halted journal requires its reason")
    if state.get("halt_reason") and mode not in {"HALTED", "STOPPED"}:
        raise LiveStateError("A recorded execution halt cannot be cleared by changing mode")
    records = history + attempts + ([pending] if pending else [])
    seen_rounds, seen_clients = [], []
    for item in records:
        _identity(item)
        if execution_breaches(item) and mode not in {"HALTED", "STOPPED"}:
            raise LiveStateError("An execution breach must remain halted or stopped")
        seen_rounds.append(item["round_key"])
        seen_clients.append(item["client_order_id"])
    if len(seen_rounds) != len(set(seen_rounds)) or set(seen_rounds) != set(rounds):
        raise LiveStateError("Round journal is missing or duplicated")
    if len(seen_clients) != len(set(seen_clients)):
        raise LiveStateError("Duplicate client order identity")
    for attempt in attempts:
        if attempt.get("outcome") not in {"UNFILLED", "NOT_SENT", "REJECTED"}:
            raise LiveStateError("Invalid unfilled attempt outcome")
        if attempt.get("execution"):
            raise LiveStateError("An unfilled attempt cannot contain a fill")
    total, loss_seen = Decimal("0"), False
    for trade in history:
        if loss_seen:
            raise LiveStateError("Trade history continues after a permanent loss stop")
        actual = _execution(trade.get("execution"))
        result = trade.get("result")
        if result not in {"YES", "NO", "VOID"}:
            raise LiveStateError("Unknown official result")
        won = None if result == "VOID" else result == trade["side"]
        if trade.get("won") is not won:
            raise LiveStateError("Result does not match won flag")
        revenue = money(trade.get("cash_payout"), "settlement payout", nonnegative=True)
        expected = actual["debit"] if won is None else actual["payout"] if won else Decimal("0")
        if revenue != expected or money(trade.get("pnl"), "trade P/L") != revenue - actual["debit"]:
            raise LiveStateError("Settled cash accounting does not reconcile")
        if timestamp(trade.get("settled_at")) < timestamp(trade["execution"]["last_filled_at"]):
            raise LiveStateError("Settlement precedes the last attributed fill")
        total += revenue - actual["debit"]
        loss_seen = won is False or revenue < actual["debit"]
    if total != recorded_pnl:
        raise LiveStateError("Live realized P/L does not reconcile")
    if loss_seen and mode != "STOPPED":
        raise LiveStateError("A settled loss must remain stopped")
    if pending:
        for field in ("budget", "quoted_debit", "quantity", "ask", "fee_multiplier", "balance_precision"):
            if not isinstance(pending.get(field), str):
                raise LiveStateError("Intent amounts must be decimal strings")
        budget, debit = money(pending["budget"]), money(pending["quoted_debit"])
        if not 0 < debit <= budget <= TRADE_BUDGET:
            raise LiveStateError("Intent exceeds the configured total budget")
        if pending.get("execution") is not None:
            _execution(pending["execution"])
        if mode == "WAITING_FOR_SETTLEMENT" and pending.get("execution") is None:
            raise LiveStateError("Settlement waiting requires verified fills")
    return state


def load(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        raise LiveStateError("Missing live journal; explicit initialization is required") from None
    except (ValueError, UnicodeError) as exc:
        raise LiveStateError("Unreadable live journal; refusing a reset") from exc
    return validate(state)


def save(path, state):
    """Atomic fsynced local journal; callers add remote durability when needed."""
    validate(state)
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"durable": True, "remote": False, "state_path": str(path)}


def clone(state):
    return copy.deepcopy(validate(state))
