"""Mocked live-order lifecycle tests. No real account writes are used."""
import copy
from decimal import Decimal as D
import json

import pytest

from live_broker import AmbiguousOrderError, OrderRejected, TradingDisabled
from live_execution import (CandidateInvalid, LiveExecution, LiveExecutionError,
                            iso, normalize_execution, ReconciliationMismatch)
from live_state import LiveStateError, load, new_state, save, validate
from timing import round_key

CLOSE = 1800000000
TICKER = "KXBTC15M-LIVE-TEST"


class Clock:
    def __init__(self): self.now = CLOSE - 30
    def __call__(self): return self.now


def candidate(clock, side="YES", **changes):
    result = {"ticker": TICKER, "side": side, "round_key": round_key(CLOSE),
              "close_time": iso(CLOSE), "observation_time": clock(), "ask": ".89",
              "quantity": "1.00", "budget": "1.00", "debit": ".90", "payout": "1.00",
              "principal": ".89", "fees": ".01", "fee_multiplier": "1", "balance_precision": ".01"}
    result.update(changes)
    return result


class Broker:
    account_scope = "production:0:fake-key-scope"
    subaccount = 0
    writes_enabled = True

    def __init__(self, clock):
        self.clock, self.posts, self.reads = clock, [], []
        self.cash = "19.8499"
        self.status = {"exchange_active": True, "trading_active": True}
        self.existing_positions, self.existing_orders, self.settled = [], [], []
        self.order, self.executions, self.submit_error = None, [], None
        self.read_error = False
        self.snapshot_hook = None

    def balance(self):
        self.reads.append("balance")
        if self.snapshot_hook: self.snapshot_hook()
        return {"available_balance_usd": self.cash, "portfolio_value_usd": "0.09"}

    def exchange_status(self): return copy.deepcopy(self.status)
    def positions(self, ticker): return copy.deepcopy(self.existing_positions)
    def orders(self, ticker): return copy.deepcopy(self.existing_orders)

    def submit_limit_buy(self, **kwargs):
        self.posts.append(copy.deepcopy(kwargs))
        side = kwargs["side"]
        price, qty = D(kwargs["price"]), D(kwargs["quantity"])
        debit = D(kwargs["expected_debit"])
        fee = debit - price * qty
        self.order = {"order_id": "order-1", "client_order_id": kwargs["client_order_id"],
            "ticker": kwargs["ticker"], "outcome_side": side.lower(), "book_side": "bid" if side == "YES" else "ask",
            "initial_count_fp": str(qty), "fill_count_fp": str(qty), "remaining_count_fp": "0.00",
            "taker_fill_cost_dollars": str(price * qty), "maker_fill_cost_dollars": "0.00",
            "taker_fees_dollars": str(fee), "maker_fees_dollars": "0.00", "status": "executed", "subaccount_number": 0}
        self.executions = [{"fill_id": "fill-1", "order_id": "order-1", "ticker": kwargs["ticker"],
            "outcome_side": side.lower(), "book_side": "bid" if side == "YES" else "ask", "count_fp": str(qty),
            "yes_price_dollars": str(price if side == "YES" else 1 - price),
            "no_price_dollars": str(price if side == "NO" else 1 - price),
            "is_taker": True, "fee_cost": str(fee), "created_time": iso(self.clock()), "subaccount_number": 0}]
        if self.submit_error: raise self.submit_error
        return {"order_id": "order-1", "client_order_id": kwargs["client_order_id"],
                "fill_count": str(qty), "remaining_count": "0.00", "ts_ms": int(self.clock() * 1000)}

    def get_order(self, order_id):
        self.reads.append("get_order")
        if self.read_error: raise TimeoutError("fake")
        return copy.deepcopy(self.order)

    def find_order(self, client_order_id, ticker):
        self.reads.append("find_order")
        if self.read_error: raise TimeoutError("fake")
        return copy.deepcopy(self.order)

    def fills(self, order_id, ticker): return copy.deepcopy(self.executions)
    def settlements(self, ticker): return copy.deepcopy(self.settled)


