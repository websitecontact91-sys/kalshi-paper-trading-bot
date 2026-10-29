import copy
import json
from decimal import Decimal

import pytest

import state_store as store


def trade(**changes):
    item = {"ticker": "KXSOL15M-TEST", "side": "YES", "budget": "1.00",
            "debit": "1.00", "payout": "1.13", "fees": "0.03",
            "quantity": "1.13", "entered_at": 1793294966.0}
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
    assert loaded["pending"]["payout"] == "1.13"
    assert loaded["balance"] == "999.00"
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
    assert restored["balance"] == "999.00"
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


def test_legacy_loss_migrates_to_funded_scanning_without_overwriting_file(tmp_path):
    path = tmp_path / "archived.json"
    path.write_text(json.dumps(legacy_state(False)))
    before = path.read_bytes()
    state = store.load(path)
    assert state["balance"] == "995.2"
    assert state["mode"] == "SCANNING"
    assert state["stop_policy"] == store.STOP_POLICY
    assert state["stop_reason"] is None
    assert store.can_enter(state, "1992551")
    assert path.read_bytes() == before
    bad = legacy_state(False)
    bad["mode"] = "SCANNING"
    assert store.migrate(bad)["mode"] == "SCANNING"


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


@pytest.mark.parametrize("budget,debit", [("5.00", "5.00"), ("5.00", "0.99"),
                                          ("1.01", "1.01")])
def test_new_entries_over_one_dollar_budget_are_refused_without_mutation(budget, debit):
    state = store.new_state()
    before = copy.deepcopy(state)
    with pytest.raises(store.StateError, match="fee-inclusive"):
        store.record_entry(state, "1992550", trade(budget=budget, debit=debit))
    assert state == before


@pytest.mark.parametrize("result,expected,mode", [("yes", "1000.69", "SCANNING"),
                                                ("no", "995.00", "SCANNING")])
def test_old_five_dollar_pending_trade_is_preserved_and_settled_after_budget_change(tmp_path, result, expected, mode):
    # This is a saved pre-change position, not a newly permitted $5 entry.
    old = {"version": 1, "mode": "WAITING_FOR_SETTLEMENT", "balance": 995.0,
           "history": [], "trade": {"ticker": "KXSOL15M-OLD", "side": "YES",
           "cost": 5.0, "count": 5.69, "fee_reserve": 0.0497,
           "round_id": "1992550", "close_time": "2026-10-29T17:30:00Z"}}
    path = tmp_path / "old-state.json"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    state = store.load(path)
    assert path.read_bytes() == before
    assert Decimal(state["pending"]["budget"]) == Decimal("5.00")
    assert Decimal(state["pending"]["debit"]) == Decimal("5.00")
    assert Decimal(state["pending"]["payout"]) == Decimal("5.69")
    store.save(path, state)
    restarted = store.load(path)
    store.record_settlement(restarted, result)
    assert Decimal(restarted["balance"]) == Decimal(expected)
    assert restarted["mode"] == mode
    assert Decimal(restarted["history"][0]["debit"]) == Decimal("5.00")
    if mode == "SCANNING":
        store.record_entry(restarted, "1992551", trade())
        assert restarted["pending"]["budget"] == "1.00"
        assert len(restarted["history"]) == 1
    else:
        assert not store.can_enter(restarted, "1992551")


@pytest.mark.parametrize("balance,mode,allowed", [("0.9999", "STOPPED", False),
                                                 ("1.00", "SCANNING", True),
                                                 ("1.0001", "SCANNING", True),
                                                 ("0", "STOPPED", False)])
def test_available_cash_minimum_is_exact_and_includes_fees(balance, mode, allowed):
    state = store.new_state(balance)
    assert state["mode"] == mode
    assert store.can_enter(state, "1992550") is allowed
    if allowed:
        store.record_entry(state, "1992550", trade())
        assert state["mode"] == "WAITING_FOR_SETTLEMENT"
    else:
        before = copy.deepcopy(state)
        with pytest.raises(store.StateError, match="refused"):
            store.record_entry(state, "1992550", trade(debit="0.50"))
        assert state == before


