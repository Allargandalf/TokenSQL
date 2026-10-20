"""Closed pricing-unit dictionary and strict legacy boundary adapters."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

PricingUnitId = Literal[
    "usd_ktoken", "usd_mtoken", "usd_second", "usd_image",
    "hkd_ktoken", "hkd_mtoken", "hkd_second", "hkd_image",
    "cny_ktoken", "cny_mtoken", "cny_second", "cny_image",
]

_UNIT_ALIASES = {
    "token": "token", "tokens": "token", "个token": "token", "令牌": "token",
    "second": "second", "seconds": "second", "s": "second", "sec": "second", "秒": "second",
    "image": "image", "images": "image", "张": "image",
}


@dataclass(frozen=True)
class PricingUnit:
    id: str
    currency: str
    unit: str
    unit_quantity: Decimal
    dimension: str
    scale: str
    label: str
    symbol: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id, "currency": self.currency, "unit": self.unit,
            "unit_quantity": format(self.unit_quantity, "f"), "dimension": self.dimension,
            "scale": self.scale, "label": self.label, "symbol": self.symbol,
        }


def _build() -> tuple[PricingUnit, ...]:
    bases = (
        ("ktoken", "token", Decimal("1000"), "token", "1K tokens", "ktoken"),
        ("mtoken", "token", Decimal("1000000"), "token", "1M tokens", "mtoken"),
        ("second", "second", Decimal("1"), "time", "second", "s"),
        ("image", "image", Decimal("1"), "image", "image", "张"),
    )
    symbols = {"USD": "$", "HKD": "HK$", "CNY": "¥"}
    return tuple(PricingUnit(
        id=f"{currency.casefold()}_{scale}", currency=currency, unit=unit,
        unit_quantity=quantity, dimension=dimension, scale=scale,
        label=f"{currency} / {label}", symbol=f"{symbols[currency]} / {suffix}",
    ) for currency in ("USD", "HKD", "CNY")
      for scale, unit, quantity, dimension, label, suffix in bases)


PRICING_UNITS = _build()
PRICING_UNIT_BY_ID = {value.id: value for value in PRICING_UNITS}
PRICING_UNIT_BY_FIELDS = {
    (value.currency, value.unit, value.unit_quantity): value for value in PRICING_UNITS
}


def pricing_units_payload() -> dict[str, Any]:
    return {"version": "1", "items": [value.as_dict() for value in PRICING_UNITS]}


def canonical_legacy_unit(value: Any) -> str:
    text = str(value).strip().casefold()
    return _UNIT_ALIASES.get(text, text)


def _quantity(value: Any) -> Decimal:
    if isinstance(value, float):
        raise ValueError("unit_quantity must use an exact Decimal string")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("unit_quantity must be an exact positive Decimal") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("unit_quantity must be an exact positive Decimal")
    return result


def unit_for_fields(currency: Any, unit: Any, unit_quantity: Any) -> PricingUnit | None:
    if currency is None or unit is None or unit_quantity is None:
        return None
    return PRICING_UNIT_BY_FIELDS.get((str(currency).strip().upper(),
                                      canonical_legacy_unit(unit), _quantity(unit_quantity)))


def expand_price_payload(value: dict[str, Any], *, allow_legacy: bool = False,
                         default_currency: str | None = None) -> dict[str, Any]:
    """Expand known units; only explicit create boundaries may default an omitted currency."""
    result = dict(value)
    if default_currency is not None and "currency" not in result and not result.get("pricing_unit_id"):
        result["currency"] = default_currency
    for key in ("pricing_unit_id", "currency", "unit", "unit_quantity"):
        if isinstance(result.get(key), str) and not result[key].strip():
            result[key] = None
    identifier = result.get("pricing_unit_id")
    supplied = {key: result[key] for key in ("currency", "unit", "unit_quantity")
                if result.get(key) is not None}
    if identifier is None and result.get("legacy_unit") is not None:
        return result
    if identifier is not None:
        definition = PRICING_UNIT_BY_ID.get(str(identifier))
        if definition is None:
            if not allow_legacy:
                raise ValueError(f"unknown pricing_unit_id {identifier}")
            legacy = {"pricing_unit_id": str(identifier), **supplied}
            result.update(pricing_unit_id=None, legacy_unit=legacy)
            for key in ("currency", "unit", "unit_quantity"):
                result[key] = legacy.get(key)
            return result
        for key, raw in supplied.items():
            actual = (str(raw).strip().upper() if key == "currency" else
                      canonical_legacy_unit(raw) if key == "unit" else _quantity(raw))
            wanted = (definition.currency if key == "currency" else definition.unit
                      if key == "unit" else definition.unit_quantity)
            if actual != wanted:
                raise ValueError(f"{key} conflicts with pricing_unit_id {identifier}")
        result.update(currency=definition.currency, unit=definition.unit,
                      unit_quantity=format(definition.unit_quantity, "f"))
        return result
    # Preserve unknown token scale/quantity. Currency defaults only at explicit create boundaries.
    currency = str(supplied["currency"]).strip().upper() if "currency" in supplied else None
    unit = canonical_legacy_unit(supplied["unit"]) if "unit" in supplied else None
    quantity = _quantity(supplied["unit_quantity"]) if "unit_quantity" in supplied else None
    result.update(pricing_unit_id=None, currency=currency, unit=unit,
                  unit_quantity=format(quantity, "f") if quantity is not None else None)
    if None in (currency, unit, quantity):
        return result
    definition = unit_for_fields(currency, unit, quantity)
    if definition is not None:
        result.update(pricing_unit_id=definition.id, currency=definition.currency,
                      unit=definition.unit, unit_quantity=format(definition.unit_quantity, "f"))
        return result
    if not allow_legacy:
        raise ValueError("currency/unit/unit_quantity do not identify one of the 12 pricing units")
    result["legacy_unit"] = {key: result[key] for key in ("currency", "unit", "unit_quantity")}
    return result


def canonical_price_payload(value: dict[str, Any]) -> dict[str, Any]:
    result = expand_price_payload(value)
    if result.get("pricing_unit_id") is not None:
        for key in ("currency", "unit", "unit_quantity", "legacy_unit"):
            result.pop(key, None)
    return result
