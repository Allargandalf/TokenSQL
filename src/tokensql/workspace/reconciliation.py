"""Deterministic supplier/source reconciliation; prices are never quote identity."""
from __future__ import annotations

from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
import hashlib
import json
import re
from typing import Any

from .pricing_conditions import compact_conditions, legacy_metric, match_official_prices
from .schemas import QuoteCreate, SupplierReconciliationRequest
from .units import canonical_price_payload, expand_price_payload


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def raw_quote(value: dict[str, Any]) -> dict[str, Any]:
    """Strip response projections while retaining every editable source fact."""
    raw = {key: value[key] for key in QuoteCreate.model_fields if key in value}
    if "raw_discount_rate" in value:
        raw["discount_rate"] = value["raw_discount_rate"]
    if raw.get("pricing_unit_id"):
        for key in ("currency", "unit", "unit_quantity"):
            raw.pop(key, None)
    return QuoteCreate.model_validate(raw, context={"allow_legacy_unit": True}).model_dump(mode="json")


def quote_identity(value: dict[str, Any]) -> dict[str, Any]:
    expanded = expand_price_payload(value, allow_legacy=True)
    metric = expanded.get("billing_metric") or legacy_metric(expanded.get("billing_item"))
    items = []
    for item in expanded.get("discount_items") or []:
        selector = {key: val for key, val in item.items() if val is not None}
        selector["pricing_conditions"] = compact_conditions(selector.get("pricing_conditions"))
        items.append(selector)
    unit = expanded.get("unit")
    quantity = expanded.get("unit_quantity")
    return {
        "model_id": expanded["model_id"],
        "billing_metric": metric,
        "billing_item": None if metric else expanded.get("billing_item"),
        "discount_scope": expanded.get("discount_scope", "unknown"),
        "discount_items": sorted(items, key=canonical_json),
        "supplier_model_id": expanded.get("supplier_model_id"),
        "pricing_conditions": compact_conditions(expanded.get("pricing_conditions")),
        # Unknown commercial conditions remain identity; ordinary tags/notes never are.
        "unstructured_conditions": expanded.get("conditions") if not compact_conditions(expanded.get("pricing_conditions")) else None,
        "currency": expanded.get("currency"),
        "unit": unit,
        # Token scale alone may change without changing product identity.
        "unit_quantity": None if unit == "token" else quantity,
        "dimension": expanded.get("dimension"),
        "direction": expanded.get("direction"),
    }


def entry_key(value: dict[str, Any]) -> str:
    identifier = value.get("source_entry_id")
    return "entry:" + identifier if identifier else "identity:" + digest(quote_identity(value))


def comparable_quote(value: dict[str, Any]) -> dict[str, Any]:
    raw = canonical_price_payload(raw_quote(value))
    raw["pricing_conditions"] = compact_conditions(raw.get("pricing_conditions"))
    raw["billing_metric"] = raw.get("billing_metric") or legacy_metric(raw.get("billing_item"))
    # File names/cells are evidence rather than changes to an otherwise identical quote.
    for key in ("source", "source_ref"):
        raw.pop(key, None)
    for key, item in list(raw.items()):
        if isinstance(item, str) and key in {"amount", "discount_rate", "unit_quantity"}:
            raw[key] = format(Decimal(item).normalize(), "f")
    raw["tags"] = sorted(raw.get("tags") or [])
    raw["discount_items"] = sorted(raw.get("discount_items") or [], key=canonical_json)
    return raw


def pricing_basis_issue(value: dict[str, Any], official_prices: list[dict]) -> dict | None:
    """Check only directly comparable source facts; never persist a derived price."""
    if value.get("pricing_basis") or value.get("amount") is None or value.get("discount_rate") is None:
        return None
    matched, match = match_official_prices(value, official_prices)
    if len(matched) != 1 or match["status"] != "matched":
        return None
    quoted = expand_price_payload(value, allow_legacy=True)
    official = expand_price_payload(matched[0], allow_legacy=True)
    if quoted.get("legacy_unit") or official.get("legacy_unit"):
        return None
    if (not quoted.get("currency") or quoted.get("currency") != official.get("currency")
            or not quoted.get("unit") or quoted.get("unit") != official.get("unit")
            or quoted.get("unit_quantity") is None or official.get("unit_quantity") is None
            or official.get("amount") is None):
        return None
    amount, rate, reference = (Decimal(str(quoted["amount"])), Decimal(str(quoted["discount_rate"])),
                               Decimal(str(official["amount"])))
    quantity, official_quantity = (Decimal(str(quoted["unit_quantity"])),
                                    Decimal(str(official["unit_quantity"])))
    # Fraction arithmetic avoids introducing a rounding decision into validation.
    basis = Fraction(reference) * Fraction(quantity) / Fraction(official_quantity)
    expected = basis * Fraction(rate)
    amount_precision = Fraction(10) ** amount.as_tuple().exponent / 2
    tolerance = abs(basis) * Fraction(5, 1000) + amount_precision
    if abs(Fraction(amount) - expected) <= tolerance:
        return None
    with localcontext() as context:
        context.prec = max(80, len(amount.as_tuple().digits) + len(reference.as_tuple().digits) + 20)
        context.traps[Inexact] = False
        expected_text = format(Decimal(expected.numerator) / Decimal(expected.denominator), "f")
        tolerance_text = format(Decimal(tolerance.numerator) / Decimal(tolerance.denominator), "f")
    return {"code": "pricing_basis_required",
            "message": "amount and discount disagree; confirm amount or discount_rate as the pricing basis",
            "official_price_id": official.get("official_price_id"),
            "observed_amount": str(amount), "expected_amount": expected_text, "tolerance": tolerance_text}


