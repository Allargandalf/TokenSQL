"""Pure, auditable price normalization.

No database objects are used here. Callers can persist the immutable results
after review without allowing a parser or a network source to choose DB keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


COMPARABLE_STATUSES = frozenset({"verified", "approved"})
_ONE = Decimal("1")
_MILLION = Decimal("1000000")


class NormalizationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    APPROVED = "approved"
    REJECTED = "rejected"


class PriceKind(str, Enum):
    MONETARY = "monetary"
    DISCOUNT = "discount"


@dataclass(frozen=True)
class FXQuote:
    """A dated, attributable exchange rate: one base buys rate quote."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_date: date | None
    source: str | None
    verification_status: str = NormalizationStatus.PENDING.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_currency", _currency(self.base_currency))
        object.__setattr__(self, "quote_currency", _currency(self.quote_currency))
        object.__setattr__(self, "rate", _decimal(self.rate, "FX rate"))
        if self.rate <= 0:
            raise ValueError("FX rate must be positive")


@dataclass(frozen=True)
class PriceQuote:
    """Raw monetary evidence. Raw values are never overwritten."""

    amount: Decimal
    currency: str
    unit_name: str
    unit_quantity: Decimal
    metric: str
    direction: str | None = None
    conditions: Mapping[str, Any] = field(default_factory=dict)
    verification_status: str = NormalizationStatus.PENDING.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _decimal(self.amount, "amount"))
        object.__setattr__(
            self,
            "unit_quantity",
            _decimal(self.unit_quantity, "unit_quantity"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "unit_name", str(self.unit_name).strip())
        object.__setattr__(self, "metric", _name(self.metric, "metric"))
        direction = None if self.direction is None else _name(self.direction, "direction")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "conditions", _freeze_mapping(self.conditions))
        object.__setattr__(self, "verification_status", _status(self.verification_status))
        if self.amount < 0:
            raise ValueError("amount must be non-negative")
        if self.unit_quantity <= 0:
            raise ValueError("unit_quantity must be positive")


@dataclass(frozen=True)
class DiscountQuote:
    """A multiplier: 0.8 means pay 80 percent of the verified base."""

    multiplier: Decimal
    metric: str
    direction: str | None = None
    conditions: Mapping[str, Any] = field(default_factory=dict)
    verification_status: str = NormalizationStatus.PENDING.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "multiplier", _decimal(self.multiplier, "multiplier"))
        object.__setattr__(self, "metric", _name(self.metric, "metric"))
        direction = None if self.direction is None else _name(self.direction, "direction")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "conditions", _freeze_mapping(self.conditions))
        object.__setattr__(self, "verification_status", _status(self.verification_status))
        if not Decimal("0") <= self.multiplier <= Decimal("1"):
            raise ValueError("discount multiplier must be between 0 and 1")


@dataclass(frozen=True)
class NormalizedPrice:
    kind: PriceKind
    raw_amount: Decimal
    raw_currency: str | None
    raw_unit_name: str | None
    raw_unit_quantity: Decimal | None
    metric: str
    direction: str | None
    conditions: Mapping[str, Any]
    normalized_amount: Decimal | None
    normalized_currency: str | None
    normalized_unit_name: str | None
    normalized_unit_quantity: Decimal | None
    comparison_key: str | None
    verification_status: str
    formula: str | None = None
    fx: FXQuote | None = None
    pending_reasons: tuple[str, ...] = ()

    @property
    def is_comparable(self) -> bool:
        return (
            self.verification_status in COMPARABLE_STATUSES
            and self.comparison_key is not None
            and self.normalized_amount is not None
        )


_UNIT_ALIASES: dict[str, tuple[str, str, Decimal]] = {
    "token": ("token", "token", _MILLION),
    "tokens": ("token", "token", _MILLION),
    "tok": ("token", "token", _MILLION),
    "second": ("time", "second", _ONE),
    "seconds": ("time", "second", _ONE),
    "sec": ("time", "second", _ONE),
    "s": ("time", "second", _ONE),
    "request": ("request", "request", _ONE),
    "requests": ("request", "request", _ONE),
    "call": ("request", "request", _ONE),
    "image": ("image", "image", _ONE),
    "images": ("image", "image", _ONE),
    "picture": ("image", "image", _ONE),
    "character": ("character", "character", _ONE),
    "characters": ("character", "character", _ONE),
    "char": ("character", "character", _ONE),
    "voice": ("voice", "voice", _ONE),
    "voices": ("voice", "voice", _ONE),
    "voice_tone": ("voice", "voice", _ONE),
    "音色": ("voice", "voice", _ONE),
    "字符": ("character", "character", _ONE),
    "次": ("request", "request", _ONE),
    "张": ("image", "image", _ONE),
    "秒": ("time", "second", _ONE),
}


