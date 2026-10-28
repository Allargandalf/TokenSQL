"""Raw pricing response projection; arithmetic lives exclusively in the frontend."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .units import expand_price_payload
from .pricing_conditions import match_official_prices


def quote_projection(value: dict[str, Any], official: dict[str, Any] | list[dict[str, Any]] | None,
                     context: dict[str, Any], *, at: datetime | None = None,
                     validate: bool = False) -> dict[str, Any]:
    """Preserve source amount/discount without calculating, validating, or persisting derived prices."""
    raw = expand_price_payload(value, allow_legacy=True)
    output = dict(raw)
    if isinstance(official, list):
        matched, match = match_official_prices(raw, official)
    else:
        # Compatibility for callers that already selected one explicit legacy official item.
        matched = [official] if official else []
        identifiers = [official["official_price_id"]] if official and official.get("official_price_id") else []
        match = {"status": "matched" if official else "missing", "reason": "legacy_explicit_item" if official else "no_official_prices",
                 "candidate_ids": identifiers, "matched_ids": identifiers}
    matched = [expand_price_payload(item, allow_legacy=True) for item in matched]
    issues = ([{"code": "unknown_legacy_pricing_unit",
                "message": "Legacy unit preserved without guessing or converting it"}]
              if raw.get("legacy_unit") else [])
    output.update(raw_discount_rate=raw.get("discount_rate"), derived_amount=None,
                  converted_supplier_amount=None, converted_supplier_pricing_unit_id=None,
                  conversion_status="unavailable", conversion_evidence={},
                  discount_status="unavailable", discount_evidence={},
                  official_price=matched[0] if len(matched) == 1 else None,
                  official_prices=matched, official_match=match,
                  normalized={}, issues=issues, display_issues=[],
                  pricing_issue=issues[0]["code"] if issues else None)
    return output


def normalize_price(value: dict[str, Any], context: dict[str, Any],
                    *, at: datetime | None = None) -> dict[str, Any]:
    """Compatibility stub: automatic backend standardized prices are intentionally removed."""
    return {}