class Fixture:
    def __init__(self, path):
        self.clock, self.path = Clock(), path
        self.broker = Broker(self.clock)
        self.market = {"ticker": TICKER, "status": "active", "result": ""}
        self.saved = []
        save(path, new_state(session_id="unit-session", account_scope=self.broker.account_scope))
        self.engine = self.restart(armed=True)

    def persist(self, state):
        self.saved.append(copy.deepcopy(state))
        return save(self.path, state)

    def restart(self, armed=False, **options):
        return LiveExecution(self.broker, load(self.path), persist_state=self.persist,
            official_market=lambda ticker: copy.deepcopy(self.market), armed=armed, clock=self.clock,
            source="mock-test", **options)

    def settle(self, result="yes", *, quantity=None, fees=None, revenue=None):
        order = self.broker.order
        side = order["outcome_side"]
        qty = D(quantity or order["fill_count_fp"])
        principal = D(order["taker_fill_cost_dollars"]) + D(order["maker_fill_cost_dollars"])
        fee = D(fees or order["taker_fees_dollars"])
        if revenue is None:
            revenue = int(qty * 100) if result == side else 0
        self.market.update(status="settled", result=result)
        self.broker.settled = [{"ticker": TICKER, "market_result": result,
            "yes_count_fp": str(qty if side == "yes" else 0),
            "no_count_fp": str(qty if side == "no" else 0),
            "yes_total_cost_dollars": str(principal if side == "yes" else 0),
            "no_total_cost_dollars": str(principal if side == "no" else 0),
            "fee_cost": str(fee), "revenue": revenue, "settled_time": iso(CLOSE + 30)}]


@pytest.fixture
def fx(tmp_path): return Fixture(tmp_path / "live-state.json")


def test_disabled_engine_never_touches_account_or_submission(fx):
    engine = fx.restart()
    assert engine.submit(candidate(fx.clock)) == "DISABLED"
    assert not fx.broker.posts and not fx.broker.reads
    assert load(fx.path)["pending"] is None


def test_broker_gate_is_independent_from_engine_gate(fx):
    fx.broker.writes_enabled = False
    assert fx.engine.submit(candidate(fx.clock)) == "DISABLED"
    assert not fx.broker.posts


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_intent_is_durable_before_first_post_and_ack_is_not_a_cash_fill(fx, side):
    original = fx.broker.submit_limit_buy
    def submit(**kwargs):
        durable = load(fx.path)
        assert durable["mode"] == "RECONCILING"
        assert durable["pending"]["execution"] is None
        assert durable["pending"]["client_order_id"] == kwargs["client_order_id"]
        assert durable["attempted_rounds"] == [round_key(CLOSE)]
        return original(**kwargs)
    fx.broker.submit_limit_buy = submit
    assert fx.engine.submit(candidate(fx.clock, side)) == "WAITING_FOR_SETTLEMENT"
    assert len(fx.broker.posts) == 1
    assert fx.engine.state["realized_pnl"] == "0.00"
    assert not fx.engine.state["history"]
    actual = fx.engine.state["pending"]["execution"]
    assert D(actual["debit"]) == D(".90") and D(actual["fees"]) == D(".01")


@pytest.mark.parametrize("remaining,allowed", [(61, False), (60, True), (1, True), (0, False), (-1, False)])
def test_exact_entry_window(fx, remaining, allowed):
    fx.clock.now = CLOSE - remaining
    if allowed:
        fx.engine.submit(candidate(fx.clock))
        assert len(fx.broker.posts) == 1
    else:
        with pytest.raises(CandidateInvalid): fx.engine.submit(candidate(fx.clock))
        assert not fx.broker.posts


@pytest.mark.parametrize("changes", [
    {"budget": "5.00"}, {"debit": "1.01", "fees": ".12"}, {"ask": ".91"}, {"ask": ".79"},
    {"quantity": "1.001"}, {"payout": "1.20"}, {"principal": ".88"}, {"fees": "-0.01"},
    {"observation_time": CLOSE - 100}, {"round_key": "1"}, {"balance_precision": ".001"},
    {"ask": float("nan")}, {"fee_multiplier": "-1"},
])
def test_invalid_or_over_budget_quotes_never_submit(fx, changes):
    with pytest.raises(CandidateInvalid): fx.engine.submit(candidate(fx.clock, **changes))
    assert not fx.broker.posts and not fx.engine.state["attempted_rounds"]


