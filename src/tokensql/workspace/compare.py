"""Strict read-only comparison of two normalized pricing catalogs.

The baseline's IDs and explicitly verified aliases are the only automatic
identity anchors. Explicit identity maps may resolve known naming differences.
No fuzzy matching, exchange-rate fetching, or database initialization occurs.
"""
from __future__ import annotations

from .discount import normalize_discount_rate

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
from typing import Any

from .pricing import identity_key
from .pricing_conditions import (DiscountItem, compact_conditions, legacy_official_semantics,
                                 legacy_quote_scope, scenario_key)
from .units import expand_price_payload


def normalize(value: Any) -> str:
    return identity_key(str(value or ""))


def label(value: Any) -> str:
    """Preserve punctuation: supplier names, tags and raw channel IDs are not aliases."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def billing(value: Any) -> str:
    known, _ = legacy_official_semantics(value if isinstance(value, str) else None)
    # Unrecognized labels are quoted business literals, not fuzzy model aliases.
    return known if known is not None else " ".join(str(value or "").split()).casefold()


def unit(value: Any) -> str:
    token = normalize(value)
    return {"tokens": "token", "s": "second", "sec": "second", "seconds": "second",
            "images": "image", "requests": "request"}.get(token, token)


def decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_snapshot(path: str | Path) -> dict:
    """Read raw payloads with SQLite mode=ro; never instantiate WorkspaceStore."""
    path = Path(path).resolve(strict=True)
    before = file_hash(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result = {}
        for key, table in (("models", "pricing_model"), ("official_prices", "pricing_official"), ("quotes", "pricing_quote")):
            where = " WHERE status='active'" if key == "quotes" else ""
            values = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM " + table + where)]
            result[key] = values if key == "models" else [
                expand_price_payload(value, allow_legacy=True) for value in values]
    finally:
        connection.close()
    after = file_hash(path)
    if before != after:
        raise ValueError("Database changed while being read: " + str(path))
    result["_read_evidence"] = {"path": str(path), "sha256_before": before, "sha256_after": after, "sqlite_mode": "ro"}
    return result


def _anchors(model: dict) -> set[str]:
    aliases = model.get("aliases") or []
    if not isinstance(aliases, list) or any(not isinstance(value, str) for value in aliases):
        raise ValueError("model aliases must be a list of strings")
    return {normalize(value) for value in [model.get("model_id"), *aliases] if normalize(value)}


def _context(context: dict | None) -> tuple[dict[str, Decimal], list, dict]:
    context = context or {}
    rates = {"CNY": Decimal("1")}
    for entry in context.get("fx_rates", []):
        rate = decimal(entry["cny_per_unit"])
        if rate <= 0 or not entry.get("source"):
            raise ValueError("FX requires a positive explicit rate and source")
        rates[entry["currency"].upper()] = rate
    factors = context.get("token_equivalences", [])
    for entry in factors:
        if decimal(entry["tokens_per_unit"]) <= 0 or not (entry.get("source") or entry.get("source_ref")):
            raise ValueError("Token equivalence requires positive tokens_per_unit and source")
    if (len(rates) > 1 or factors) and context.get("revision") is None and not context.get("version"):
        raise ValueError("Shared pricing context requires revision or version")
    return rates, factors, {"revision": context.get("revision"), "version": context.get("version"),
                            "fx_rates": context.get("fx_rates", []), "token_equivalences": factors}


def _compare_snapshots(baseline: dict, actual: dict, *, identity_map: dict[str, str] | None = None,
                      amount_tolerance: str = "1e-18", rate_tolerance: str = "1e-12",
                      pricing_context: dict | None = None) -> dict:
    """Return strict evidence. Estimated cross-dimension values never grant pass."""
    amount_tol, rate_tol = decimal(amount_tolerance), decimal(rate_tolerance)
    if amount_tol < 0 or rate_tol < 0:
        raise ValueError("Tolerances must be nonnegative")
    rates, factors, shared_context = _context(pricing_context)
    report = {"pass": False, "missing": [], "unexpected": [], "issues": [], "estimates": [],
              "matched": {"models": 0, "official_prices": 0, "quotes": 0}, "identity": {},
              "comparison_context": shared_context, "tolerances": {"amount_per_unit": str(amount_tol), "rate": str(rate_tol)},
              "read_evidence": {"baseline": baseline.get("_read_evidence"), "actual": actual.get("_read_evidence")}}
    issues = report["issues"]
    gold_models, actual_models = baseline.get("models", []), actual.get("models", [])
    for side, snapshot in (("baseline", baseline), ("actual", actual)):
        for key in ("models", "official_prices", "quotes"):
            if not isinstance(snapshot.get(key, []), list):
                raise ValueError(side + "." + key + " must be a list")
        if not snapshot.get("models") or not (snapshot.get("official_prices") or snapshot.get("quotes")):
            issues.append({"code": "empty_catalog", "side": side})
    for version_key in ("fx_version", "factor_version", "pricing_context_revision"):
        left, right = baseline.get(version_key), actual.get(version_key)
        if left is not None or right is not None:
            if left != right:
                issues.append({"code": "context_version_mismatch", "field": version_key, "baseline": left, "actual": right})

    gold = {}
    anchors = defaultdict(set)
    for item in gold_models:
        identifier = item.get("model_id")
        if not identifier or identifier in gold:
            issues.append({"code": "duplicate_or_missing_baseline_model_id", "model_id": identifier})
            continue
        gold[identifier] = item
        vendor = normalize(item.get("vendor"))
        for anchor in _anchors(item):
            anchors[(vendor, anchor)].add(identifier)

    mapping = {}
    mapped_ids = defaultdict(list)
    seen_actual = set()
    for item in actual_models:
        identifier = item.get("model_id")
        if not identifier or identifier in seen_actual:
            issues.append({"code": "duplicate_or_missing_actual_model_id", "model_id": identifier})
            continue
        seen_actual.add(identifier)
        if identifier in (identity_map or {}):
            target = identity_map[identifier]
            candidates = {target} if target in gold else set()
            if not candidates:
                issues.append({"code": "invalid_identity_map_target", "model_id": identifier, "target": target})
        else:
            candidates = {identifier} if identifier in gold else set()
            vendor = normalize(item.get("vendor"))
            if identifier in gold:
                expected_vendor = normalize(gold[identifier].get("vendor"))
                if expected_vendor and vendor and expected_vendor != vendor:
                    issues.append({"code": "model_vendor_mismatch", "model_id": identifier})
            for anchor in _anchors(item):
                candidates.update(anchors.get((vendor, anchor), set()))
                # Missing vendor is not evidence of another entity. Require a unique anchor.
                for (known_vendor, known_anchor), identifiers in anchors.items():
                    if known_anchor == anchor and (not vendor or not known_vendor):
                        candidates.update(identifiers)
        if len(candidates) != 1:
            issues.append({"code": "unknown_model" if not candidates else "ambiguous_model", "model_id": identifier,
                           "candidates": sorted(candidates)})
            report["unexpected"].append({"kind": "model", "model_id": identifier})
            continue
        target = next(iter(candidates))
        mapping[identifier] = target
        mapped_ids[target].append(identifier)
    for target, ids in mapped_ids.items():
        if len(ids) > 1:
            issues.append({"code": "model_identity_split", "baseline_model_id": target, "actual_model_ids": sorted(ids)})
        else:
            report["matched"]["models"] += 1
    for identifier in gold:
        if identifier not in mapped_ids:
            report["missing"].append({"kind": "model", "model_id": identifier})
    report["identity"] = mapping

    def structured(row: dict, kind: str = "official_prices") -> dict:
        # Reuse only the backend's complete, documented legacy literals. Currency,
        # notes and arbitrary long labels never become inferred pricing conditions.
        inferred_metric, inferred_conditions = legacy_official_semantics(row.get("billing_item"))
        metric = row.get("billing_metric")
        if metric is not None and not isinstance(metric, str):
            raise ValueError("billing_metric must be a string or null")
        conditions = compact_conditions(row.get("pricing_conditions"))
        raw = row.get("conditions")
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("original conditions must be an object or null")
        if kind == "official_prices":
            if any(key in conditions and conditions[key] != value
                   for key, value in inferred_conditions.items()):
                raise ValueError("explicit pricing_conditions conflict with the original billing_item")
            # Match the backend's bounded literal adapter: a 5m cache label supplies
            # TTL while an explicit typed market supplies a separate known condition.
            conditions = {**inferred_conditions, **conditions}
            if "pricing_conditions" not in row and raw:
                conditions["raw"] = raw
        return {**row, "billing_metric": metric if metric is not None else inferred_metric,
                "pricing_conditions": conditions or inferred_conditions, "conditions": raw or {}}

    def literal(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)

    official_by_id = {"baseline": defaultdict(list), "actual": defaultdict(list)}
    for side, snapshot in (("baseline", baseline), ("actual", actual)):
        for row in snapshot.get("official_prices", []):
            identifier = row.get("official_price_id")
            if identifier is not None:
                if not isinstance(identifier, str) or not identifier.strip():
                    issues.append({"code": "invalid_official_price_id", "side": side})
                    continue
                official_by_id[side][identifier].append(row)
        for identifier, rows in official_by_id[side].items():
            if len(rows) != 1:
                issues.append({"code": "duplicate_official_price_id", "side": side,
                               "official_price_id": identifier, "count": len(rows)})

    def official_scenario(row: dict, side: str) -> tuple:
        identifier = row.get("model_id")
        target = identifier if side == "baseline" else mapping.get(identifier)
        return target, scenario_key(structured(row))

    def discount_selectors(row: dict, side: str) -> tuple:
        selectors = row.get("discount_items") or []
        if not isinstance(selectors, list):
            raise ValueError("discount_items must be an array")
        result = []
        for raw_selector in selectors:
            selector = DiscountItem.model_validate(raw_selector).model_dump(mode="json", exclude_none=True)
            selector["pricing_conditions"] = compact_conditions(selector.get("pricing_conditions"))
            for field in ("billing_metric", "billing_item"):
                if selector.get(field) is not None:
                    selector[field] = selector[field].casefold()
            identifier = selector.pop("official_price_id", None)
            if identifier is not None:
                referenced = official_by_id[side].get(identifier, [])
                if len(referenced) != 1 or referenced[0].get("model_id") != row.get("model_id"):
                    issues.append({"code": "invalid_discount_item_reference", "side": side,
                                   "model_id": row.get("model_id"), "official_price_id": identifier})
                    selector["invalid_official_price_id"] = identifier
                else:
                    # Opaque IDs are local storage identities. Compare their referenced
                    # model/scenario meaning, never the ID strings across databases.
                    selector["official_scenario"] = official_scenario(referenced[0], side)
            result.append(literal(selector))
        return tuple(sorted(result))

    def key_for(row: dict, kind: str, side: str):
        original_id = row.get("model_id")
        target = original_id if side == "baseline" else mapping.get(original_id)
        if target not in gold:
            issues.append({"code": "orphan_price_model", "side": side, "kind": kind, "model_id": original_id})
            return None
        item = structured(row, kind)
        billing_item = billing(row.get("billing_item") or row.get("metric"))
        metric = item.get("billing_metric")
        base = (target, billing_item, metric.casefold() if metric is not None else "",
                literal(item["pricing_conditions"]), label(row.get("effective_date")),
                literal(item["conditions"]))
        if kind == "quotes":
            base += (label(row.get("dimension")) or unit(row.get("unit")), label(row.get("direction")) or billing_item)
        if kind == "quotes":
            tags = tuple(sorted(str(tag) for tag in (row.get("tags") or [])))
            scope = row.get("discount_scope", legacy_quote_scope(row))
            if scope not in {"unknown", "model", "items"}:
                raise ValueError("invalid discount_scope")
            base += (label(row.get("supplier") or row.get("provider")), tags, str(row.get("supplier_model_id") or ""),
                     scope, discount_selectors(row, side))
        return base

    def measure(row: dict):
        currency = str(row.get("currency") or "").upper()
        if currency and currency not in {"CNY", "USD", "HKD"}:
            raise ValueError("unsupported original currency " + currency)
        quantity = None if row.get("unit_quantity") is None else decimal(row["unit_quantity"])
        if quantity is not None and quantity <= 0:
            raise ValueError("unit_quantity must be positive")
        amount = row.get("amount")
        if amount is None:
            return None, currency, unit(row.get("unit"))
        amount = decimal(amount)
        if amount < 0:
            raise ValueError("amount must not be negative")
        # Missing scale is compared as raw evidence, never assumed to mean one unit.
        return (amount if quantity is None else amount / quantity), currency, unit(row.get("unit"))

    def estimate(row: dict, side: str, model_id: str):
        if any(row.get(field) is None for field in ("currency", "unit", "unit_quantity")):
            return
        value, currency, dimension = measure(row)
        if value is None or currency not in rates or dimension not in {"second", "image"}:
            return
        matching = [factor for factor in factors if factor.get("model_id") in {model_id, row.get("model_id")} and
                    billing(factor.get("billing_item")) == billing(row.get("billing_item")) and unit(factor.get("unit")) == dimension]
        if len(matching) == 1:
            factor = matching[0]
            report["estimates"].append({"side": side, "model_id": model_id, "billing_item": row.get("billing_item"),
                "estimated": True, "cny_per_million_tokens": str(value * rates[currency] / decimal(factor["tokens_per_unit"]) * Decimal("1000000")),
                "context_revision": shared_context["revision"], "source": factor.get("source"), "source_ref": factor.get("source_ref")})

    for kind in ("official_prices", "quotes"):
        groups = {"baseline": defaultdict(list), "actual": defaultdict(list)}
        for side, snapshot in (("baseline", baseline), ("actual", actual)):
            for row in snapshot.get(kind, []):
                if kind == "quotes" and row.get("status", "active") != "active":
                    continue
                try:
                    key = key_for(row, kind, side)
                except (ValueError, TypeError) as error:
                    issues.append({"code": "invalid_pricing_structure", "side": side, "kind": kind,
                                   "model_id": row.get("model_id"), "message": str(error)})
                    continue
                if key is not None:
                    groups[side][key].append(row)
                    if unit(row.get("unit")) in {"second", "image"}:
                        try: estimate(row, side, key[0])
                        except (InvalidOperation, ValueError, TypeError): pass
            if kind == "official_prices":
                unique_counts = defaultdict(int)
                for rows in groups[side].values():
                    for row in rows:
                        unique_counts[official_scenario(row, side)] += 1
                for key, count in unique_counts.items():
                    if count > 1:
                        issues.append({"code": "duplicate_price_key", "side": side, "kind": kind, "key": list(key), "count": count})
        for key in sorted(set(groups["baseline"]) | set(groups["actual"]), key=repr):
            left, right = groups["baseline"].get(key, []), groups["actual"].get(key, [])
            if not left:
                report["unexpected"].extend({"kind": kind, "key": list(key), "row": row} for row in right)
                continue
            if not right:
                report["missing"].extend({"kind": kind, "key": list(key), "row": row} for row in left)
                continue
            # Maximum matching preserves quote multiplicity when tolerances overlap.
            candidates, evidence = defaultdict(list), defaultdict(list)
            for left_index, expected in enumerate(left):
                for right_index, observed in enumerate(right):
                    try:
                        lv, lc, lu = measure(expected); rv, rc, ru = measure(observed)
                        reason, fx_equal = None, None
                        partial = any(row.get(field) is None for row in (expected, observed)
                                      for field in ("currency", "unit", "unit_quantity"))
                        if partial:
                            fields = ("currency", "unit", "unit_quantity", "pricing_unit_id")
                            if any(expected.get(field) != observed.get(field) for field in fields):
                                reason = "partial_unit_context_mismatch"
                        if reason is None and lu != ru:
                            reason = "unit_dimension_mismatch"
                        elif reason is None and lc != rc:
                            reason = "original_currency_mismatch"
                            if pricing_context and lc in rates and rc in rates and lv is not None and rv is not None:
                                fx_equal = abs(lv * rates[lc] - rv * rates[rc]) <= amount_tol
                        if reason is None and ((lv is None) != (rv is None) or (lv is not None and abs(lv - rv) > amount_tol)):
                            reason = "amount_mismatch"
                        lrate = normalize_discount_rate(expected.get("raw_discount_rate", expected.get("discount_rate")))
                        rrate = normalize_discount_rate(observed.get("raw_discount_rate", observed.get("discount_rate")))
                        if reason is None and ((lrate is None) != (rrate is None) or
                            (lrate is not None and abs(decimal(lrate) - decimal(rrate)) > rate_tol)):
                            reason = "discount_rate_mismatch"
                        if reason is None:
                            candidates[left_index].append(right_index)
                        else:
                            evidence[left_index].append({"code": reason, "baseline_per_unit": None if lv is None else str(lv),
                                "actual_per_unit": None if rv is None else str(rv), "baseline_currency": lc, "actual_currency": rc,
                                "baseline_unit": lu, "actual_unit": ru, "shared_fx_equal": fx_equal})
                    except (InvalidOperation, ValueError, TypeError) as error:
                        evidence[left_index].append({"code": "invalid_price", "message": str(error)})
            paired_left, paired_right = {}, {}
            for initial in range(len(left)):
                queue, parents, free = [initial], {}, None
                for left_index in queue:
                    for right_index in candidates[left_index]:
                        if right_index in parents:
                            continue
                        parents[right_index] = left_index
                        if right_index not in paired_right:
                            free = right_index
                            break
                        queue.append(paired_right[right_index])
                    if free is not None:
                        break
                while free is not None:
                    left_index = parents[free]
                    previous = paired_left.get(left_index)
                    paired_left[left_index] = free
                    paired_right[free] = left_index
                    free = previous
            report["matched"][kind] += len(paired_left)
            for left_index, expected in enumerate(left):
                if left_index not in paired_left:
                    report["missing"].append({"kind": kind, "key": list(key), "row": expected})
                    issues.append({"code": "price_mismatch", "kind": kind, "key": list(key), "evidence": evidence[left_index]})
            report["unexpected"].extend({"kind": kind, "key": list(key), "row": row}
                                        for index, row in enumerate(right) if index not in paired_right)
    report["pass"] = not (report["missing"] or report["unexpected"] or issues)
    return report


def compare_snapshots(baseline: dict, actual: dict, **options) -> dict:
    # Default Decimal precision can erase material differences in large prices.
    def longest(value):
        if isinstance(value, dict): return max([0, *[longest(item) for item in value.values()]])
        if isinstance(value, list): return max([0, *[longest(item) for item in value]])
        return len(str(value)) if value is not None else 0
    with localcontext() as context:
        context.prec = max(100, longest([baseline, actual, options]) + 80)
        def expanded(snapshot):
            result = dict(snapshot)
            for key in ("official_prices", "quotes"):
                result[key] = [expand_price_payload(dict(row), allow_legacy=True)
                               for row in snapshot.get(key, [])]
            return result
        return _compare_snapshots(expanded(baseline), expanded(actual), **options)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--actual", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--identity-map", type=Path, help="JSON object: actual model ID -> baseline model ID")
    parser.add_argument("--pricing-context", type=Path, help="Explicit shared FX/factor snapshot with revision and evidence")
    parser.add_argument("--amount-tolerance", default="1e-18", help="Absolute tolerance after unit_quantity normalization")
    parser.add_argument("--rate-tolerance", default="1e-12")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output in {args.baseline.resolve(), args.actual.resolve()}:
        parser.error("output must not overwrite an input database")
    try:
        identity_map = json.loads(args.identity_map.read_text()) if args.identity_map else None
        if identity_map is not None and (not isinstance(identity_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in identity_map.items())):
            raise ValueError("identity-map must map actual model IDs to baseline model IDs")
        context = json.loads(args.pricing_context.read_text()) if args.pricing_context else None
        report = compare_snapshots(read_snapshot(args.baseline), read_snapshot(args.actual), identity_map=identity_map,
                                   amount_tolerance=args.amount_tolerance, rate_tolerance=args.rate_tolerance, pricing_context=context)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError, InvalidOperation) as error:
        report = {"pass": False, "missing": [], "unexpected": [], "issues": [{"code": "comparison_error", "message": str(error)}]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"pass": report["pass"], "missing": len(report["missing"]), "unexpected": len(report["unexpected"]),
                      "issues": len(report["issues"]), "output": str(output)}))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
