"""Exact fee-inclusive paper accounting; display rounding never creates payout."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
MIN_MULT, MAX_MULT = Decimal("1.10"), Decimal("1.20")
MIN_ASK, MAX_ASK = Decimal("0.80"), Decimal("0.90")


def as_decimal(value):
    """Parse finite monetary inputs without silently importing binary floats."""
    if isinstance(value, (float, bool)):
        raise TypeError("floats and booleans are not allowed for money; pass str or Decimal")
    if not isinstance(value, (str, int, Decimal)):
        raise TypeError("money must be a string, integer, or Decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("decimal value must be finite")
    return result


_d = as_decimal


@dataclass(frozen=True)
class PayoutQuote:
    budget: Decimal
    debit: Decimal
    payout: Decimal

    def __post_init__(self):
        for name in ("budget", "debit", "payout"):
            object.__setattr__(self, name, as_decimal(getattr(self, name)))
        if not Decimal("0") < self.debit <= self.budget:
            raise ValueError("debit must be > 0 and <= budget")
        if self.payout <= 0:
            raise ValueError("payout must be positive")

    @classmethod
    def from_kalshi(cls, budget, debit, payout):
        """Accounting constructor; source verification belongs to quote_adapter."""
        return cls(as_decimal(budget), as_decimal(debit), as_decimal(payout))

    @property
    def exact_multiplier(self):
        return self.payout / self.debit

    @property
    def displayed_multiplier(self):
        # The owner's specified display rule. UI parity is separately evidenced.
        return self.exact_multiplier.quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def unused_cash(self):
        return self.budget - self.debit

    def pnl(self, won):
        if not isinstance(won, bool):
            raise TypeError("won must be a boolean")
        return self.payout - self.debit if won else -self.debit


def ask_in_band(ask):
    return MIN_ASK <= as_decimal(ask) <= MAX_ASK


def payout_eligible(quote):
    return MIN_MULT <= quote.displayed_multiplier <= MAX_MULT
