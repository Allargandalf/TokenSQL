"""Deterministic two-decimal payment-ratio policy shared by all write paths."""
from __future__ import annotations

from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext
from typing import Any


DISCOUNT_POLICY_VERSION = "discount-rate-fixed-2-v1"
DISCOUNT_QUANTUM = Decimal("0.01")


def normalize_discount_rate(value: Any) -> Decimal | None:
    """Return a nonnegative fixed-2 Decimal; never round invalid negatives to zero.

    Eight-tenths payable (八折) is 0.80, and 1.5折 is 0.15. Values are
    parsed without binary floats; amount, FX and price precision are unrelated.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("discount_rate must use a Decimal string, not a binary float or boolean")
    try:
        number = Decimal(value)
    except (DecimalException, ValueError):
        raise ValueError("discount_rate must be a finite nonnegative Decimal") from None
    if not number.is_finite() or number < 0:
        raise ValueError("discount_rate must be a finite nonnegative Decimal")
    try:
        # quantize's default precision (28) is insufficient for large exact input.
        # adjusted()+3 includes every integer digit and both decimal places.
        precision = max(28, len(number.as_tuple().digits), number.adjusted() + 3)
        # Do not inherit a caller\'s rounding traps or restricted exponent range.
        with localcontext(Context(prec=precision, rounding=ROUND_HALF_UP)):
            rounded = number.quantize(DISCOUNT_QUANTUM, rounding=ROUND_HALF_UP)
    except (DecimalException, ValueError, OverflowError):
        raise ValueError("discount_rate cannot be represented at two decimal places") from None
    return Decimal("0.00") if rounded.is_zero() else rounded
