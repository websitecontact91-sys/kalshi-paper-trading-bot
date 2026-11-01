"""Opt-in live execution with durable intent and reconciliation before another order.

No network operations occur at import. A disabled engine cannot submit an order.
Tests inject a broker; this module never retries a submission after uncertainty.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
import time
import uuid

from live_broker import BrokerError, OrderRejected, TradingDisabled, worst_case_debit
from live_state import (LiveStateError, execution_breaches, money, validate, apply_balance,
                        FUNDS_POLICY, NON_HALTING_EXECUTION_WARNINGS, warning_only_halt)
from live_stakes import MAX_LIVE_BUDGET, NEW_LIVE_BUDGETS
from payout_quote import PayoutQuote, ask_in_band, payout_eligible, as_decimal
from quote_adapter import documented_taker_cost
from timing import (MAX_FUTURE_S, entry_time_eligible, in_entry_window,
                    observation_fresh, round_key, timestamp)
from trade_config import TRADE_BUDGET

FUNDS_CHECK_INTERVAL_SECONDS = 5


class LiveExecutionError(RuntimeError):
    pass


class CandidateInvalid(ValueError):
    pass


class ReconciliationMismatch(ValueError):
    pass


def iso(value):
    return datetime.fromtimestamp(timestamp(value), timezone.utc).isoformat()


def _side(record):
    outcome, book = record.get("outcome_side"), record.get("book_side")
    outcome = str(outcome).upper() if outcome is not None else None
    from_book = {"bid": "YES", "ask": "NO"}.get(book)
    if outcome is not None and outcome not in {"YES", "NO"}:
        raise ReconciliationMismatch("unknown_outcome_side")
    if book is not None and from_book is None:
        raise ReconciliationMismatch("unknown_book_side")
    if outcome and from_book and outcome != from_book:
        raise ReconciliationMismatch("inconsistent_direction")
    if not (outcome or from_book):
        raise ReconciliationMismatch("canonical_direction_missing")
    return outcome or from_book


def _decimal(record, field, *, nonnegative=True):
    try:
        value = as_decimal(record[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReconciliationMismatch("invalid_" + field) from exc
    if nonnegative and value < 0:
        raise ReconciliationMismatch("negative_" + field)
    return value


def _record_matches(record, pending, subaccount):
    if record.get("ticker", record.get("market_ticker")) != pending["ticker"]:
        raise ReconciliationMismatch("market_identity_mismatch")
    if _side(record) != pending["side"]:
        raise ReconciliationMismatch("outcome_identity_mismatch")
    if record.get("subaccount_number", subaccount) != subaccount:
        raise ReconciliationMismatch("account_identity_mismatch")


def normalize_execution(order, fills, pending, *, subaccount=0, now=None):
    """Verify every attributed fill and reconcile it to the expanded order."""
    if not isinstance(order, dict) or not isinstance(fills, list):
        raise ReconciliationMismatch("invalid_order_or_fill_response")
    _record_matches(order, pending, subaccount)
    if order.get("client_order_id") != pending["client_order_id"]:
        raise ReconciliationMismatch("client_order_identity_mismatch")
    order_id = order.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ReconciliationMismatch("missing_order_identity")
    if pending.get("order_id") and pending["order_id"] != order_id:
        raise ReconciliationMismatch("order_identity_mismatch")
    requested = _decimal(order, "initial_count_fp")
    if requested != as_decimal(pending["quantity"]):
        raise ReconciliationMismatch("requested_quantity_mismatch")
    declared = _decimal(order, "fill_count_fp")
    remaining = _decimal(order, "remaining_count_fp")
    if declared + remaining > requested:
        raise ReconciliationMismatch("order_counts_exceed_request")
    unique = {}
    for fill in fills:
        if not isinstance(fill, dict):
            raise ReconciliationMismatch("invalid_fill")
        identity = fill.get("fill_id")
        if not isinstance(identity, str) or not identity:
            raise ReconciliationMismatch("missing_fill_identity")
        if identity in unique and unique[identity] != fill:
            raise ReconciliationMismatch("conflicting_duplicate_fill")
        unique[identity] = fill
    checked_at = timestamp(time.time() if now is None else now)
    previous_fills = {x["fill_id"]: x for x in (pending.get("execution") or {}).get("fills", [])}
    earliest = max(timestamp(pending["prepared_at"]), timestamp(pending.get("submitted_at", pending["prepared_at"])))
    quantity, principal, fees = Decimal("0"), Decimal("0"), Decimal("0")
    normalized, flags = [], []
    for identity, fill in sorted(unique.items()):
        _record_matches(fill, pending, subaccount)
        if fill.get("order_id") != order_id:
            raise ReconciliationMismatch("foreign_fill")
        count = _decimal(fill, "count_fp")
        if count <= 0 or count % Decimal("0.01"):
            raise ReconciliationMismatch("invalid_fill_quantity")
        yes_price = _decimal(fill, "yes_price_dollars")
        no_price = _decimal(fill, "no_price_dollars")
        if not 0 < yes_price < 1 or not 0 < no_price < 1 or yes_price + no_price != 1:
            raise ReconciliationMismatch("invalid_complementary_fill_prices")
        price = yes_price if pending["side"] == "YES" else no_price
        fee = _decimal(fill, "fee_cost")
        try:
            filled_at = timestamp(fill.get("created_time", fill.get("ts")))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReconciliationMismatch("missing_fill_time") from exc
        if not in_entry_window(pending["close_time"], filled_at):
            flags.append("fill_outside_final_minute")
        if filled_at < earliest:
            flags.append("fill_precedes_order_intent_or_submission")
        if not observation_fresh(pending.get("observation_time"), filled_at):
            flags.append("fill_reference_stale")
        if filled_at > checked_at + MAX_FUTURE_S:
            flags.append("fill_time_in_future")
        if price > as_decimal(pending["ask"]):
            flags.append("fill_exceeds_limit")
        if not ask_in_band(price):
            flags.append("fill_price_out_of_band")
        if fill.get("is_taker") is not True:
            flags.append("unexpected_maker_fill")
        quantity += count
        principal += count * price
        fees += fee
        normalized.append({"fill_id": identity, "quantity": str(count), "price": str(price),
                           "fees": str(fee), "filled_at": iso(filled_at), "is_taker": fill.get("is_taker") is True,
                           "observed_at": previous_fills.get(identity, {}).get("observed_at", iso(checked_at))})
    if quantity > declared:
        raise ReconciliationMismatch("fills_exceed_order_quantity")
    if quantity < declared:
        return None, ["awaiting_all_fills"]
    order_principal = _decimal(order, "taker_fill_cost_dollars") + _decimal(order, "maker_fill_cost_dollars")
    order_fees = _decimal(order, "taker_fees_dollars") + _decimal(order, "maker_fees_dollars")
    if principal != order_principal or fees != order_fees:
        raise ReconciliationMismatch("order_and_fill_costs_differ")
    if not quantity:
        return None, []
    debit = principal + fees
    if debit <= 0:
        raise ReconciliationMismatch("nonpositive_actual_debit")
    planned_budget = as_decimal(pending["budget"])
    quote = PayoutQuote(max(planned_budget, debit), debit, quantity)
    if debit > planned_budget:
        flags.append("actual_debit_exceeds_budget")
    if not payout_eligible(quote):
        flags.append("actual_payout_multiple_out_of_band")
    if quantity != requested:
        flags.append("partial_fill_of_fill_or_kill_order")
    if remaining > 0 or order.get("status") == "resting":
        flags.append("unexpected_remaining_order")
    if order.get("status") not in {"executed", "canceled", "cancelled"}:
        flags.append("order_not_terminal")
    execution = {"quantity": str(quantity), "principal": str(principal), "fees": str(fees),
                 "debit": str(debit), "payout": str(quantity), "weighted_price": str(principal / quantity),
                 "displayed_multiplier": str(quote.displayed_multiplier),
                 "fill_ids": sorted(unique), "fills": normalized,
                 "first_filled_at": min(x["filled_at"] for x in normalized),
                 "last_filled_at": max(x["filled_at"] for x in normalized)}
    return execution, sorted(set(flags))


def verify_settlement(market, settlements, pending):
    """An aggregate account settlement must exactly match this bot's exposure."""
    if market.get("ticker") != pending["ticker"]:
        raise ReconciliationMismatch("settlement_market_identity_mismatch")
    status = market.get("status")
    outcome = str(market.get("result") or "").upper()
    binary = status in {"settled", "finalized"} and outcome in {"YES", "NO"}
    void = status in {"voided", "canceled", "cancelled"} and outcome in {"", "VOID", "CANCELED", "CANCELLED"}
    if not binary and not void:
        return None
    if not settlements:
        return None
    if len(settlements) != 1:
        raise ReconciliationMismatch("ambiguous_account_settlement")
    row = settlements[0]
    if row.get("ticker") != pending["ticker"]:
        raise ReconciliationMismatch("account_settlement_ticker_mismatch")
    account_result = str(row.get("market_result") or "").upper()
    if binary and account_result != outcome:
        raise ReconciliationMismatch("official_results_disagree")
    if void and account_result not in {"VOID", "CANCELED", "CANCELLED"}:
        return None
    execution = pending["execution"]
    selected = pending["side"].lower()
    opposite = "no" if selected == "yes" else "yes"
    if (_decimal(row, selected + "_count_fp") != as_decimal(execution["quantity"])
            or _decimal(row, opposite + "_count_fp") != 0
            or _decimal(row, selected + "_total_cost_dollars") != as_decimal(execution["principal"])
            or _decimal(row, opposite + "_total_cost_dollars") != 0
            or _decimal(row, "fee_cost") != as_decimal(execution["fees"])):
        raise ReconciliationMismatch("settlement_contains_unattributed_exposure_or_fees")
    revenue = row.get("revenue")
    if isinstance(revenue, bool) or not isinstance(revenue, int) or revenue < 0:
        raise ReconciliationMismatch("invalid_settlement_revenue")
    revenue = Decimal(revenue) / 100
    try:
        settled_at = timestamp(row["settled_time"])
        if settled_at < timestamp(execution["last_filled_at"]):
            raise ValueError("settlement precedes fill")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReconciliationMismatch("invalid_settlement_time") from exc
    won = None if void else outcome == pending["side"]
    expected = as_decimal(execution["debit"]) if void else as_decimal(execution["payout"]) if won else Decimal("0")
    if revenue != expected:
        raise ReconciliationMismatch("actual_settlement_payout_differs")
    return {"result": "VOID" if void else outcome, "won": won, "cash_payout": str(revenue),
            "pnl": str(revenue - as_decimal(execution["debit"])), "settled_at": iso(settled_at)}


