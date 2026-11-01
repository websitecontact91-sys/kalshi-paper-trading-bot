from decimal import Decimal as D

from live_stakes import BASE_LIVE_BUDGET, BOOST_LIVE_BUDGET, next_live_budget


def T(budget, won):
    return {"budget": str(D(budget)), "won": won}


def test_base_budget_is_five_dollars_without_new_streak():
    assert next_live_budget([]) == BASE_LIVE_BUDGET == D("5.00")
    assert next_live_budget([T("1.00", True)]) == D("5.00")
    assert next_live_budget([T("5.00", True)] * 3) == D("5.00")


def test_four_consecutive_settled_five_dollar_wins_trigger_one_ten_dollar_trade():
    assert next_live_budget([T("5.00", True)] * 4) == BOOST_LIVE_BUDGET == D("10.00")


def test_any_five_dollar_loss_breaks_the_streak():
    history = [T("5.00", True), T("5.00", True), T("5.00", True), T("5.00", False)]
    assert next_live_budget(history) == D("5.00")


def test_ten_dollar_trade_resets_streak_regardless_of_result():
    four = [T("5.00", True)] * 4
    assert next_live_budget(four + [T("10.00", True)]) == D("5.00")
    assert next_live_budget(four + [T("10.00", False)]) == D("5.00")


def test_new_four_win_streak_is_required_after_ten_dollar_trade():
    history = [T("5.00", True)] * 4 + [T("10.00", False)] + [T("5.00", True)] * 4
    assert next_live_budget(history) == D("10.00")


def test_old_one_dollar_history_never_counts_toward_boost():
    history = [T("1.00", True)] * 20 + [T("5.00", True)] * 3
    assert next_live_budget(history) == D("5.00")


def test_partial_ioc_fill_keeps_selected_tier_for_streak_logic():
    partial_win = {"budget": "5.00", "won": True, "time_in_force": "immediate_or_cancel",
                   "quantity": "5.00", "execution": {"quantity": "2.00"}}
    history = [dict(partial_win) for _ in range(4)]
    assert next_live_budget(history) == BOOST_LIVE_BUDGET == D("10.00")

    boosted_partial = {"budget": "10.00", "won": True, "time_in_force": "immediate_or_cancel",
                       "quantity": "10.00", "execution": {"quantity": "1.00"}}
    assert next_live_budget(history + [boosted_partial]) == BASE_LIVE_BUDGET == D("5.00")
