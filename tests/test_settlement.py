import copy

import pytest

import state_store as store


def opened():
    state = store.new_state()
    store.record_entry(state, "1992550", {"ticker": "KXSOL15M-TEST", "side": "NO",
                                         "budget": "5.00", "debit": "5.00", "payout": "5.69"})
    return state


def test_win_credits_exact_payout_and_allows_only_next_round(tmp_path):
    state = opened()
    assert store.record_settlement(state, "no", settled_at=1793295001) is True
    assert state["balance"] == "1000.69"
    assert state["history"][0]["pnl"] == "0.69"
    assert not store.can_enter(state, "1992550")
    assert store.can_enter(state, "1992551")
    path = tmp_path / "state.json"
    store.save(path, state)
    assert store.load(path)["balance"] == "1000.69"


def test_loss_persists_stop_across_restart(tmp_path):
    state = opened()
    assert store.record_settlement(state, "yes") is False
    assert state["balance"] == "995.00"
    assert state["history"][0]["pnl"] == "-5.00"
    path = tmp_path / "state.json"
    store.save(path, state)
    assert store.load(path)["mode"] == "STOPPED"
    assert not store.can_enter(store.load(path), "1992551")


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
    assert state["balance"] == "1000.69"