class LiveExecution:
    def __init__(self, broker, state, *, persist_state, official_market, armed=False,
                 clock=time.time, source="unknown", require_remote_durability=False,
                 candidate_refresher=None):
        validate(state)
        if state["account_scope"] != getattr(broker, "account_scope", None):
            raise LiveExecutionError("Live journal belongs to a different account/key scope")
        if not isinstance(armed, bool):
            raise TypeError("armed must be a boolean")
        self.broker, self.state = broker, copy.deepcopy(state)
        self.persist_state, self.official_market = persist_state, official_market
        self.candidate_refresher = candidate_refresher
        self.armed, self.clock, self.source = armed, clock, source
        self.require_remote_durability = require_remote_durability
        self.source_update = None
        self.last_event = None
        # Never trust a saved account snapshot as fresh after a process restart.
        self._last_funds_check = None
        self._funds_check_ok = False

    def _event(self, kind, **fields):
        self.last_event = {"kind": kind, "at": iso(self.clock()), **fields}

    def _commit(self, updated, kind):
        validate(updated)
        receipt = self.persist_state(copy.deepcopy(updated))
        if (not isinstance(receipt, dict) or
                not (receipt.get("durable") is True or receipt.get("remote") is True)):
            raise LiveExecutionError("Live journal persistence did not confirm durability")
        if self.require_remote_durability and receipt.get("remote") is not True:
            raise LiveExecutionError("Live execution requires confirmed remote journal durability")
        self.state = updated
        self._event(kind, mode=updated["mode"])
        if receipt.get("restart_required"):
            self.source_update = {key: receipt.get(key) for key in ("remote_source_sha", "state_commit")}
            self._event("SOURCE_UPDATE_HANDOFF", **self.source_update)

    def _halt(self, reason, pending=None):
        updated = copy.deepcopy(self.state)
        updated.update(mode="HALTED", halt_reason=reason, stop_reason=None)
        if pending is not None:
            updated["pending"] = pending
        if updated != self.state:
            self._commit(updated, "LIVE_HALTED")
        return self.state["mode"]

    def check_funds(self, *, force=False):
        """Check available cash before scanning, including when no quote exists.

        The short cache is process-local only. Every submission forces a new
        read, and pending orders bypass this gate so settlement is never lost.
        A failed read blocks this scan without turning a transient outage into
        a permanent execution-integrity halt.
        """
        if self.source_update is not None:
            return "SOURCE_UPDATE_HANDOFF"
        if self.state.get("pending") is not None:
            return self.state["mode"]
        halt_reason = self.state.get("halt_reason")
        if halt_reason and not warning_only_halt(halt_reason):
            return self._halt(halt_reason)
        if any(execution_breaches(item) for item in self.state["history"] + self.state["attempts"]):
            return self._halt("historical_execution_breach")
        validate(self.state)
        now = self.clock()
        if (not force and self._funds_check_ok and self._last_funds_check is not None
                and 0 <= now - self._last_funds_check < FUNDS_CHECK_INTERVAL_SECONDS):
            return self.state["mode"]
        try:
            balance = self.broker.balance()
            cash = money(balance["available_balance_usd"], "available account cash", nonnegative=True)
        except Exception as exc:
            self._funds_check_ok = False
            self._event("LIVE_BALANCE_UNAVAILABLE", reason=type(exc).__name__)
            return "BLOCKED_BALANCE_UNAVAILABLE"
        checked_at = self.clock()
        updated = apply_balance(self.state, cash, iso(checked_at))
        transition = any(updated.get(key) != self.state.get(key)
                         for key in ("mode", "funds_policy", "stop_reason", "halt_reason"))
        if transition:
            self._commit(updated, "LIVE_INSUFFICIENT_FUNDS" if updated["mode"] == "STOPPED" else "LIVE_FUNDS_READY")
        else:
            # Observational fields are kept in memory until the next durable
            # order/settlement transition; polling does not create Git commits.
            self.state = updated
        self._last_funds_check, self._funds_check_ok = checked_at, True
        return "SOURCE_UPDATE_HANDOFF" if self.source_update is not None else self.state["mode"]

    def _terminal_balance(self, updated):
        """Choose the post-order mode using a fresh balance, never realized P/L."""
        updated.update(mode="HALTED" if updated.get("halt_reason") else "SCANNING",
                       account_cash=None, account_cash_checked_at=None,
                       funds_policy=FUNDS_POLICY, stop_reason=None)
        self._funds_check_ok = False
        try:
            balance = self.broker.balance()
            cash = money(balance["available_balance_usd"], "available account cash", nonnegative=True)
        except Exception as exc:
            # The official settlement remains durable. The next scan must get
            # a fresh balance successfully before it may inspect candidates.
            self._event("LIVE_BALANCE_UNAVAILABLE", reason=type(exc).__name__)
            return updated
        checked_at = self.clock()
        updated = apply_balance(updated, cash, iso(checked_at))
        self._last_funds_check, self._funds_check_ok = checked_at, True
        return updated

    def _validate_candidate(self, candidate):
        try:
            c = copy.deepcopy(candidate)
            if not isinstance(c.get("ticker"), str) or not c["ticker"] or c.get("side") not in {"YES", "NO"}:
                raise ValueError("identity")
            if c.get("round_key") != round_key(c["close_time"]):
                raise ValueError("round")
            now = self.clock()
            if not entry_time_eligible(c["close_time"], now) or not observation_fresh(c["observation_time"], now):
                raise ValueError("entry_window_or_reference_expired")
            current = as_decimal(c["current"])
            target = as_decimal(c["target"])
            if current <= 0 or target <= 0:
                raise ValueError("invalid_reference_or_target")
            signed_difference = current - target if c["side"] == "YES" else target - current
            if signed_difference <= 0:
                raise ValueError("selected_side_not_confirmed")
            c["current"], c["target"] = str(current), str(target)
            for field in ("ask", "quantity", "budget", "debit", "payout", "principal", "fees", "fee_multiplier"):
                c[field] = str(as_decimal(c[field]))
            c["balance_precision"] = str(as_decimal(c.get("balance_precision", "0.01")))
            quote = PayoutQuote(c["budget"], c["debit"], c["payout"])
            if quote.budget not in NEW_LIVE_BUDGETS or quote.budget > MAX_LIVE_BUDGET:
                raise ValueError("unsupported_live_stake_budget")
            if not ask_in_band(c["ask"]) or not payout_eligible(quote):
                raise ValueError("strategy_budget_or_price_or_payout")
            quantity = as_decimal(c["quantity"])
            if quantity <= 0 or quantity % Decimal("0.01") or quote.payout != quantity:
                raise ValueError("quantity")
            if (as_decimal(c["principal"]) != quantity * as_decimal(c["ask"])
                    or as_decimal(c["fees"]) < 0
                    or as_decimal(c["principal"]) + as_decimal(c["fees"]) != quote.debit):
                raise ValueError("quoted_costs_do_not_reconcile")
            principal, fees, debit = documented_taker_cost(c["ask"], c["quantity"], c["fee_multiplier"], c["balance_precision"])
            if (principal != as_decimal(c["principal"]) or fees != as_decimal(c["fees"])
                    or debit != quote.debit):
                raise ValueError("quoted_costs_do_not_match_fee_model")
            maximum = worst_case_debit(c["ask"], c["quantity"], c["fee_multiplier"], c["balance_precision"])
            if maximum > quote.budget:
                raise ValueError("worst_case_total_exceeds_budget")
            c["worst_case_debit"] = str(maximum)
            return c
        except (KeyError, TypeError, ValueError, ArithmeticError, BrokerError) as exc:
            raise CandidateInvalid("Live candidate failed final validation: " + str(exc)) from exc

    @staticmethod
    def _signed_strike_distance(candidate):
        current = as_decimal(candidate["current"])
        target = as_decimal(candidate["target"])
        return current - target if candidate["side"] == "YES" else target - current

    def _locked_candidate(self, candidate):
        """Refresh the exact selected market and revalidate the live strategy.

        Normal executable-price/depth drift is allowed when the refreshed trade
        still satisfies every live rule. Market identity, selected outcome,
        round, strike and stake tier remain locked. The selected-side cushion
        from the strike may stay unchanged or increase, but may not shrink.
        """
        previous = self._validate_candidate(candidate)
        if self.candidate_refresher is None:
            return previous
        fresh = self._validate_candidate(self.candidate_refresher(copy.deepcopy(previous)))
        identity_fields = ("ticker", "side", "round_key", "close_time", "target", "budget")
        for field in identity_fields:
            if str(fresh.get(field)) != str(previous.get(field)):
                raise CandidateInvalid("final_refresh_identity_changed:" + field)
        if self._signed_strike_distance(fresh) < self._signed_strike_distance(previous):
            raise CandidateInvalid("underlying_moved_toward_strike")
        return fresh

    def _refresh_pending_intent(self, candidate):
        """Durably replace the intended order terms with the latest valid refresh."""
        updated = copy.deepcopy(self.state)
        pending = updated["pending"]
        pending.update(
            ticker=candidate["ticker"], side=candidate["side"],
            round_key=candidate["round_key"], close_time=candidate["close_time"],
            observation_time=candidate["observation_time"], ask=candidate["ask"],
            quantity=candidate["quantity"], budget=candidate["budget"],
            fee_multiplier=candidate["fee_multiplier"],
            balance_precision=candidate["balance_precision"],
            worst_case_debit=candidate["worst_case_debit"],
            quoted_debit=candidate["debit"], quoted_payout=candidate["payout"],
            quoted_fees=candidate["fees"], quoted_principal=candidate["principal"])
        for key in ("current", "target", "distance_pct", "series_ticker", "probability_field"):
            if key in candidate:
                pending[key] = str(candidate[key])
        self._commit(updated, "LIVE_INTENT_REFRESHED")

    def submit(self, candidate):
        if self.source_update is not None:
            return "SOURCE_UPDATE_HANDOFF"
        if self.state["pending"] is not None:
            return self.reconcile()
        if self.state["mode"] == "HALTED":
            return self.state["mode"]
        if self.state.get("halt_reason") or any(execution_breaches(item) for item in self.state["history"]):
            return self._halt(self.state.get("halt_reason") or "historical_execution_breach")
        validate(self.state)
        if not self.armed or getattr(self.broker, "writes_enabled", False) is not True:
            return "DISABLED"
        c = self._validate_candidate(candidate)
        if c["round_key"] in self.state["attempted_rounds"]:
            return "BLOCKED_ALREADY_ATTEMPTED_ROUND"
        try:
            status = self.broker.exchange_status()
            if status.get("exchange_active") is not True or status.get("trading_active") is not True:
                return "BLOCKED_EXCHANGE_INACTIVE"
            shards = status.get("exchange_index_statuses", [])
            if not isinstance(shards, list) or any(x.get("exchange_active") is not True or x.get("trading_active") is not True for x in shards):
                return "BLOCKED_EXCHANGE_INACTIVE"
        except Exception as exc:
            self._event("LIVE_PREFLIGHT_UNAVAILABLE", reason=type(exc).__name__)
            return "BLOCKED_ACCOUNT_PREFLIGHT"
        funds = self.check_funds(force=True)
        if funds != "SCANNING":
            return funds
        cash = money(self.state["account_cash"], "available account cash", nonnegative=True)
        if cash < as_decimal(c["budget"]):
            self._event("LIVE_STAKE_FUNDS_UNAVAILABLE", required_budget=c["budget"], account_cash=str(cash))
            return "BLOCKED_INSUFFICIENT_STAKE_FUNDS"
        try:
            if self.broker.positions(c["ticker"]) or self.broker.orders(c["ticker"]):
                return "BLOCKED_EXISTING_EXPOSURE"
        except Exception as exc:
            self._event("LIVE_PREFLIGHT_UNAVAILABLE", reason=type(exc).__name__)
            return "BLOCKED_ACCOUNT_PREFLIGHT"
        # Account reads can consume several seconds in the final minute. Re-read
        # the exact selected market after those checks. Normal quote/depth drift
        # is allowed only when the refreshed trade still satisfies every live
        # rule and its selected-side cushion from the strike has not shrunk.
        c = self._locked_candidate(c)
        pending = {key: c[key] for key in ("ticker", "side", "round_key", "close_time", "observation_time",
                    "ask", "quantity", "budget", "fee_multiplier", "balance_precision", "worst_case_debit")}
        pending.update(quoted_debit=c["debit"], quoted_payout=c["payout"], quoted_fees=c["fees"],
                       quoted_principal=c["principal"], stage="INTENT", execution=None, order_id=None,
                       prepared_at=iso(self.clock()), source_sha=self.source, risk_flags=[])
        for key in ("current", "target", "distance_pct", "series_ticker", "probability_field"):
            if key in c:
                pending[key] = str(c[key])
        pending["client_order_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL,
            self.state["account_scope"] + ":" + self.state["session_id"] + ":" + c["round_key"]))
        updated = copy.deepcopy(self.state)
        updated.update(mode="RECONCILING", pending=pending, account_cash=str(cash), stop_reason=None)
        updated["attempted_rounds"].append(c["round_key"])
        self._commit(updated, "LIVE_INTENT_DURABLE")
        if self.source_update is not None:
            return "SOURCE_UPDATE_HANDOFF"
        if not entry_time_eligible(c["close_time"], self.clock()) or not observation_fresh(c["observation_time"], self.clock()):
            return self._finish_unfilled("NOT_SENT", "expired_after_durable_intent")
        # The durable write itself may consume time. Refresh once more immediately
        # before POST and require the refreshed trade to remain fully eligible
        # without moving toward the strike. Persist the latest intended order
        # terms before the POST so reconciliation never depends on stale values.
        try:
            c = self._locked_candidate(c)
        except CandidateInvalid as exc:
            reason = ("underlying_moved_toward_strike"
                      if "underlying_moved_toward_strike" in str(exc)
                      else "final_refresh_no_longer_eligible")
            return self._finish_unfilled("NOT_SENT", reason)
        self._refresh_pending_intent(c)
        if self.source_update is not None:
            return "SOURCE_UPDATE_HANDOFF"
        if not entry_time_eligible(c["close_time"], self.clock()) or not observation_fresh(c["observation_time"], self.clock()):
            return self._finish_unfilled("NOT_SENT", "expired_after_final_refresh")
        pending = self.state["pending"]
        submitted_at = iso(self.clock())
        try:
            ack = self.broker.submit_limit_buy(ticker=c["ticker"], side=c["side"], quantity=c["quantity"],
                price=c["ask"], client_order_id=pending["client_order_id"], expected_debit=c["debit"],
                fee_multiplier=c["fee_multiplier"], balance_precision=c["balance_precision"],
                budget=c["budget"])
        except TradingDisabled:
            return self._finish_unfilled("NOT_SENT", "broker_disabled")
        except OrderRejected:
            # Definite rejection is terminal; the round still cannot be retried.
            return self._finish_unfilled("REJECTED", "exchange_rejected_order")
        except Exception as exc:
            self._event("LIVE_SUBMISSION_UNCERTAIN", reason=type(exc).__name__)
            # The saved INTENT is authoritative. Never blindly POST it again.
            return "RECONCILING"
        if (not isinstance(ack, dict) or not isinstance(ack.get("order_id"), str)
                or not ack["order_id"] or ack.get("client_order_id") != pending["client_order_id"]):
            self._event("LIVE_SUBMISSION_UNCERTAIN", reason="invalid_acknowledgement")
            return "RECONCILING"
        updated = copy.deepcopy(self.state)
        updated["pending"].update(order_id=ack["order_id"], submitted_at=submitted_at, stage="ACKNOWLEDGED")
        self._commit(updated, "LIVE_ORDER_ACKNOWLEDGED")
        return "SOURCE_UPDATE_HANDOFF" if self.source_update is not None else self.reconcile()

    def _finish_unfilled(self, outcome, reason):
        updated = copy.deepcopy(self.state)
        attempt = updated["pending"]
        if attempt is None or attempt.get("execution"):
            raise LiveExecutionError("Cannot discard an observed live fill")
        attempt.update(outcome=outcome, reason=reason, finished_at=iso(self.clock()))
        updated["attempts"].append(attempt)
        updated["pending"] = None
        updated = self._terminal_balance(updated)
        self._commit(updated, "LIVE_UNFILLED")
        return self.state["mode"]

    def reconcile(self):
        if self.source_update is not None:
            return "SOURCE_UPDATE_HANDOFF"
        if self.state["pending"] is None:
            return self.check_funds(force=True)
        pending = copy.deepcopy(self.state["pending"])
        try:
            order = (self.broker.get_order(pending["order_id"]) if pending.get("order_id")
                     else self.broker.find_order(pending["client_order_id"], pending["ticker"]))
            if order is None:
                self._event("LIVE_ORDER_IDENTITY_PENDING")
                return self.state["mode"]
            fills = self.broker.fills(order["order_id"], pending["ticker"])
        except Exception as exc:
            self._event("LIVE_RECONCILIATION_UNAVAILABLE", reason=type(exc).__name__)
            return self.state["mode"]
        try:
            execution, flags = normalize_execution(order, fills, pending, subaccount=self.broker.subaccount, now=self.clock())
            if "awaiting_all_fills" in flags:
                self._event("LIVE_FILLS_PENDING")
                return self.state["mode"]
            if pending.get("execution"):
                previous = set(pending["execution"]["fill_ids"])
                if execution is None or not previous.issubset(execution["fill_ids"]):
                    raise ReconciliationMismatch("previously_recorded_fills_disappeared")
                current_fills = {x["fill_id"]: x for x in execution["fills"]}
                for old_fill in pending["execution"]["fills"]:
                    new_fill = current_fills[old_fill["fill_id"]]
                    if (any(as_decimal(old_fill[key]) != as_decimal(new_fill[key]) for key in ("quantity", "price", "fees"))
                            or timestamp(old_fill["filled_at"]) != timestamp(new_fill["filled_at"])
                            or old_fill["is_taker"] is not new_fill["is_taker"]):
                        raise ReconciliationMismatch("previously_recorded_fill_changed")
            if execution is None:
                if order.get("status") in {"canceled", "cancelled"} and _decimal(order, "remaining_count_fp") == 0:
                    return self._finish_unfilled("UNFILLED", "fill_or_kill_unfilled")
                if order.get("status") == "resting" or _decimal(order, "remaining_count_fp") > 0:
                    return self._halt("unexpected_remaining_order")
                self._event("LIVE_ORDER_TERMINAL_STATE_PENDING")
                return self.state["mode"]
            pending.update(order_id=order["order_id"], stage="FILLED", execution=execution,
                           risk_flags=sorted(set(pending.get("risk_flags", []) + flags)))
            updated = copy.deepcopy(self.state)
            updated["pending"] = pending
            blocking_flags = sorted(set(flags) - NON_HALTING_EXECUTION_WARNINGS)
            if blocking_flags or (updated.get("halt_reason") and not warning_only_halt(updated.get("halt_reason"))):
                updated.update(mode="HALTED", halt_reason=updated.get("halt_reason") or ",".join(blocking_flags))
            else:
                updated["halt_reason"] = None
                updated["mode"] = "WAITING_FOR_SETTLEMENT"
            if updated != self.state:
                self._commit(updated, "LIVE_FILLS_VERIFIED")
            if self.source_update is not None:
                return "SOURCE_UPDATE_HANDOFF"
        except ReconciliationMismatch as exc:
            return self._halt(str(exc))
        try:
            market = self.official_market(pending["ticker"])
            settlements = self.broker.settlements(pending["ticker"])
            result = verify_settlement(market, settlements, pending)
        except ReconciliationMismatch as exc:
            return self._halt(str(exc), pending)
        except Exception as exc:
            self._event("LIVE_SETTLEMENT_PENDING", reason=type(exc).__name__)
            return self.state["mode"]
        if result is None:
            return self.state["mode"]
        updated = copy.deepcopy(self.state)
        trade = updated["pending"]
        trade.update(result, stage="SETTLED")
        updated["history"].append(trade)
        updated["realized_pnl"] = str(as_decimal(updated["realized_pnl"]) + as_decimal(result["pnl"]))
        updated["pending"] = None
        updated = self._terminal_balance(updated)
        self._commit(updated, "LIVE_SETTLED")
        return self.state["mode"]

    def tick(self, candidate=None):
        if self.state["pending"] is not None:
            return self.reconcile()
        return self.submit(candidate) if candidate is not None else self.check_funds()