def _relaxed_identity(value: dict[str, Any]) -> dict[str, Any]:
    identity = quote_identity(value)
    for key in ("pricing_conditions", "unstructured_conditions", "currency", "unit", "unit_quantity", "dimension", "direction"):
        identity.pop(key, None)
    return identity


def _ordered_version(value: str | None) -> tuple[int, ...] | None:
    if value and re.fullmatch(r"[vV]?\d+(?:[._-]\d+)*", value.strip()):
        return tuple(int(part) for part in re.findall(r"\d+", value))
    return None


def build_reconciliation(request: SupplierReconciliationRequest | dict,
                         snapshot: dict[str, Any]) -> dict[str, Any]:
    request = SupplierReconciliationRequest.model_validate(request)
    suppliers = {value["supplier_id"]: value for value in snapshot.get("suppliers", [])}
    supplier = suppliers.get(request.supplier_id)
    if supplier is None:
        raise ValueError("supplier_id must reference a confirmed supplier")
    name = supplier["name"]
    # Approving supplier quotes may create missing models, but cannot authorize
    # edits to the established model catalog. Existing ID stubs are harmless.
    existing_models = {value["model_id"]: value for value in snapshot.get("models", [])}
    new_models = []
    for item in request.models:
        data = item.model_dump(mode="json", exclude={"expected_revision"})
        old_model = existing_models.get(item.model_id)
        if old_model is None:
            new_models.append(item.model_dump(mode="json"))
            continue
        for field, value in data.items():
            if field == "model_id" or value is None or value == []:
                continue
            changed = (not set(value).issubset(old_model.get(field) or [])
                       if field == "aliases" else value != old_model.get(field))
            if changed:
                raise ValueError(
                    f"supplier updates cannot modify existing model metadata: {item.model_id} ({field})")
    allowed_names = {name.casefold(), *(value.casefold() for value in supplier.get("aliases", []))}
    all_quotes = [value for value in snapshot.get("quotes", []) if value.get("status", "active") == "active"]
    by_id = {value["id"]: value for value in all_quotes}

    def in_scope(value: dict[str, Any], *, claim: bool = False) -> bool:
        return (value.get("supplier_id") == request.supplier_id
                and (value.get("source_scope") == request.source_scope
                     or (claim and not value.get("source_scope")))
                and (not request.model_ids or value["model_id"] in request.model_ids)
                and (not request.supplier_model_ids or value.get("supplier_model_id") in request.supplier_model_ids)
                and (not request.expected_sheets or value.get("source_sheet") in request.expected_sheets
                     or (claim and not value.get("source_sheet"))))

    old_quotes = [value for value in all_quotes if in_scope(value)]
    mapping = {value["entry_key"]: value for value in snapshot.get("supplier_mappings", [])
               if value["supplier_id"] == request.supplier_id and value["source_scope"] == request.source_scope}
    groups: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("create", "update", "delete", "unchanged", "needs_confirmation")}
    bundle: dict[str, Any] = {
        "models": new_models,
        "official_prices": [value.model_dump(mode="json") for value in request.official_prices],
        "create": [], "update": [], "delete": []}
    decisions = {value.entry_key: value for value in request.decisions}
    if len(decisions) != len(request.decisions):
        raise ValueError("duplicate reconciliation decisions")
    used_decisions: set[str] = set()
    used_ids: set[int] = set()
    seen: dict[str, dict[str, Any]] = {}
    mappings: list[dict[str, Any]] = []
    semantic_rows: list[dict[str, Any]] = []

    def pending(key: str, reason: str, after: dict | None = None,
                candidates: list[dict] | None = None) -> None:
        groups["needs_confirmation"].append({"entry_key": key, "reason": reason,
            "after": after, "before": None, "candidate_ids": [value["id"] for value in candidates or []]})

    for item in request.entries:
        after = item.model_dump(mode="json")
        if item.supplier_id is not None and item.supplier_id != request.supplier_id:
            raise ValueError("an update entry cannot change supplier")
        if item.supplier and item.supplier.casefold() not in allowed_names:
            raise ValueError("entry supplier name does not match the confirmed supplier")
        if item.source_scope and item.source_scope != request.source_scope:
            raise ValueError("entry source_scope is outside the confirmed update boundary")
        after.update(supplier_id=request.supplier_id, supplier=name, source_scope=request.source_scope)
        if not after.get("source_sheet") and len(request.expected_sheets) == 1:
            after["source_sheet"] = request.expected_sheets[0]
        key = entry_key(after)
        if request.model_ids and after["model_id"] not in request.model_ids:
            pending(key, "model_outside_confirmed_scope", after)
            continue
        if request.supplier_model_ids and after.get("supplier_model_id") not in request.supplier_model_ids:
            pending(key, "channel_outside_confirmed_scope", after)
            continue
        if request.expected_sheets and after.get("source_sheet") not in request.expected_sheets:
            pending(key, "sheet_outside_or_missing_from_confirmed_scope", after)
            continue
        if key in seen:
            if comparable_quote(seen[key]) != comparable_quote(after):
                pending(key, "duplicate_source_identity_has_conflicting_values", after)
            continue
        seen[key] = after
        semantic_rows.append(comparable_quote(after))
        decision = decisions.get(key)
        if decision:
            used_decisions.add(key)
        if decision and decision.action == "ignore":
            ignored = [value for value in old_quotes
                       if (after.get("source_entry_id") and value.get("source_entry_id") == after["source_entry_id"])
                       or quote_identity(value) == quote_identity(after)
                       or _relaxed_identity(value) == _relaxed_identity(after)]
            saved_ignore = mapping.get(key)
            if saved_ignore and saved_ignore["quote_id"] in by_id:
                ignored.append(by_id[saved_ignore["quote_id"]])
            used_ids.update(value["id"] for value in ignored if in_scope(value))
            groups["unchanged"].append({"entry_key": key, "before": None, "after": after,
                                        "candidate_ids": [], "reason": "explicitly_ignored"})
            continue
        target = None
        explicit = bool(decision)
        if decision and decision.action == "update":
            target = by_id.get(decision.quote_id)
            if target is None or not in_scope(target, claim=True):
                raise ValueError("explicit mapping target is outside the confirmed supplier/source scope")
            if target["revision"] != decision.expected_revision:
                pending(key, "mapping_target_revision_changed_requires_confirmation", after, [target])
                continue
        elif not decision:
            if after.get("source_entry_id"):
                candidates = [value for value in old_quotes
                              if value.get("source_entry_id") == after["source_entry_id"]]
            else:
                candidates = []
            saved = mapping.get(key)
            if not candidates and saved and saved["quote_id"] in by_id and in_scope(by_id[saved["quote_id"]]):
                candidates = [by_id[saved["quote_id"]]]
            if not candidates:
                candidates = [value for value in old_quotes if quote_identity(value) == quote_identity(after)]
            if len(candidates) > 1:
                pending(key, "multiple_existing_quote_candidates", after, candidates)
                continue
            if len(candidates) == 1:
                target = candidates[0]
                if quote_identity(target) != quote_identity(after):
                    pending(key, "pricing_identity_changed_requires_mapping", after, candidates)
                    continue
            else:
                if not after.get("supplier_model_id"):
                    def without_channel(value: dict) -> dict:
                        identity = _relaxed_identity(value)
                        identity.pop("supplier_model_id", None)
                        return identity
                    channels = [value for value in old_quotes if value.get("supplier_model_id")
                                and without_channel(value) == without_channel(after)]
                    if channels:
                        pending(key, "channel_not_confirmed_choose_update_or_create", after, channels)
                        continue
                similar = [value for value in old_quotes if _relaxed_identity(value) == _relaxed_identity(after)]
                if similar:
                    pending(key, "conditions_or_units_changed_choose_update_or_create", after, similar)
                    continue
        if target is not None:
            if target["id"] in used_ids:
                pending(key, "multiple_entries_target_one_quote", after, [target])
                continue
            used_ids.add(target["id"])
            group = {"entry_key": key, "before": target, "after": after,
                     "candidate_ids": [target["id"]], "reason": "explicit_mapping" if explicit else "stable_identity"}
            if comparable_quote(target) == comparable_quote(after):
                groups["unchanged"].append(group)
            else:
                bundle["update"].append({"id": target["id"], "expected_revision": target["revision"], "changes": after})
                groups["update"].append(group)
            mappings.append({"entry_key": key, "quote_id": target["id"], "create_index": None,
                             "identity_hash": digest(quote_identity(after))})
        else:
            create_index = len(bundle["create"])
            bundle["create"].append(after)
            groups["create"].append({"entry_key": key, "before": None, "after": after,
                                     "candidate_ids": [], "reason": "explicit_create" if explicit else "new_identity"})
            mappings.append({"entry_key": key, "quote_id": None, "create_index": create_index,
                             "identity_hash": digest(quote_identity(after))})

    unknown_decisions = set(decisions) - used_decisions
    if unknown_decisions:
        raise ValueError("decision entry_key does not exist in the current parsed entries")
    for deletion in request.explicit_delete_ids:
        old = by_id.get(deletion.id)
        if old is None or not in_scope(old):
            raise ValueError("explicit deletion is outside the confirmed supplier/source scope")
        if old["revision"] != deletion.expected_revision:
            pending("delete:" + str(old["id"]), "deletion_revision_changed_requires_confirmation", None, [old])
            continue
        if old["id"] in used_ids:
            raise ValueError("a quote cannot be updated and deleted in the same reconciliation")
        used_ids.add(old["id"])
        bundle["delete"].append(deletion.model_dump(mode="json"))
        groups["delete"].append({"entry_key": "delete:" + str(old["id"]), "before": old, "after": None,
                                 "candidate_ids": [old["id"]], "reason": "explicit_deletion"})

    if request.unresolved_items:
        pending("request", "unresolved_items_block_confirmation")
    full_complete = (request.scope_confirmed and request.complete_read and request.expected_sheets
                     and set(request.expected_sheets).issubset(request.covered_sheets)
                     and request.unresolved_items == 0)
    if request.mode == "full":
        if not full_complete:
            pending("request", "full_replace_requires_confirmed_scope_and_complete_sheet_read")
        # No missing-row deletion is ever produced from an incomplete/ambiguous parse.
        if full_complete and not groups["needs_confirmation"]:
            for old in old_quotes:
                if old["id"] not in used_ids:
                    bundle["delete"].append({"id": old["id"], "expected_revision": old["revision"]})
                    groups["delete"].append({"entry_key": "delete:" + str(old["id"]), "before": old,
                        "after": None, "candidate_ids": [old["id"]], "reason": "missing_from_confirmed_complete_source"})

    receipts = [value for value in snapshot.get("supplier_update_receipts", [])
                if value["supplier_id"] == request.supplier_id and value["source_scope"] == request.source_scope]
    semantic_digest = digest(sorted(semantic_rows, key=canonical_json))
    same_file = [value for value in receipts if value["file_sha256"] == request.file_sha256]
    has_changes = any(bundle[key] for key in ("models", "official_prices", "create", "update", "delete"))
    if has_changes:
        dates = [value.get("source_effective_date") for value in receipts if value.get("source_effective_date")]
        if request.source_effective_date and dates and request.source_effective_date.isoformat() < max(dates):
            if not request.allow_rollback:
                pending("version", "older_source_requires_explicit_rollback_confirmation")
        incoming_version = _ordered_version(request.source_version)
        versions = [_ordered_version(value.get("source_version")) for value in receipts]
        ordered = [value for value in versions if value is not None]
        if incoming_version and ordered and incoming_version < max(ordered) and not request.allow_rollback:
            pending("version", "older_version_requires_explicit_rollback_confirmation")
        changed_same_version = [value for value in receipts if request.source_version
            and value.get("source_version") == request.source_version
            and value.get("file_sha256") != request.file_sha256
            and value.get("semantic_digest") != semantic_digest]
        if changed_same_version and not request.allow_rollback:
            pending("version", "same_version_changed_content_requires_explicit_confirmation")
        if not request.source_effective_date and not incoming_version and not request.latest_confirmed:
            pending("version", "source_order_unknown_confirm_this_is_the_current_file")

    review = {"bundle": bundle, "groups": groups, "request": request.model_dump(mode="json"),
              "semantic_digest": semantic_digest, "mappings": mappings,
              "already_applied_file": bool(same_file)}
    review["ready"] = not groups["needs_confirmation"]
    stable_groups = {}
    for group, values in groups.items():
        stable_groups[group] = [{**value, "before": (
            {"id": value["before"]["id"], "revision": value["before"]["revision"],
             "raw": comparable_quote(value["before"])} if value.get("before") else None)}
            for value in values]
    review["review_digest"] = digest({**review, "groups": stable_groups})
    return review
