"""Explicit pricing scenarios and conservative, arithmetic-free official matching."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConditionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("conditions require exact integers or strings")
        return value


class TokenRange(ConditionContract):
    min: int | None = Field(default=None, ge=0, strict=True)
    max: int | None = Field(default=None, ge=0, strict=True)
    min_inclusive: bool = True
    max_inclusive: bool = True

    @model_validator(mode="after")
    def ordered(self) -> "TokenRange":
        if self.min is None and self.max is None:
            raise ValueError("token range requires at least one bound")
        if self.min is not None and self.max is not None:
            if self.min > self.max or (self.min == self.max and not (
                    self.min_inclusive and self.max_inclusive)):
                raise ValueError("token range must be nonempty")
        return self


class PricingConditions(ConditionContract):
    market: str | None = None
    platform: str | None = None
    input_modality: str | None = None
    output_modality: str | None = None
    input_tokens: TokenRange | None = None
    output_tokens: TokenRange | None = None
    mode: str | None = None
    time_band: str | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=0, strict=True)
    resolution: str | None = None
    with_audio: bool | None = Field(default=None, strict=True)
    reference_video: bool | None = Field(default=None, strict=True)
    customer_tier: str | None = None
    tax_status: Literal["included", "excluded", "unknown"] | None = None
    legacy_text: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("market", "platform", "input_modality", "output_modality", "mode",
                     "time_band", "resolution", "customer_tier", "legacy_text")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("condition values must not be blank")
        return value


class DiscountItem(ConditionContract):
    official_price_id: str | None = None
    billing_metric: str | None = None
    billing_item: str | None = None
    pricing_conditions: PricingConditions = Field(default_factory=PricingConditions)

    @model_validator(mode="after")
    def selector_required(self) -> "DiscountItem":
        if not (self.official_price_id or self.billing_metric or self.billing_item):
            raise ValueError("discount item requires an official_price_id, billing_metric or billing_item")
        return self


class OfficialMatch(ConditionContract):
    status: Literal["matched", "multiple", "ambiguous", "needs_scope", "missing"]
    reason: str
    candidate_ids: list[str] = Field(default_factory=list)
    matched_ids: list[str] = Field(default_factory=list)


SIMPLE_METRICS = frozenset({"input", "output", "cache", "cache_read", "cache_write",
                           "cache_storage", "image", "video", "audio", "request", "subscription"})


def legacy_official_semantics(label: str | None) -> tuple[str | None, dict[str, Any]]:
    """Recognize complete documented literals, preserving every source label unchanged."""
    text = label.strip().casefold() if label is not None else ""
    conditions: dict[str, Any] = {}
    tags = {"[忙时]": ("time_band", "busy"), "[闲时]": ("time_band", "off_peak"),
            "[国内版]": ("market", "domestic"), "[国际版]": ("market", "international")}
    for token, (field, value) in tags.items():
        if token in text:
            if field in conditions and conditions[field] != value:
                return None, {}
            conditions[field] = value
            text = text.replace(token, "").strip()
    explicit = {"cache read": ("cache_read", {}),
                "cache write (5m)": ("cache_write", {"cache_ttl_seconds": 300}),
                "cache write (1h)": ("cache_write", {"cache_ttl_seconds": 3600})}
    if text in explicit:
        metric, extras = explicit[text]
        return metric, {**conditions, **extras}
    if text in SIMPLE_METRICS:
        return text, conditions
    return None, {}


def legacy_metric(label: str | None) -> str | None:
    return legacy_official_semantics(label)[0]


def compact_conditions(value: Any) -> dict[str, Any]:
    if not isinstance(value, PricingConditions):
        value = PricingConditions.model_validate(value or {})
    value = value.model_dump(mode="json", exclude_none=True)
    return {key: item for key, item in value.items()
            if item is not None and not (key == "raw" and item == {})}


def scenario_key(value: dict[str, Any]) -> str:
    """Retain legacy labels in identities; a missing structured metric is not equivalence."""
    metric = value.get("billing_metric")
    metric = metric.casefold() if isinstance(metric, str) else metric
    conditions = compact_conditions(value.get("pricing_conditions"))
    if "pricing_conditions" not in value and value.get("conditions"):
        conditions = {"raw": value["conditions"]}
    identity = {"billing_metric": metric, "pricing_conditions": conditions}
    if not metric:
        identity["billing_item"] = value.get("billing_item")
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _range_contains(outer: dict, inner: dict) -> bool:
    for bound, direction in (("min", 1), ("max", -1)):
        outside, inside = outer.get(bound), inner.get(bound)
        if outside is None:
            continue
        if inside is None or direction * (inside - outside) < 0:
            return False
        if outside == inside and inner.get(bound + "_inclusive", True) and not outer.get(
                bound + "_inclusive", True):
            return False
    return True


def _ranges_overlap(left: dict, right: dict) -> bool:
    for low, high in ((left, right), (right, left)):
        minimum, maximum = low.get("min"), high.get("max")
        if minimum is not None and maximum is not None:
            if minimum > maximum or (minimum == maximum and not (
                    low.get("min_inclusive", True) and high.get("max_inclusive", True))):
                return False
    return True


def conditions_match(selector: dict, scenario: dict) -> tuple[bool, bool]:
    """Return (compatible, needs_evidence); opaque text is never assumed equivalent."""
    wanted, actual = compact_conditions(selector), compact_conditions(scenario)
    if wanted.get("raw") or wanted.get("legacy_text"):
        return False, True
    uncertain = False
    for key, item in wanted.items():
        other = actual.get(key)
        if key == "tax_status" and (item == "unknown" or other == "unknown"):
            uncertain = True
            continue
        if other is None:
            uncertain = True
        elif key in {"input_tokens", "output_tokens"}:
            if not _range_contains(other, item):
                return False, _ranges_overlap(other, item)
        elif item != other:
            return False, False
    return not uncertain, uncertain


def _selected(selector: dict, price: dict) -> bool:
    for field in ("official_price_id", "billing_metric", "billing_item"):
        if selector.get(field) is None:
            continue
        expected, actual = selector[field], price.get(field)
        if field in {"billing_metric", "billing_item"} and isinstance(actual, str):
            expected, actual = expected.casefold(), actual.casefold()
        if expected != actual:
            return False
    return True


def match_official_prices(quote: dict, official: list[dict]) -> tuple[list[dict], dict]:
    """Match explicit scopes without guessing unknown scopes or selecting a cheapest candidate."""
    prices = [item for item in official if item.get("model_id") == quote.get("model_id")]
    scope = quote.get("discount_scope", "unknown")
    selectors = quote.get("discount_items") or [] if scope == "items" else []
    explicit_metric = quote.get("billing_metric") or legacy_metric(quote.get("billing_item"))
    if scope not in {"unknown", "model", "items"}:
        return [], OfficialMatch(status="needs_scope", reason="discount_scope_unknown",
            candidate_ids=[p["official_price_id"] for p in prices]).model_dump()
    if scope == "items":
        if not selectors:
            return [], OfficialMatch(status="needs_scope", reason="discount_items_required").model_dump()
    elif scope == "model":
        selectors = [{}]
    elif explicit_metric or quote.get("billing_item") is not None:
        selectors = [{"billing_metric": explicit_metric}] if explicit_metric else [
            {"billing_item": quote["billing_item"]}]
    else:
        state = "needs_scope" if prices else "missing"
        return [], OfficialMatch(status=state, reason="discount_scope_unknown" if prices else
            "no_official_prices", candidate_ids=[p["official_price_id"] for p in prices]).model_dump()
    candidates, matches, uncertain = {}, {}, False
    if scope == "unknown" and explicit_metric:
        asserted = compact_conditions(quote.get("pricing_conditions"))
        inferred = legacy_official_semantics(quote.get("billing_item"))[1]
        if any(key in asserted and asserted[key] != item for key, item in inferred.items()):
            return [], OfficialMatch(status="ambiguous", reason="source_conditions_conflict",
                candidate_ids=[p["official_price_id"] for p in prices]).model_dump()
        quote = {**quote, "pricing_conditions": {**inferred, **asserted}}
    for selector in selectors:
        if isinstance(selector, DiscountItem):
            selector = selector.model_dump(mode="json", exclude_none=True)
        for price in prices:
            if not _selected(selector, price):
                continue
            identifier = price["official_price_id"]
            candidates[identifier] = price
            accepted, pending = conditions_match(quote.get("pricing_conditions") or {},
                                                price.get("pricing_conditions") or {})
            selected, pending_selector = conditions_match(selector.get("pricing_conditions") or {},
                                                          price.get("pricing_conditions") or {})
            if scope == "unknown" and accepted and selected:
                actual = compact_conditions(price.get("pricing_conditions"))
                asserted = {**compact_conditions(quote.get("pricing_conditions")),
                            **compact_conditions(selector.get("pricing_conditions"))}
                missing_dimensions = set(actual) - set(asserted)
                if missing_dimensions or actual.get("legacy_text") or actual.get("raw"):
                    accepted, pending = False, True
            uncertain |= pending or pending_selector
            if accepted and selected:
                matches[identifier] = price
    candidate_ids, matched_ids = sorted(candidates), sorted(matches)
    # Legacy text can enumerate scenarios only for an explicitly broad model rule or exact label/id.
    groups = defaultdict(list)
    for price in matches.values():
        groups[scenario_key(price)].append(price)
    conflicts = any(len(group) > 1 for group in groups.values())
    broad = scope in {"model", "items"}
    if conflicts or uncertain or (len(matches) > 1 and not broad):
        return [], OfficialMatch(status="ambiguous", reason="conflicting_official_scenarios" if conflicts
            else "conditions_need_evidence" if uncertain else "multiple_official_scenarios",
            candidate_ids=candidate_ids, matched_ids=[]).model_dump()
    if not matches:
        unclassified = [p["official_price_id"] for p in prices if not p.get("billing_metric")]
        if unclassified and not all(selector.get("official_price_id") or selector.get("billing_item")
                                    for selector in selectors):
            return [], OfficialMatch(status="ambiguous", reason="legacy_official_metric_unclassified",
                candidate_ids=sorted(set(candidate_ids + unclassified))).model_dump()
        return [], OfficialMatch(status="missing", reason="no_matching_official_scenario",
                                 candidate_ids=candidate_ids).model_dump()
    status = "matched" if len(matches) == 1 else "multiple"
    return [matches[key] for key in matched_ids], OfficialMatch(status=status,
        reason="explicit_model_scope" if scope == "model" else "explicit_items_scope" if scope == "items"
        else "explicit_item_match", candidate_ids=candidate_ids, matched_ids=matched_ids).model_dump()


def legacy_quote_scope(value: dict) -> str:
    """A source-specific, exact legacy assertion is the only automatic model-scope migration."""
    if (value.get("discount_rate") is not None and value.get("amount") is None
            and value.get("billing_item") is None
            and value.get("source") == "【港研院】Lab折扣以及模型计费细则.xlsx"
            and (value.get("conditions") or {}).get("折扣口径") == "合作折扣基于原厂定价-不含税"):
        return "model"
    return "unknown"
