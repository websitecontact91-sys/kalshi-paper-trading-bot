from decimal import Decimal, InvalidOperation

MIN_STRIKE_CUSHION_PCT = Decimal("0.012")


class StrikeCushionError(ValueError):
    pass


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise StrikeCushionError(f"invalid {name}") from exc
    if not result.is_finite():
        raise StrikeCushionError(f"invalid {name}")
    return result


def strike_cushion_pct(current_underlying, strike):
    """Return absolute strike distance as a percentage of the strike.

    This module is a review/test reference implementation only. It is not
    connected to the live execution path.
    """
    current = _decimal(current_underlying, "current_underlying")
    target = _decimal(strike, "strike")
    if target == 0:
        raise StrikeCushionError("strike must be nonzero")
    return (abs(current - target) / abs(target)) * Decimal("100")


def meets_min_strike_cushion(current_underlying, strike,
                             minimum_pct=MIN_STRIKE_CUSHION_PCT):
    minimum = _decimal(minimum_pct, "minimum_pct")
    if minimum < 0:
        raise StrikeCushionError("minimum_pct must be nonnegative")
    return strike_cushion_pct(current_underlying, strike) >= minimum
