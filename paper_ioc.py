"""Paper-only immediate-or-cancel execution model.

This module never sends orders.  It models IOC semantics using an observed
quantity available at the selected limit price: fill immediately up to that
quantity and cancel the remainder.
"""

from decimal import Decimal, ROUND_FLOOR

from payout_quote import as_decimal
from quote_adapter import QUANTITY_STEP, documented_taker_cost


def simulate_ioc(*, requested_quantity, available_quantity, price,
                 fee_multiplier, balance_precision, budget):
    requested = as_decimal(requested_quantity)
    available = as_decimal(available_quantity)
    limit_price = as_decimal(price)
    amount = as_decimal(budget)

    if requested <= 0 or requested % QUANTITY_STEP:
        raise ValueError("requested quantity must be a positive 0.01-contract increment")
    if available < 0:
        raise ValueError("available quantity cannot be negative")

    immediate = min(requested, available)
    steps = int((immediate / QUANTITY_STEP).to_integral_value(rounding=ROUND_FLOOR))
    filled = QUANTITY_STEP * steps

    if filled == 0:
        return {
            "time_in_force": "immediate_or_cancel",
            "requested_quantity": str(requested),
            "filled_quantity": "0.00",
            "cancelled_quantity": str(requested),
            "principal": "0",
            "fees": "0",
            "debit": "0",
            "payout": "0",
            "status": "UNFILLED",
            "resting_quantity": "0.00",
        }

    principal, fees, debit = documented_taker_cost(
        limit_price, filled, fee_multiplier, balance_precision
    )
    if debit > amount:
        raise ValueError("immediate IOC fill exceeds selected paper budget")

    return {
        "time_in_force": "immediate_or_cancel",
        "requested_quantity": str(requested),
        "filled_quantity": str(filled),
        "cancelled_quantity": str(requested - filled),
        "principal": str(principal),
        "fees": str(fees),
        "debit": str(debit),
        "payout": str(filled),
        "status": "FILLED" if filled == requested else "PARTIALLY_FILLED",
        "resting_quantity": "0.00",
    }