def test_worst_case_multifill_budget_blocks_nominally_affordable_quantity(fx):
    with pytest.raises(CandidateInvalid, match="worst_case"):
        fx.engine.submit(candidate(fx.clock, quantity="1.10", principal=".979", fees=".011", debit=".99", payout="1.10"))
    assert not fx.broker.posts


@pytest.mark.parametrize("mode", ["exchange", "shard", "cash", "position", "order"])
def test_account_preflight_blocks_without_reserving_a_round(fx, mode):
    if mode == "exchange": fx.broker.status["trading_active"] = False
    if mode == "shard": fx.broker.status["exchange_index_statuses"] = [{"exchange_index": 1, "exchange_active": True, "trading_active": False}]
    if mode == "cash": fx.broker.cash = ".99"
    if mode == "position": fx.broker.existing_positions = [{"ticker": TICKER, "position_fp": "1.00"}]
    if mode == "order": fx.broker.existing_orders = [{"ticker": TICKER, "status": "resting"}]
    assert fx.engine.submit(candidate(fx.clock)).startswith("BLOCKED_")
    assert not fx.broker.posts and not fx.engine.state["attempted_rounds"]


def test_preflight_latency_requires_another_window_and_freshness_check(fx):
    fx.broker.snapshot_hook = lambda: setattr(fx.clock, "now", CLOSE + 1)
    with pytest.raises(CandidateInvalid): fx.engine.submit(candidate(fx.clock))
    assert not fx.broker.posts and not load(fx.path)["pending"]


def test_persistence_latency_cannot_submit_after_close(fx):
    fx.clock.now = CLOSE - 1
    def slow(state):
        receipt = fx.persist(state)
        fx.clock.now = CLOSE + 1
        return receipt
    fx.engine.persist_state = slow
    assert fx.engine.submit(candidate(fx.clock)) == "SCANNING"
    assert not fx.broker.posts
    assert load(fx.path)["attempts"][0]["outcome"] == "NOT_SENT"


def test_pre_submission_persistence_failure_cannot_reach_broker(fx):
    def broken(state): raise OSError("mock persistence failure")
    fx.engine.persist_state = broken
    with pytest.raises(OSError): fx.engine.submit(candidate(fx.clock))
    assert not fx.broker.posts


@pytest.mark.parametrize("receipt", [None, {}, {"durable": False}, {"durable": "true"}])
def test_persistence_must_explicitly_confirm_durability(fx, receipt):
    fx.engine.persist_state = lambda state: receipt
    with pytest.raises(LiveExecutionError): fx.engine.submit(candidate(fx.clock))
    assert not fx.broker.posts


def test_ephemeral_execution_can_require_remote_durability(fx):
    engine = fx.restart(armed=True, require_remote_durability=True)
    with pytest.raises(LiveExecutionError, match="remote"): engine.submit(candidate(fx.clock))
    assert not fx.broker.posts