def test_current_schema_loss_migration_preserves_history_and_all_financial_values(tmp_path):
    state = store.new_state("3.00", session_id="keep-session")
    store.record_entry(state, "1992550", trade())
    store.record_settlement(state, "no", settled_at=123)
    # Exact saved shape of the former policy: the current mode and historical
    # state_after recorded a stop. Only the former is allowed to change.
    state.pop("stop_policy")
    state.pop("stop_reason")
    state["mode"] = "STOPPED"
    state["history"][-1]["state_after"] = "STOPPED"
    state["round_reports"] = [{"mode": "STOPPED", "audit": "keep-exact"}]
    before = copy.deepcopy(state)
    path = tmp_path / "old-current-schema.json"
    path.write_text(json.dumps(state))
    original_bytes = path.read_bytes()
    migrated = store.load(path)
    assert state == before
    assert path.read_bytes() == original_bytes
    assert migrated["mode"] == "SCANNING"
    for key in ("session_id", "initial_balance", "balance", "history", "rounds_traded", "round_reports"):
        assert migrated[key] == before[key]
    assert store.apply_balance_policy(migrated) == migrated
    store.save(path, migrated)
    restored = store.load(path)
    store.record_entry(restored, "1992551", trade())
    store.record_settlement(restored, "yes")
    assert len(restored["history"]) == 2
    assert restored["history"][0]["state_after"] == "STOPPED"
    assert restored["balance"] == "2.13"


@pytest.mark.parametrize("change", [
    {"balance": "2.01"}, {"rounds_traded": []}, {"stop_policy": "unknown_policy"},
    {"stop_reason": "manual_unreviewed_halt"},
])
def test_policy_migration_does_not_repair_or_bypass_invalid_state(change):
    state = store.new_state("3.00")
    store.record_entry(state, "1992550", trade())
    store.record_settlement(state, "no")
    state.pop("stop_policy")
    state.pop("stop_reason")
    state["mode"] = "STOPPED"
    state.update(change)
    before = copy.deepcopy(state)
    with pytest.raises(store.StateError):
        store.apply_balance_policy(state)
    assert state == before


def test_stale_low_balance_legacy_scanning_migrates_to_insufficient_funds_stop():
    state = store.new_state("0.9999")
    state.pop("stop_policy")
    state.pop("stop_reason")
    state["mode"] = "SCANNING"
    assert not store.can_enter(state, "1992550")
    migrated = store.apply_balance_policy(state)
    assert migrated["mode"] == "STOPPED"
    assert migrated["stop_reason"] == "insufficient_funds"


def test_current_policy_cannot_claim_funded_scanning_below_minimum():
    state = store.new_state("0.9999")
    state.update(mode="SCANNING", stop_reason=None)
    with pytest.raises(store.StateError, match="available funds"):
        store.validate(state)


def test_current_schema_cannot_silently_discard_non_null_legacy_trade_alias():
    state = store.new_state()
    state["trade"] = trade(round_key="1992550")
    before = copy.deepcopy(state)
    with pytest.raises(store.StateError, match="trade alias"):
        store.validate(state)
    with pytest.raises(store.StateError, match="trade alias"):
        store.migrate(state)
    assert state == before


def test_legacy_schema_cannot_silently_discard_non_null_current_pending_alias():
    state = legacy_state()
    state["pending"] = trade(round_key="1992551")
    before = copy.deepcopy(state)
    with pytest.raises(store.StateError, match="pending alias"):
        store.migrate(state)
    assert state == before


@pytest.mark.parametrize("schema", ["current", "legacy"])
def test_identical_position_alias_is_accepted_and_normalized_for_future_settlement(schema):
    if schema == "current":
        state = store.new_state()
        store.record_entry(state, "1992550", trade())
        state["trade"] = copy.deepcopy(state["pending"])
        store.validate(state)
    else:
        position = {"ticker": "KXSOL15M-OLD", "side": "YES", "cost": 1.0,
                    "count": 1.13, "fee_reserve": 0.03, "round_id": "1992550"}
        state = {"version": 1, "mode": "WAITING_FOR_SETTLEMENT", "balance": 999.0,
                 "history": [], "trade": position, "pending": copy.deepcopy(position)}
    migrated = store.migrate(state)
    assert "trade" not in migrated
    assert migrated["pending"] is not None
    assert migrated["mode"] == "WAITING_FOR_SETTLEMENT"
    store.record_settlement(migrated, "yes")
    assert migrated["pending"] is None
    assert Decimal(migrated["balance"]) == Decimal("1000.13")
