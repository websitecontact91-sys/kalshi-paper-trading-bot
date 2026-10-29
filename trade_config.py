"""Shared execution budget for new paper and live trades.

The cap includes the purchase principal and all entry fees. Existing ledger
entries retain their original budget and amounts when this setting changes.
"""

from decimal import Decimal

TRADE_BUDGET = Decimal("1.00")