def test_lost_submit_response_is_recovered_without_a_second_post(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock lost acknowledgement")
    assert fx.engine.submit(candidate(fx.clock)) == "RECONCILING"
    assert load(fx.path)["pending"]["stage"] == "INTENT"
    engine = fx.restart(armed=False)
    assert engine.reconcile() == "WAITING_FOR_SETTLEMENT"
    assert len(fx.broker.posts) == 1
    fx.settle("yes")
    assert engine.reconcile() == "SCANNING"
    assert D(engine.state["realized_pnl"]) == D(".10")


def test_unlocated_uncertain_order_never_resubmits_even_after_close(fx):
    def uncertain(**kwargs):
        fx.broker.posts.append(kwargs)
        raise AmbiguousOrderError("mock")
    fx.broker.submit_limit_buy = uncertain
    fx.engine.submit(candidate(fx.clock))
    fx.clock.now = CLOSE + 600
    engine = fx.restart(armed=True)
    for _ in range(3): assert engine.tick(candidate(fx.clock)) == "RECONCILING"
    assert len(fx.broker.posts) == 1 and engine.state["pending"] is not None


def test_ack_persistence_failure_recovers_from_durable_intent(fx):
    def persistence(state):
        if state["pending"]["stage"] == "ACKNOWLEDGED": raise OSError("mock")
        return fx.persist(state)
    fx.engine.persist_state = persistence
    with pytest.raises(OSError): fx.engine.submit(candidate(fx.clock))
    assert load(fx.path)["pending"]["stage"] == "INTENT"
    assert fx.restart().reconcile() == "WAITING_FOR_SETTLEMENT"
    assert len(fx.broker.posts) == 1


def test_source_handoff_after_intent_prevents_submission(fx):
    def changed(state):
        return dict(fx.persist(state), restart_required=True, remote_source_sha="new", state_commit="saved")
    fx.engine.persist_state = changed
    assert fx.engine.submit(candidate(fx.clock)) == "SOURCE_UPDATE_HANDOFF"
    assert fx.engine.reconcile() == "SOURCE_UPDATE_HANDOFF"
    assert not fx.broker.posts


def test_source_handoff_after_ack_preserves_order_for_new_process(fx):
    def changed(state):
        receipt = fx.persist(state)
        if state["pending"]["stage"] == "ACKNOWLEDGED": receipt["restart_required"] = True
        return receipt
    fx.engine.persist_state = changed
    assert fx.engine.submit(candidate(fx.clock)) == "SOURCE_UPDATE_HANDOFF"
    assert load(fx.path)["pending"]["order_id"] == "order-1"
    assert fx.restart().reconcile() == "WAITING_FOR_SETTLEMENT"
    assert len(fx.broker.posts) == 1


def test_settlement_uses_actual_fill_costs_and_not_changes_in_account_cash(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("yes")
    fx.broker.cash = "119.8499"  # unrelated deposit must not become bot profit
    assert fx.engine.reconcile() == "SCANNING"
    assert fx.engine.state["account_cash"] == "119.8499"
    assert D(fx.engine.state["realized_pnl"]) == D(".10")
    assert fx.engine.reconcile() == "SCANNING"  # no duplicate settlement
    assert len(fx.engine.state["history"]) == 1
    assert fx.engine.submit(candidate(fx.clock)) == "BLOCKED_ALREADY_ATTEMPTED_ROUND"


def test_loss_persists_stop_across_restart_and_later_rounds(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("no")
    assert fx.engine.reconcile() == "STOPPED"
    engine = fx.restart(armed=True)
    assert engine.submit(candidate(fx.clock, round_key=round_key(CLOSE + 900), close_time=iso(CLOSE + 900))) == "STOPPED"
    assert len(fx.broker.posts) == 1
    assert D(engine.state["realized_pnl"]) == D("-.90")


@pytest.mark.parametrize("result,mode,pnl", [("no", "SCANNING", ".10"), ("yes", "STOPPED", "-.90")])
def test_no_side_account_settlement_uses_no_leg_costs(fx, result, mode, pnl):
    fx.engine.submit(candidate(fx.clock, "NO"))
    fx.settle(result)
    assert fx.engine.reconcile() == mode
    assert D(fx.engine.state["realized_pnl"]) == D(pnl)
    assert fx.engine.state["history"][0]["side"] == "NO"


def test_explicit_rejection_ends_attempt_and_does_not_spend_cash(fx):
    def rejected(**kwargs):
        fx.broker.posts.append(kwargs)
        raise OrderRejected(400)
    fx.broker.submit_limit_buy = rejected
    assert fx.engine.submit(candidate(fx.clock)) == "SCANNING"
    assert fx.engine.state["attempts"][0]["outcome"] == "REJECTED"
    assert not fx.engine.state["pending"] and not fx.engine.state["history"]
    assert fx.engine.submit(candidate(fx.clock)) == "BLOCKED_ALREADY_ATTEMPTED_ROUND"
    assert len(fx.broker.posts) == 1


def test_invalid_ack_does_not_trigger_a_second_submission(fx):
    original = fx.broker.submit_limit_buy
    def incomplete(**kwargs):
        original(**kwargs)
        return {"unexpected": "payload"}
    fx.broker.submit_limit_buy = incomplete
    assert fx.engine.submit(candidate(fx.clock)) == "RECONCILING"
    assert fx.restart().reconcile() == "WAITING_FOR_SETTLEMENT"
    assert len(fx.broker.posts) == 1


def test_state_identity_and_actual_execution_accounting_are_validated(fx):
    fx.engine.submit(candidate(fx.clock))
    original = load(fx.path)
    state = copy.deepcopy(original)
    state["attempted_rounds"].append(state["attempted_rounds"][0])
    with pytest.raises(LiveStateError): validate(state)
    state = copy.deepcopy(original)
    state["pending"]["execution"]["debit"] = ".91"
    with pytest.raises(LiveStateError): validate(state)
    state = copy.deepcopy(original)
    state["pending"]["client_order_id"] = "not-a-uuid"
    with pytest.raises(LiveStateError): validate(state)


@pytest.mark.parametrize("market_change,settlements", [
    ({"status": "closed", "result": "yes"}, True),
    ({"status": "settled", "result": "yes"}, False),
    ({"status": "settled", "result": ""}, True),
    ({"status": "active", "result": "no"}, True),
])
def test_unknown_or_delayed_official_result_stays_pending(fx, market_change, settlements):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("yes")
    fx.market.update(market_change)
    if not settlements: fx.broker.settled = []
    assert fx.engine.reconcile() == "WAITING_FOR_SETTLEMENT"
    assert not fx.engine.state["history"]


@pytest.mark.parametrize("field,value", [
    ("yes_count_fp", "2"), ("no_count_fp", "1"), ("yes_total_cost_dollars", ".88"),
    ("fee_cost", ".02"), ("market_result", "no"), ("revenue", 99), ("ticker", "FOREIGN"),
])
def test_unattributed_or_conflicting_settlement_halts_without_fabricating_pnl(fx, field, value):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("yes")
    fx.broker.settled[0][field] = value
    assert fx.engine.reconcile() == "HALTED"
    assert fx.engine.state["pending"] and not fx.engine.state["history"]
    assert fx.engine.state["realized_pnl"] == "0.00"


def test_void_needs_verified_actual_refund_and_does_not_double_refund(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("void", revenue=90)
    fx.market.update(status="voided", result="void")
    assert fx.engine.reconcile() == "SCANNING"
    trade = fx.engine.state["history"][0]
    assert trade["result"] == "VOID" and trade["won"] is None and D(trade["pnl"]) == 0
    assert D(trade["cash_payout"]) == D(".90")
    fx.engine.reconcile()
    assert len(fx.engine.state["history"]) == 1


def test_missing_verified_void_refund_keeps_exposure_pending(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.market.update(status="voided", result="void")
    assert fx.engine.reconcile() == "WAITING_FOR_SETTLEMENT"
    assert fx.engine.state["pending"]


def _partial(fx):
    fx.broker.order.update(fill_count_fp=".50", remaining_count_fp="0", status="canceled",
                          taker_fill_cost_dollars=".445", taker_fees_dollars=".005")
    fx.broker.executions[0].update(count_fp=".50", fee_cost=".005")


def test_partial_fill_is_tracked_and_settled_but_never_allows_another_entry(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    _partial(fx)
    assert fx.engine.reconcile() == "HALTED"
    assert D(fx.engine.state["pending"]["execution"]["quantity"]) == D(".50")
    fx.settle("yes", quantity=".50", fees=".005", revenue=50)
    assert fx.engine.reconcile() == "HALTED"
    assert D(fx.engine.state["realized_pnl"]) == D(".05")
    assert fx.engine.state["pending"] is None and len(fx.broker.posts) == 1


def test_unfilled_cancellation_consumes_attempt_without_counting_a_trade(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.order.update(fill_count_fp="0", remaining_count_fp="0", status="canceled",
                          taker_fill_cost_dollars="0", taker_fees_dollars="0")
    fx.broker.executions = []
    assert fx.engine.reconcile() == "SCANNING"
    assert fx.engine.state["attempts"][0]["outcome"] == "UNFILLED"
    assert not fx.engine.state["history"]
    assert fx.engine.submit(candidate(fx.clock)) == "BLOCKED_ALREADY_ATTEMPTED_ROUND"


def test_remaining_order_halts_even_when_no_fill_has_arrived(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.order.update(fill_count_fp="0", remaining_count_fp="1", status="resting",
                          taker_fill_cost_dollars="0", taker_fees_dollars="0")
    fx.broker.executions = []
    assert fx.engine.reconcile() == "HALTED"
    assert fx.engine.state["pending"] and len(fx.broker.posts) == 1


def test_lagging_fills_do_not_become_an_unfilled_order(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fills = copy.deepcopy(fx.broker.executions)
    fx.broker.executions = []
    assert fx.engine.reconcile() == "RECONCILING"
    fx.broker.executions = fills
    assert fx.engine.reconcile() == "WAITING_FOR_SETTLEMENT"


def test_duplicate_identical_fill_is_not_double_charged(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.executions *= 2
    assert fx.engine.reconcile() == "WAITING_FOR_SETTLEMENT"
    assert D(fx.engine.state["pending"]["execution"]["debit"]) == D(".90")


@pytest.mark.parametrize("change", ["wrong_order", "wrong_side", "wrong_subaccount", "duplicate_conflict", "wrong_cost", "late_fill"])
def test_fill_identity_or_strategy_discrepancies_block_new_entries(fx, change):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fill = fx.broker.executions[0]
    if change == "wrong_order": fill["order_id"] = "foreign"
    if change == "wrong_side": fill.update(outcome_side="no", book_side="ask")
    if change == "wrong_subaccount": fill["subaccount_number"] = 1
    if change == "duplicate_conflict": fx.broker.executions.append(dict(fill, fee_cost=".02"))
    if change == "wrong_cost": fx.broker.order["taker_fill_cost_dollars"] = ".88"
    if change == "late_fill": fill["created_time"] = iso(CLOSE + 1)
    assert fx.engine.reconcile() == "HALTED"
    assert fx.engine.state["pending"] and len(fx.broker.posts) == 1


def test_actual_over_budget_fill_is_preserved_for_reconciliation(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.executions[0]["fee_cost"] = ".20"
    fx.broker.order["taker_fees_dollars"] = ".20"
    assert fx.engine.reconcile() == "HALTED"
    assert D(load(fx.path)["pending"]["execution"]["debit"]) == D("1.09")


def test_api_failure_preserves_pending_journal_without_a_resubmission(fx):
    fx.engine.submit(candidate(fx.clock))
    original = fx.path.read_bytes()
    fx.broker.read_error = True
    fx.engine.reconcile()
    assert fx.path.read_bytes() == original and len(fx.broker.posts) == 1


def test_wrong_account_scope_is_rejected_before_any_io(fx):
    state = load(fx.path)
    state["account_scope"] = "production:0:other-key"
    with pytest.raises(LiveExecutionError, match="different account"):
        LiveExecution(fx.broker, state, persist_state=fx.persist, official_market=lambda _: {})
    assert not fx.broker.posts and not fx.broker.reads


def test_missing_or_corrupt_state_is_never_reinitialized(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(LiveStateError): load(path)
    assert not path.exists()
    path.write_text('{"schema": 1, "balance": "1000.00"}')
    before = path.read_bytes()
    with pytest.raises(LiveStateError): load(path)
    assert path.read_bytes() == before


def test_state_rejects_forged_profit_and_loss_reset(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("no")
    fx.engine.reconcile()
    state = load(fx.path)
    state["mode"] = "SCANNING"
    with pytest.raises(LiveStateError): validate(state)
    state = load(fx.path)
    state["realized_pnl"] = "100.00"
    with pytest.raises(LiveStateError): validate(state)


def test_settlement_persistence_failure_keeps_durable_position_until_recovery(fx):
    fx.engine.submit(candidate(fx.clock))
    fx.settle("yes")
    fx.engine.persist_state = lambda state: (_ for _ in ()).throw(OSError("mock"))
    with pytest.raises(OSError): fx.engine.reconcile()
    assert load(fx.path)["pending"] is not None
    recovered = fx.restart()
    assert recovered.reconcile() == "SCANNING"
    assert len(recovered.state["history"]) == 1 and len(fx.broker.posts) == 1


def test_fee_model_disagreement_is_rejected_before_any_durable_intent(fx):
    original = fx.path.read_bytes()
    with pytest.raises(CandidateInvalid, match="fee_model"):
        fx.engine.submit(candidate(fx.clock, fees=".02", debit=".91"))
    assert fx.path.read_bytes() == original
    assert not fx.broker.posts and not fx.broker.reads


@pytest.mark.parametrize("remove_metadata", [False, True])
def test_historical_partial_fill_cannot_resume_by_changing_saved_mode(fx, remove_metadata):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    _partial(fx)
    fx.engine.reconcile()
    fx.settle("yes", quantity=".50", fees=".005", revenue=50)
    assert fx.engine.reconcile() == "HALTED"
    state = load(fx.path)
    state["mode"] = "SCANNING"
    if remove_metadata:
        state["halt_reason"] = None
        state["history"][0]["risk_flags"] = []
    with pytest.raises(LiveStateError): validate(state)
    with pytest.raises(LiveStateError):
        LiveExecution(fx.broker, state, persist_state=fx.persist, official_market=lambda _: {}, armed=True)
    assert len(fx.broker.posts) == 1


def test_in_memory_mode_change_cannot_clear_an_execution_halt(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    _partial(fx)
    fx.engine.reconcile()
    fx.settle("yes", quantity=".50", fees=".005", revenue=50)
    fx.engine.reconcile()
    fx.engine.state["mode"] = "SCANNING"
    assert fx.engine.submit(candidate(fx.clock)) == "HALTED"
    assert len(fx.broker.posts) == 1


@pytest.mark.parametrize("field", ["fees", "price", "quantity", "filled_at"])
def test_previously_recorded_fill_is_immutable_across_api_reads(fx, field):
    fx.engine.submit(candidate(fx.clock))
    original = copy.deepcopy(fx.engine.state["pending"]["execution"])
    fill, order = fx.broker.executions[0], fx.broker.order
    if field == "fees":
        fill["fee_cost"] = ".02"
        order["taker_fees_dollars"] = ".02"
    elif field == "price":
        fill.update(yes_price_dollars=".88", no_price_dollars=".12")
        order["taker_fill_cost_dollars"] = ".88"
    elif field == "quantity":
        fill.update(count_fp=".50", fee_cost=".005")
        order.update(fill_count_fp=".50", status="canceled", taker_fill_cost_dollars=".445", taker_fees_dollars=".005")
    else:
        fill["created_time"] = iso(fx.clock() + 1)
    assert fx.engine.reconcile() == "HALTED"
    assert fx.engine.state["halt_reason"] == "previously_recorded_fill_changed"
    assert fx.engine.state["pending"]["execution"] == original


@pytest.mark.parametrize("field,value", [("price", ".50"), ("fees", ".02"), ("quantity", ".50"), ("filled_at", "bad")])
def test_saved_fill_details_must_match_saved_execution_aggregates(fx, field, value):
    fx.engine.submit(candidate(fx.clock))
    state = load(fx.path)
    state["pending"]["execution"]["fills"][0][field] = value
    with pytest.raises((LiveStateError, ValueError)): validate(state)


def test_backdated_fill_inside_last_minute_is_still_inconsistent_with_intent(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.executions[0]["created_time"] = iso(CLOSE - 59)
    assert fx.engine.reconcile() == "HALTED"
    assert "fill_precedes_order_intent_or_submission" in fx.engine.state["pending"]["risk_flags"]


def test_fill_latency_cannot_silently_use_a_reference_older_than_allowed(fx):
    original = fx.broker.submit_limit_buy
    def delayed(**kwargs):
        fx.clock.now += 4
        return original(**kwargs)
    fx.broker.submit_limit_buy = delayed
    old_reference = fx.clock() - 14
    assert fx.engine.submit(candidate(fx.clock, observation_time=old_reference)) == "HALTED"
    assert "fill_reference_stale" in fx.engine.state["pending"]["risk_flags"]


def test_future_fill_beyond_allowed_clock_skew_is_recorded_but_halted(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    fx.broker.executions[0]["created_time"] = iso(fx.clock() + 10)
    assert fx.engine.reconcile() == "HALTED"
    assert "fill_time_in_future" in fx.engine.state["pending"]["risk_flags"]


def test_settlement_cannot_precede_the_last_of_multiple_fills(fx):
    fx.broker.submit_error = AmbiguousOrderError("mock")
    fx.engine.submit(candidate(fx.clock))
    first = dict(fx.broker.executions[0], count_fp=".50", fee_cost=".005")
    second = dict(first, fill_id="fill-2", created_time=iso(fx.clock() + 2))
    fx.broker.executions = [first, second]
    fx.clock.now += 2
    assert fx.engine.reconcile() == "WAITING_FOR_SETTLEMENT"
    fx.settle("yes")
    fx.broker.settled[0]["settled_time"] = iso(CLOSE - 29)
    assert fx.engine.reconcile() == "HALTED"
    assert not fx.engine.state["history"] and fx.engine.state["pending"]
