"""Live-only stake sizing.

Paper trading keeps its existing shared budget. New live entries use a $5
fee-inclusive cap, except the next eligible live entry uses $10 after four
consecutive settled $5 wins. Any settled non-winning $5 trade resets the
streak, and any settled $10 trade resets it regardless of outcome.
"""
from decimal import Decimal

from payout_quote import as_decimal

BASE_LIVE_BUDGET = Decimal("5.00")
BOOST_LIVE_BUDGET = Decimal("10.00")
BOOST_AFTER_BASE_WINS = 4
MAX_LIVE_BUDGET = BOOST_LIVE_BUDGET
NEW_LIVE_BUDGETS = frozenset({BASE_LIVE_BUDGET, BOOST_LIVE_BUDGET})


def next_live_budget(history):
    if not isinstance(history, list):
        raise ValueError("Live history must be a list")
    consecutive = 0
    for trade in reversed(history):
        if not isinstance(trade, dict):
            raise ValueError("Live history entry must be an object")
        try:
            budget = as_decimal(trade.get("budget"))
        except (TypeError, ValueError):
            return BASE_LIVE_BUDGET
        if budget != BASE_LIVE_BUDGET:
            return BASE_LIVE_BUDGET
        if trade.get("won") is not True:
            return BASE_LIVE_BUDGET
        consecutive += 1
        if consecutive >= BOOST_AFTER_BASE_WINS:
            return BOOST_LIVE_BUDGET
    return BASE_LIVE_BUDGET
