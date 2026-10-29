"""Validated paper ledger and atomic local state; never resets a session on load.

The JSON history is authoritative. A CSV ledger is a derived reporting view.
Callers form a next-state copy, persist it successfully, then announce an entry.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_CEILING

SCHEMA = 1
MAX_BUDGET = Decimal("5.00")
MODES = {"SCANNING", "WAITING_FOR_SETTLEMENT", "STOPPED"}
MONEY_FIELDS = {
    "budget", "debit", "payout", "fees", "unused_cash", "pnl", "cash_payout",
    "balance_after", "cost", "fee_reserve", "quantity", "count", "ask",
    "exact_multiplier", "displayed_multiplier", "gross_payout", "fee_multiplier",
}


class StateError(ValueError):
    """Invalid state or an attempted transition forbidden by the strategy."""


def _decimal(value, name, *, legacy=False):
    if isinstance(value, bool) or (isinstance(value, float) and not legacy):
        raise StateError(f"{name} must be a decimal string or Decimal")
    try:
        number = Decimal(str(value)) if legacy else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StateError(f"Invalid {name}") from exc
    if not number.is_finite():
        raise StateError(f"Nonfinite {name}")
    return number


def _s(value):
    return format(value, "f")


def _finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise StateError("Nonfinite value in saved state")
    if isinstance(value, Decimal) and not value.is_finite():
        raise StateError("Nonfinite value in saved state")
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _finite_tree(child)


def _json_ready(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise StateError("Nonfinite value in saved state")
        return _s(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def canonical_round_key(value):
    """UTC quarter-hour closing bucket, including the legacy bucket format."""
    if isinstance(value, bool) or value is None:
        raise StateError("Invalid round key")
    if isinstance(value, str) and ("T" in value or ":" in value):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Round timestamps require a timezone")
            number = Decimal(str(parsed.timestamp()))
        except ValueError as exc:
            raise StateError("Invalid round timestamp") from exc
    else:
        number = _decimal(value, "round key", legacy=True)
    if number <= 0:
        raise StateError("Round key must be positive")
    # Contemporary epoch timestamps are much larger than quarter-hour indices.
    if number >= Decimal("100000000"):
        number = (number / Decimal(900)).to_integral_value(rounding=ROUND_CEILING)
    if number != number.to_integral_value():
        raise StateError("Round bucket must be an integer")
    return str(int(number))


def new_state(initial_balance="1000.00", *, session_id=None):
    balance = _decimal(initial_balance, "initial balance")
    if balance < 0:
        raise StateError("Negative initial balance")
    return {"schema": SCHEMA, "session_id": session_id or uuid.uuid4().hex,
            "mode": "SCANNING", "initial_balance": _s(balance),
            "balance": _s(balance), "pending": None, "rounds_traded": [],
            "history": []}


def _trade(raw, *, legacy=False, round_key=None):
    if not isinstance(raw, dict):
        raise StateError("Trade must be an object")
    trade = copy.deepcopy(raw)
    for name in MONEY_FIELDS & trade.keys():
        if trade[name] is not None:
            trade[name] = _s(_decimal(trade[name], name, legacy=legacy))
    if legacy:
        trade.setdefault("debit", trade.get("cost"))
        trade.setdefault("budget", "5.00")
        trade.setdefault("fees", trade.get("fee_reserve", "0"))
        trade.setdefault("quantity", trade.get("count"))
        # A stored quantity is the historical potential binary payout. Do not
        # rebuild it by multiplying a rounded display multiplier by a budget.
        trade.setdefault("payout", trade.get("count"))
        trade.setdefault("quote_source", trade.get("payout_source", "legacy"))
        trade.setdefault("ui_parity_verified", False)
    ticker = trade.get("ticker")
    side = str(trade.get("side", "")).upper()
    if not isinstance(ticker, str) or not ticker.strip() or side not in {"YES", "NO"}:
        raise StateError("Trade requires ticker and YES/NO side")
    trade["side"] = side
    budget = _decimal(trade.get("budget", "5.00"), "budget")
    debit = _decimal(trade.get("debit"), "debit")
    payout = _decimal(trade.get("payout"), "payout")
    fees = _decimal(trade.get("fees", "0"), "fees")
    if not (0 < debit <= budget <= MAX_BUDGET) or payout <= 0 or not 0 <= fees <= debit:
        raise StateError("Invalid budget, debit, payout or fees")
    trade.update(budget=_s(budget), debit=_s(debit), payout=_s(payout), fees=_s(fees))
    unused = budget - debit
    if "unused_cash" in trade and _decimal(trade["unused_cash"], "unused cash") != unused:
        raise StateError("Unused cash does not reconcile")
    trade["unused_cash"] = _s(unused)
    if trade.get("quantity") is not None:
        quantity = _decimal(trade["quantity"], "quantity")
        if quantity <= 0:
            raise StateError("Quantity must be positive")
        trade["quantity"] = _s(quantity)
    key = canonical_round_key(round_key if round_key is not None else
                              trade.get("round_key", trade.get("round_id", trade.get("close_time"))))
    for name in ("round_key", "round_id", "close_time"):
        if trade.get(name) is not None and canonical_round_key(trade[name]) != key:
            raise StateError("Inconsistent trade round")
    trade["round_key"] = key
    trade["round_id"] = key
    _finite_tree(trade)
    return _json_ready(trade)


def migrate(raw):
    """Convert legacy snapshots in memory without changing any archived file."""
    if not isinstance(raw, dict):
        raise StateError("State must be an object")
    _finite_tree(raw)
    if raw.get("schema") == SCHEMA:
        result = copy.deepcopy(raw)
        validate(result)
        return result
    if "schema" in raw or raw.get("version", 1) != 1:
        raise StateError("Unsupported state schema")
    if not isinstance(raw.get("history"), list):
        raise StateError("Missing legacy history")
    history = [_trade(t, legacy=True) for t in raw["history"]]
    pending = _trade(raw["trade"], legacy=True) if raw.get("trade") is not None else None
    balance = _decimal(raw.get("balance"), "balance", legacy=True)
    pnl_sum = Decimal(0)
    for trade in history:
        result = str(trade.get("result", "")).upper()
        if result not in {"YES", "NO", "VOID"}:
            raise StateError("Unknown settled legacy result")
        won = None if result == "VOID" else trade["side"] == result
        if trade.get("won") is not won:
            raise StateError("Legacy result and won flag disagree")
        debit = _decimal(trade["debit"], "debit")
        credit = debit if result == "VOID" else (_decimal(trade["payout"], "payout") if won else Decimal(0))
        pnl = credit - debit
        if "pnl" in trade and _decimal(trade["pnl"], "pnl") != pnl:
            raise StateError("Legacy settled P&L does not reconcile")
        trade.update(result=result.lower() if result != "VOID" else "VOID",
                     pnl=_s(pnl), cash_payout=_s(credit))
        pnl_sum += pnl
    initial = balance - pnl_sum + (_decimal(pending["debit"], "debit") if pending else Decimal(0))
    stable_id = hashlib.sha256(json.dumps(raw, sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
    state = {"schema": SCHEMA, "session_id": "legacy-" + stable_id,
             "migration": {"from_version": raw.get("version", 1)},
             "mode": raw.get("mode"), "initial_balance": _s(initial),
             "balance": _s(balance), "history": history, "pending": pending,
             "rounds_traded": [t["round_key"] for t in history] + ([pending["round_key"]] if pending else [])}
    for trade in history + ([pending] if pending else []):
        trade.setdefault("event_id", f"{state['session_id']}:{trade['round_key']}")
    validate(state)
    return state


def validate(state):
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise StateError("Unsupported state schema")
    _finite_tree(state)
    if state.get("mode") not in MODES or not isinstance(state.get("session_id"), str) or not state["session_id"]:
        raise StateError("Invalid state mode or session identity")
    initial = _decimal(state.get("initial_balance"), "initial balance")
    balance = _decimal(state.get("balance"), "balance")
    if initial < 0 or balance < 0 or not isinstance(state["balance"], str) or not isinstance(state["initial_balance"], str):
        raise StateError("Balances must be nonnegative decimal strings")
    history, rounds = state.get("history"), state.get("rounds_traded")
    if not isinstance(history, list) or not isinstance(rounds, list):
        raise StateError("Invalid history or rounds")
    canonical = [canonical_round_key(key) for key in rounds]
    if canonical != rounds or len(set(rounds)) != len(rounds):
        raise StateError("Duplicate or noncanonical round keys")
    pending = state.get("pending")
    if (state["mode"] == "WAITING_FOR_SETTLEMENT") != (pending is not None):
        raise StateError("Pending trade and state mode disagree")
    seen, events = [], set()
    loss_seen = False
    calculated = initial
    for trade in history:
        if loss_seen:
            raise StateError("History contains a trade after the permanent loss stop")
        normalized = _trade(trade)
        if any(not isinstance(trade.get(key), str) for key in ("budget", "debit", "payout", "fees")):
            raise StateError("Trade money must be decimal strings")
        seen.append(normalized["round_key"])
        event = trade.get("event_id")
        if not isinstance(event, str) or not event or event in events:
            raise StateError("Missing or duplicate event identity")
        events.add(event)
        result = str(trade.get("result", "")).upper()
        if result not in {"YES", "NO", "VOID"}:
            raise StateError("Unknown settled result")
        won = None if result == "VOID" else normalized["side"] == result
        if trade.get("won") is not won:
            raise StateError("Result and won flag disagree")
        if won is False and state["mode"] != "STOPPED":
            raise StateError("A losing session must remain STOPPED")
        loss_seen = won is False
        debit = _decimal(trade["debit"], "debit")
        credit = debit if result == "VOID" else (_decimal(trade["payout"], "payout") if won else Decimal(0))
        if _decimal(trade.get("pnl"), "pnl") != credit - debit or _decimal(trade.get("cash_payout"), "cash payout") != credit:
            raise StateError("Settlement accounting does not reconcile")
        calculated += credit - debit
        if "balance_after" in trade and _decimal(trade["balance_after"], "balance after") != calculated:
            raise StateError("Historical balance does not reconcile")
    if pending is not None:
        normalized = _trade(pending)
        if any(not isinstance(pending.get(key), str) for key in ("budget", "debit", "payout", "fees")):
            raise StateError("Pending money must be decimal strings")
        seen.append(normalized["round_key"])
        if pending.get("result") not in (None, "") or "won" in pending:
            raise StateError("Pending trade already has a result")
        event = pending.get("event_id")
        if not isinstance(event, str) or not event or event in events:
            raise StateError("Missing or duplicate pending identity")
        calculated -= _decimal(pending["debit"], "debit")
    if len(seen) != len(set(seen)) or set(seen) != set(rounds):
        raise StateError("Round history and pending position disagree")
    if balance != calculated:
        raise StateError("Cash balance does not reconcile with the ledger")
    return state


def load(path, *, allow_create=False):
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        if allow_create:
            return new_state()
        raise StateError("Missing persistent state; explicit initialization is required") from None
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise StateError("Unreadable persistent state; refusing to reset") from exc
    return migrate(raw)


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
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


def save(path, state):
    validate(state)
    _atomic_write(path, (json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())


def can_enter(state, round_key):
    validate(state)
    key = canonical_round_key(round_key)
    return state["mode"] == "SCANNING" and state["pending"] is None and key not in state["rounds_traded"]


def record_entry(state, round_key, trade):
    key = canonical_round_key(round_key)
    if not can_enter(state, key):
        raise StateError("Paper entry refused: pending, stopped, or round already traded")
    pending = _trade(trade, round_key=key)
    if _decimal(pending["debit"], "debit") > _decimal(state["balance"], "balance"):
        raise StateError("Insufficient paper cash")
    pending["event_id"] = f"{state['session_id']}:{key}"
    pending.setdefault("entered_at", time.time())
    updated = copy.deepcopy(state)
    updated["pending"] = pending
    updated["rounds_traded"].append(key)
    updated["balance"] = _s(_decimal(state["balance"], "balance") - _decimal(pending["debit"], "debit"))
    updated["mode"] = "WAITING_FOR_SETTLEMENT"
    validate(updated)
    state.clear()
    state.update(updated)
    return pending["event_id"]


def _settle(state, result, *, settled_at=None):
    validate(state)
    if state["pending"] is None:
        raise StateError("No pending position to settle")
    updated = copy.deepcopy(state)
    trade = updated["pending"]
    won = None if result == "VOID" else trade["side"].lower() == result
    debit = _decimal(trade["debit"], "debit")
    credit = debit if result == "VOID" else (_decimal(trade["payout"], "payout") if won else Decimal(0))
    updated["balance"] = _s(_decimal(updated["balance"], "balance") + credit)
    updated["mode"] = "STOPPED" if won is False else "SCANNING"
    trade.update(result=result, won=won, pnl=_s(credit - debit), cash_payout=_s(credit),
                 settled_at=time.time() if settled_at is None else settled_at,
                 balance_after=updated["balance"], state_after=updated["mode"])
    updated["history"].append(trade)
    updated["pending"] = None
    validate(updated)
    state.clear()
    state.update(updated)
    return won


def record_settlement(state, official_result, *, settled_at=None):
    """Only an official YES/NO result settles; unknown/delayed values are inert."""
    validate(state)
    result = str(official_result or "").strip().lower()
    if result not in {"yes", "no"}:
        return None
    return _settle(state, result, settled_at=settled_at)


def record_void(state, *, verified=False, settled_at=None):
    """Explicit verified cancellation policy: refund, zero P&L, keep round used."""
    if verified is not True:
        raise StateError("Void requires a verified official final cancellation")
    return _settle(state, "VOID", settled_at=settled_at)


LEDGER_FIELDS = ["session_id", "event_id", "round_key", "ticker", "side", "index_time",
                 "entered_at", "seconds_to_close", "ask", "probability", "probability_field", "current",
                 "underlying", "target", "distance_pct", "budget", "debit", "fees",
                 "quantity", "unused_cash", "payout", "exact_multiplier",
                 "displayed_multiplier", "quote_source", "ui_parity_verified", "result",
                 "pnl", "cash_payout", "settled_at", "balance_after", "state_after",
                 "source_sha"]


def write_ledger(path, state):
    validate(state)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for trade in state["history"] + ([state["pending"]] if state["pending"] else []):
        row = dict(trade, session_id=state["session_id"])
        if trade is state["pending"]:
            row.update(balance_after=state["balance"], state_after=state["mode"])
        writer.writerow(row)
    _atomic_write(path, output.getvalue().encode())


def archive_and_reset(path, *, confirm=False, archive_dir=None, initial_balance="1000.00", timestamp=None):
    """Explicit owner-authorized reset; archive the exact old bytes first."""
    if confirm is not True:
        raise StateError("A new session requires explicit reset confirmation")
    path = Path(path)
    old = load(path)
    if old["pending"] is not None:
        raise StateError("Cannot reset a session with an unsettled position")
    stamp = timestamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if not isinstance(stamp, str) or any(c not in "0123456789TZ.-" for c in stamp):
        raise StateError("Invalid archive timestamp")
    directory = Path(archive_dir) if archive_dir is not None else path.parent / "ledger_archive"
    archived = directory / (stamp + ".json")
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an earlier session archive.
    with archived.open("xb") as handle:
        handle.write(path.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    fresh = new_state(initial_balance)
    fresh["previous_session_archive"] = str(archived)
    save(path, fresh)
    return fresh, archived
