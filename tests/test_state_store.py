import copy
import json
from decimal import Decimal

import pytest

import state_store as store


def trade(**changes):
    item = {"ticker": "KXSOL15M-TEST", "side": "YES", "budget": "5.00",
            "debit": "5.00", "payout": "5.69", "fees": "0.03",
            "quantity": "5.69", "entered_at": 1793294966.0}
    item.update(changes)
    return item


def test_duplicate_entry_and_saved_pending_are_refused(tmp_path):
    state = store.new_state()
    store.record_entry(state, "1992550", trade())
    with pytest.raises(store.StateError, match="refused"):
        store.record_entry(state, "1992550", trade())
    path = tmp_path / "state.json"
    store.save(path, state)
    loaded = store.load(path)
    assert loaded["pending"]["payout"] == "5.69"
    assert loaded["balance"] == "995.00"
    assert not store.can_enter(loaded, "1992551")
    assert loaded["pending"]["round_key"] in loaded["rounds_traded"]


def test_round_canonicalization_preserves_legacy_and_timezone():
    assert store.canonical_round_key("2026-10-29T17:30:00Z") == "1992550"
    assert store.canonical_round_key("2026-10-29T19:30:00+02:00") == "1992550"
    assert store.canonical_round_key("1992550") == "1992550"
    with pytest.raises(store.StateError):
        store.canonical_round_key("2026-10-29T17:30:00")


def test_missing_or_corrupt_file_never_resets(tmp_path):
    path = tmp_path / "state.json"
    with pytest.raises(store.StateError, match="Missing"):
        store.load(path)
    path.write_text('{"mode": broken')
    before = path.read_bytes()
    with pytest.raises(store.StateError, match="Unreadable"):
        store.load(path)
    assert path.read_bytes() == before


def test_crash_before_save_discards_only_uncommitted_draft(tmp_path):
    path = tmp_path / "state.json"
    state = store.new_state()
    store.save(path, state)
    uncommitted = copy.deepcopy(state)
    store.record_entry(uncommitted, "1992550", trade())
    # The unsaved copy was never an acknowledged paper entry.
    after_restart = store.load(path)
    assert after_restart["balance"] == "1000.00"
    assert after_restart["pending"] is None
    store.record_entry(after_restart, "1992550", trade())
    store.save(path, after_restart)
    restored = store.load(path)
    assert restored["balance"] == "995.00"
    assert restored["rounds_traded"] == ["1992550"]


def test_crash_after_json_save_can_rebuild_csv(tmp_path):
    state = store.new_state()
    store.record_entry(state, "1992550", trade())
    path = tmp_path / "state.json"
    store.save(path, state)
    loaded = store.load(path)
    store.write_ledger(tmp_path / "ledger.csv", loaded)
    assert not store.can_enter(loaded, "1992550")
    assert (tmp_path / "ledger.csv").read_text().count("KXSOL15M-TEST") == 1


def legacy_state(won=True):
    return {"version": 1, "mode": "SCANNING" if won else "STOPPED",
            "balance": 995.71 if won else 995.2, "trade": None,
            "history": [{"ticker": "KX10YRRATE15M-TEST" if won else "KXBRENT15M-TEST",
                         "side": "NO", "cost": 5.0 if won else 4.8,
                         "count": 5.51 if won else 5, "fee_reserve": 0.04 if won else 0.5,
                         "round_id": "1992550", "close_time": "2026-10-29T17:30:00Z",
                         "result": "no" if won else "yes", "won": won,
                         "pnl": 0.51 if won else -4.8}]}


def test_current_legacy_win_migrates_without_resetting_balance_or_history(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(legacy_state()))
    original = path.read_bytes()
    state = store.load(path)
    assert path.read_bytes() == original
    assert state["balance"] == "995.71"
    assert state["initial_balance"] == "995.20"
    assert state["history"][0]["payout"] == "5.51"
    assert state["history"][0]["cash_payout"] == "5.51"
    assert state["mode"] == "SCANNING"
    assert state["rounds_traded"] == ["1992550"]


def test_archived_legacy_loss_stays_stopped_and_is_not_overwritten(tmp_path):
    path = tmp_path / "archived.json"
    path.write_text(json.dumps(legacy_state(False)))
    before = path.read_bytes()
    state = store.load(path)
    assert state["balance"] == "995.2"
    assert state["mode"] == "STOPPED"
    assert not store.can_enter(state, "1992551")
    assert path.read_bytes() == before
    bad = legacy_state(False)
    bad["mode"] = "SCANNING"
    with pytest.raises(store.StateError, match="STOPPED"):
        store.migrate(bad)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, 1000.0])
def test_invalid_money_rejected(value):
    with pytest.raises(store.StateError):
        store.new_state(value)


def test_invalid_entry_does_not_partially_mutate_state():
    state = store.new_state()
    original = copy.deepcopy(state)
    for invalid in (trade(debit="5.01"), trade(side="MAYBE"), trade(payout="NaN"),
                    trade(budget="6.00"), trade(debit=5.0), trade(unused_cash="1")):
        with pytest.raises(store.StateError):
            store.record_entry(state, "1992550", invalid)
        assert state == original


def test_balance_round_and_pending_corruption_is_rejected():
    state = store.new_state()
    store.record_entry(state, "1992550", trade())
    for key, value in (("rounds_traded", []), ("balance", "999.99"), ("mode", "SCANNING")):
        corrupted = copy.deepcopy(state)
        corrupted[key] = value
        with pytest.raises(store.StateError):
            store.validate(corrupted)


def test_decimal_audit_metadata_serializes_without_float_conversion(tmp_path):
    state = store.new_state()
    store.record_entry(state, "1992550", trade(current=Decimal("102.088500001"),
                                               target=Decimal("102.0885")))
    path = tmp_path / "state.json"
    store.save(path, state)
    loaded = store.load(path)
    assert loaded["pending"]["current"] == "102.088500001"
    assert loaded["pending"]["target"] == "102.0885"


def test_explicit_reset_archives_exact_bytes_and_requires_confirmation(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(legacy_state(False), indent=4))
    before = path.read_bytes()
    with pytest.raises(store.StateError, match="confirmation"):
        store.archive_and_reset(path)
    assert path.read_bytes() == before
    fresh, archive = store.archive_and_reset(path, confirm=True, timestamp="20261029T180000Z")
    assert archive.read_bytes() == before
    assert fresh["balance"] == "1000.00"
    assert store.load(path)["history"] == []


def test_reset_cannot_discard_unsettled_position(tmp_path):
    state = store.new_state()
    store.record_entry(state, "1992550", trade())
    path = tmp_path / "state.json"
    store.save(path, state)
    with pytest.raises(store.StateError, match="unsettled"):
        store.archive_and_reset(path, confirm=True)