def normalize_price(quote: PriceQuote, fx: FXQuote | None = None) -> NormalizedPrice:
    """Normalize a monetary quote without guessing missing evidence."""

    unit = _UNIT_ALIASES.get(_unit_key(quote.unit_name))
    if unit is None:
        return _pending_price(quote, f"unsupported unit: {quote.unit_name!r}")

    _, target_unit, target_quantity = unit
    amount_cny = quote.amount
    formula_parts = [f"{quote.amount} {quote.currency}"]
    used_fx: FXQuote | None = None

    if quote.currency != "CNY":
        fx_error = _fx_error(quote.currency, fx)
        if fx_error is not None:
            return _pending_price(quote, fx_error)
        assert fx is not None
        used_fx = fx
        if fx.base_currency == quote.currency and fx.quote_currency == "CNY":
            amount_cny = quote.amount * fx.rate
            formula_parts.append(
                f"* {fx.rate} ({fx.base_currency}/{fx.quote_currency})"
            )
        else:
            amount_cny = quote.amount / fx.rate
            formula_parts.append(
                f"/ {fx.rate} ({fx.base_currency}/{fx.quote_currency})"
            )

    normalized_amount = amount_cny * target_quantity / quote.unit_quantity
    formula_parts.append(f"* {target_quantity} / {quote.unit_quantity}")

    key = build_comparison_key(
        metric=quote.metric,
        direction=quote.direction,
        unit_name=target_unit,
        unit_quantity=target_quantity,
        conditions=quote.conditions,
    )
    status = quote.verification_status
    reasons: tuple[str, ...] = ()
    if status not in COMPARABLE_STATUSES:
        reasons = ("source price is not verified or approved",)
        status = NormalizationStatus.PENDING.value

    return NormalizedPrice(
        kind=PriceKind.MONETARY,
        raw_amount=quote.amount,
        raw_currency=quote.currency,
        raw_unit_name=quote.unit_name,
        raw_unit_quantity=quote.unit_quantity,
        metric=quote.metric,
        direction=quote.direction,
        conditions=quote.conditions,
        normalized_amount=normalized_amount,
        normalized_currency="CNY",
        normalized_unit_name=target_unit,
        normalized_unit_quantity=target_quantity,
        comparison_key=key,
        verification_status=status,
        formula=" ".join(formula_parts),
        fx=used_fx,
        pending_reasons=reasons,
    )


def apply_discount(base: NormalizedPrice, discount: DiscountQuote) -> NormalizedPrice:
    """Apply a discount only to a verified base with exactly the same key."""

    conditions = discount.conditions
    target_key = None
    if (
        base.normalized_unit_name is not None
        and base.normalized_unit_quantity is not None
    ):
        target_key = build_comparison_key(
            metric=discount.metric,
            direction=discount.direction,
            unit_name=base.normalized_unit_name,
            unit_quantity=base.normalized_unit_quantity,
            conditions=conditions,
        )

    reasons: list[str] = []
    if base.kind is not PriceKind.MONETARY:
        reasons.append("discount base must be an absolute monetary price")
    if not base.is_comparable:
        reasons.append("discount requires a verified or approved normalized base price")
    if discount.verification_status not in COMPARABLE_STATUSES:
        reasons.append("discount is not verified or approved")
    if target_key is None or target_key != base.comparison_key:
        reasons.append("discount and base comparison_key differ")

    if reasons:
        return NormalizedPrice(
            kind=PriceKind.DISCOUNT,
            raw_amount=discount.multiplier,
            raw_currency=None,
            raw_unit_name=None,
            raw_unit_quantity=None,
            metric=discount.metric,
            direction=discount.direction,
            conditions=conditions,
            normalized_amount=None,
            normalized_currency=base.normalized_currency,
            normalized_unit_name=base.normalized_unit_name,
            normalized_unit_quantity=base.normalized_unit_quantity,
            comparison_key=target_key,
            verification_status=NormalizationStatus.PENDING.value,
            formula=None,
            pending_reasons=tuple(reasons),
        )

    assert base.normalized_amount is not None
    status = (
        NormalizationStatus.APPROVED.value
        if base.verification_status == NormalizationStatus.APPROVED.value
        and discount.verification_status == NormalizationStatus.APPROVED.value
        else NormalizationStatus.VERIFIED.value
    )
    return NormalizedPrice(
        kind=PriceKind.DISCOUNT,
        raw_amount=discount.multiplier,
        raw_currency=None,
        raw_unit_name=None,
        raw_unit_quantity=None,
        metric=discount.metric,
        direction=discount.direction,
        conditions=conditions,
        normalized_amount=base.normalized_amount * discount.multiplier,
        normalized_currency=base.normalized_currency,
        normalized_unit_name=base.normalized_unit_name,
        normalized_unit_quantity=base.normalized_unit_quantity,
        comparison_key=base.comparison_key,
        verification_status=status,
        formula=f"{base.normalized_amount} * {discount.multiplier}",
        pending_reasons=(),
    )


