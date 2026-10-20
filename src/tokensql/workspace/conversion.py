"""Raw pricing response projection; arithmetic lives exclusively in the frontend."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .units import expand_price_payload


def quote_projection(value: dict[str, Any], official: dict[str, Any] | None,
                     context: dict[str, Any], *, at: datetime | None = None,
                     validate: bool = False) -> dict[str, Any]:
    """Preserve source amount/discount without calculating, validating, or persisting derived prices."""
    raw = expand_price_payload(value, allow_legacy=True)
    output = dict(raw)
    issues = ([{"code": "unknown_legacy_pricing_unit",
                "message": "Legacy unit preserved without guessing or converting it"}]
              if raw.get("legacy_unit") else [])
    output.update(raw_discount_rate=raw.get("discount_rate"), derived_amount=None,
                  converted_supplier_amount=None, converted_supplier_pricing_unit_id=None,
                  conversion_status="unavailable", conversion_evidence={},
                  discount_status="unavailable", discount_evidence={},
                  official_price=expand_price_payload(official, allow_legacy=True) if official else None,
                  normalized={}, issues=issues, display_issues=[],
                  pricing_issue=issues[0]["code"] if issues else None)
    return output


def normalize_price(value: dict[str, Any], context: dict[str, Any],
                    *, at: datetime | None = None) -> dict[str, Any]:
    """Compatibility stub: automatic backend standardized prices are intentionally removed."""
    return {}
