import copy

import pytest

import state_store as store


def opened():
    state = store.new_state()
    store.record_entry(state, "1992550", {"ticker": "KXSOL15M-TEST", "side": "NO",
                                         "budget": "1.00", "debit": "1.00", "payout": "1.13"})
    return state


def test_win_credits_exact_payout_and_allows_only_next_round(tmp_path):
    state = opened()
    assert store.record_settlement(state, "no", settled_at=1793295001) is True
    assert state["balance"] == "1000.13"
    assert state["history"][0]["pnl"] == "0.13"
    assert not store.can_enter(state, "1992550")
    assert store.can_enter(state, "1992551")
    path = tmp_path / "state.json"
    store.save(path, state)
    assert store.load(path)["balance"] == "1000.13"


def test_loss_persists_scanning_across_restart_and_next_trade_is_allowed(tmp_path):
    state = opened()
    assert store.record_settlement(state, "yes") is False
    assert state["balance"] == "999.00"
    assert state["history"][0]["pnl"] == "-1.00"
    path = tmp_path / "state.json"
    store.save(path, state)
    restored = store.load(path)
    assert restored["mode"] == "SCANNING"
    assert store.can_enter(restored, "1992551")
    assert not store.can_enter(restored, "1992550")
    store.record_entry(restored, "1992551", {"ticker": "KXSOL15M-NEXT", "side": "NO",
        "budget": "1.00", "debit": "1.00", "payout": "1.13"})
    store.record_settlement(restored, "yes")
    assert restored["balance"] == "998.00"
    assert restored["mode"] == "SCANNING"
    assert len(restored["history"]) == 2


@pytest.mark.parametrize("result", [None, "", "pending", "closed", "void", "cancelled", "scalar"])
def test_unknown_or_delayed_result_cannot_become_a_loss(result):
    state = opened()
    before = copy.deepcopy(state)
    assert store.record_settlement(state, result) is None
    assert state == before


def test_verified_void_refunds_once_and_keeps_round_used():
    state = opened()
    with pytest.raises(store.StateError, match="verified"):
        store.record_void(state)
    store.record_void(state, verified=True)
    assert state["balance"] == "1000.00"
    assert state["history"][0]["result"] == "VOID"
    assert state["history"][0]["pnl"] == "0.00"
    assert state["mode"] == "SCANNING"
    assert not store.can_enter(state, "1992550")
    with pytest.raises(store.StateError, match="No pending"):
        store.record_void(state, verified=True)
    assert state["balance"] == "1000.00"


def test_duplicate_settlement_cannot_double_credit():
    state = opened()
    store.record_settlement(state, "no")
    with pytest.raises(store.StateError, match="No pending"):
        store.record_settlement(state, "no")
    assert state["balance"] == "1000.13"


@pytest.mark.parametrize("initial,remaining,mode", [("1.00", "0.00", "STOPPED"),
                                                  ("1.9999", "0.9999", "STOPPED"),
                                                  ("2.00", "1.00", "SCANNING")])
def test_loss_stops_only_when_remaining_cash_is_strictly_below_one(initial, remaining, mode):
    state = store.new_state(initial)
    store.record_entry(state, "1992550", {"ticker": "KXSOL15M-TEST", "side": "YES",
        "budget": "1.00", "debit": "1.00", "payout": "1.13"})
    assert state["mode"] == "WAITING_FOR_SETTLEMENT"
    store.record_settlement(state, "no")
    assert state["mode"] == mode
    assert state["balance"] == remaining
    assert state["stop_reason"] == ("insufficient_funds" if mode == "STOPPED" else None)


@pytest.mark.parametrize("result,expected", [("yes", "0.85"), ("VOID", "0.8")])
def test_existing_small_pending_position_finishes_before_insufficient_funds_stop(result, expected):
    # A historically saved small position can exist below today's $1 entry
    # minimum. It must still settle/refund exactly, including a win or void.
    legacy = {"version": 1, "mode": "WAITING_FOR_SETTLEMENT", "balance": 0.4,
        "history": [], "trade": {"ticker": "KXSOL15M-OLD", "side": "YES", "cost": 0.4,
        "count": 0.45, "fee_reserve": 0.01, "round_id": "1992550"}}
    state = store.migrate(legacy)
    assert state["mode"] == "WAITING_FOR_SETTLEMENT"
    if result == "VOID":
        store.record_void(state, verified=True)
    else:
        store.record_settlement(state, result)
    assert state["balance"] == expected
    assert state["mode"] == "STOPPED"
    assert state["stop_reason"] == "insufficient_funds"
    assert state["history"][0]["won"] is (None if result == "VOID" else True)