def approve(price: NormalizedPrice) -> NormalizedPrice:
    """Return an approved copy; pending/rejected results cannot be promoted."""

    if price.normalized_amount is None or price.comparison_key is None:
        raise ValueError("an incomplete normalization cannot be approved")
    if price.verification_status == NormalizationStatus.REJECTED.value:
        raise ValueError("a rejected normalization cannot be approved")
    return replace(price, verification_status=NormalizationStatus.APPROVED.value)


def build_comparison_key(
    *,
    metric: str,
    direction: str | None,
    unit_name: str,
    unit_quantity: Decimal,
    conditions: Mapping[str, Any] | None = None,
) -> str:
    """Build a stable SHA-256 identity for exactly comparable prices."""

    payload = {
        "version": 1,
        "metric": _name(metric, "metric"),
        "direction": None if direction is None else _name(direction, "direction"),
        "target": {
            "unit_name": _unit_key(unit_name),
            "unit_quantity": _decimal_text(
                _decimal(unit_quantity, "unit_quantity")
            ),
        },
        "conditions": _canonical_value(conditions or {}),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_conditions(conditions: Mapping[str, Any]) -> str:
    """Expose the canonical condition document used by comparison keys."""

    return json.dumps(
        _canonical_value(conditions),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pending_price(quote: PriceQuote, reason: str) -> NormalizedPrice:
    return NormalizedPrice(
        kind=PriceKind.MONETARY,
        raw_amount=quote.amount,
        raw_currency=quote.currency,
        raw_unit_name=quote.unit_name,
        raw_unit_quantity=quote.unit_quantity,
        metric=quote.metric,
        direction=quote.direction,
        conditions=quote.conditions,
        normalized_amount=None,
        normalized_currency=None,
        normalized_unit_name=None,
        normalized_unit_quantity=None,
        comparison_key=None,
        verification_status=NormalizationStatus.PENDING.value,
        pending_reasons=(reason,),
    )


def _fx_error(currency: str, fx: FXQuote | None) -> str | None:
    if fx is None:
        return f"verified FX evidence is required for {currency}/CNY"
    if fx.verification_status not in COMPARABLE_STATUSES:
        return "FX evidence is not verified or approved"
    if fx.rate_date is None:
        return "FX evidence has no source date"
    if not fx.source or not fx.source.strip():
        return "FX evidence has no source"
    direct = fx.base_currency == currency and fx.quote_currency == "CNY"
    inverse = fx.base_currency == "CNY" and fx.quote_currency == currency
    if not (direct or inverse):
        return (
            f"FX pair {fx.base_currency}/{fx.quote_currency} "
            f"cannot convert {currency} to CNY"
        )
    return None


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be Decimal-compatible without binary float")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (str, int)):
        result = Decimal(str(value))
    else:
        raise TypeError(f"{label} must be Decimal, str, or int")
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _currency(value: str) -> str:
    currency = str(value).strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a three-letter code")
    return currency


def _name(value: str, label: str) -> str:
    result = str(value).strip().casefold()
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _unit_key(value: str) -> str:
    return "_".join(str(value).strip().casefold().replace("-", " ").split())


def _status(value: str | NormalizationStatus) -> str:
    status = (
        value.value
        if isinstance(value, NormalizationStatus)
        else str(value).strip().lower()
    )
    if status not in {item.value for item in NormalizationStatus}:
        raise ValueError(f"unsupported verification status: {value!r}")
    return status


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("conditions must be a mapping")
    canonical = _canonical_value(value)
    return MappingProxyType(canonical)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).strip().casefold()
            if not name:
                raise ValueError("condition keys must not be empty")
            if name in result:
                raise ValueError(f"duplicate canonical condition key: {name}")
            result[name] = _canonical_value(item)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, float):
        raise TypeError("condition numbers must not use binary float")
    if isinstance(value, (str, int, bool)) or value is None:
        return value.strip() if isinstance(value, str) else value
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    raise TypeError(f"unsupported condition value: {type(value).__name__}")
