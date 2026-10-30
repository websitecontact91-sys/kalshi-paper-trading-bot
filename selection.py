"""Select one eligible paper candidate by signed percentage distance."""

from dataclasses import dataclass
from decimal import Decimal

from payout_quote import PayoutQuote, as_decimal, ask_in_band, payout_eligible
from trade_config import TRADE_BUDGET


@dataclass(frozen=True)
class Candidate:
    ticker: str
    side: str
    ask: Decimal
    current: Decimal
    target: Decimal
    quote: PayoutQuote
    fresh: bool

    def __post_init__(self):
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("ticker is required")
        if self.side not in ("YES", "NO"):
            raise ValueError("side must be YES or NO")
        if not isinstance(self.fresh, bool):
            raise TypeError("fresh must be a boolean")
        if not isinstance(self.quote, PayoutQuote):
            raise TypeError("quote must be a PayoutQuote")
        for name in ("ask", "current", "target"):
            object.__setattr__(self, name, as_decimal(getattr(self, name)))
        if not Decimal("0") < self.ask < Decimal("1"):
            raise ValueError("ask must be between zero and one")
        if self.current <= 0 or self.target <= 0:
            raise ValueError("underlying and target must be positive")


def distance_pct(candidate):
    difference = (candidate.current - candidate.target if candidate.side == "YES"
                  else candidate.target - candidate.current)
    return Decimal("100") * difference / candidate.target


def rejection_reason(candidate):
    if not candidate.fresh:
        return "stale_or_closed"
    if candidate.quote.budget > TRADE_BUDGET or candidate.quote.debit > TRADE_BUDGET:
        return "trade_budget_exceeds_limit"
    if not ask_in_band(candidate.ask):
        return "ask_out_of_band"
    if not payout_eligible(candidate.quote):
        return "payout_out_of_band"
    return None


def eligible(candidate):
    return rejection_reason(candidate) is None


def rank(candidates):
    return sorted((c for c in candidates if eligible(c)), key=lambda c: (
        -distance_pct(c), -c.quote.exact_multiplier, c.ticker, c.side))


def select(candidates):
    ranked = rank(candidates)
    return ranked[0] if ranked else None
