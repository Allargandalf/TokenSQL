"""Pure deterministic Decimal pricing. No network, FX tables, or model arithmetic."""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any
import re
import unicodedata

PRECISION = 60
TOLERANCE = Decimal("1e-40")
UNIT_ALIASES = {
    "tokens": "token", "个token": "token", "令牌": "token",
    "seconds": "second", "s": "second", "sec": "second", "秒": "second",
    "images": "image", "张": "image", "requests": "request", "次": "request",
}


def identity_key(value: str) -> str:
    """Normalize presentation separators only; no fuzzy entity matching."""
    return re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", value).casefold())


def canonical_unit(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized:
        raise ValueError("unit must not be blank")
    return UNIT_ALIASES.get(normalized, normalized)


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{name} must use an exact Decimal string")
    result = Decimal(value)
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def decimal_equal(left: Decimal, right: Decimal) -> bool:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return abs(left - right) <= TOLERANCE * max(Decimal(1), abs(left), abs(right))


def convert_amount(original_amount: Any, exchange_rate: Any) -> Decimal:
    """Rate is explicit CNY per one original-currency unit, never looked up."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return _decimal(original_amount, "original_amount") * _decimal(
            exchange_rate, "exchange_rate", positive=True
        )


def scaled_amount(amount: Any, from_quantity: Any, to_quantity: Any) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return (_decimal(amount, "amount") * _decimal(to_quantity, "to_quantity", positive=True)
                / _decimal(from_quantity, "from_quantity", positive=True))


def verify_conversion(value: dict[str, Any]) -> None:
    evidence = value.get("conversion")
    if evidence:
        if value.get("amount") is None:
            raise ValueError("conversion evidence requires a CNY amount")
        converted = convert_amount(evidence["original_amount"], evidence["exchange_rate"])
        if not decimal_equal(converted, _decimal(value["amount"], "amount")):
            raise ValueError("CNY amount disagrees with original_amount * explicit exchange_rate")
