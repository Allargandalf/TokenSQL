"""Source content reuse and frozen confirmed baselines; never position-to-price rules."""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
import json
from typing import Any
from .memory import supplier_profile_context, _supplier_id
from .schemas import PricingBundle, SupplierSourceProfile, OfficialPriceCreate
from .pricing_conditions import scenario_key
from .supplier_profiles import profile_meaning_fingerprint
from .table_diff import PARSER_VERSION, build_reuse_plan, compare_snapshots, prompt_fingerprint, read_workbooks


def _empty_profile() -> dict:
    return {"model_mappings": [], "rules": [], "default_prompt": None}


def source_prompt_fingerprint(workflow: Any, job: dict) -> str:
    # Persisted answers move into the confirmed profile; batch-only answers must
    # still invalidate a later batch that does not include those instructions.
    answers = [{"questions": item.get("questions", []), "answer": item.get("prompt", "")}
               for item in job.get("clarifications", [])
               if not (job.get("persist_profile", True) and item.get("persist_profile", True))]
    rules = [{"text": rule["text"], "scope": rule.get("scope", "workspace")}
             for rule in workflow.memory.read()["rules"] if rule.get("enabled", True)
             and rule.get("scope", "workspace") in {"workspace", "global"}]
    return prompt_fingerprint(json.dumps({"prompt": job["prompt"].strip(), "answers": answers,
        "global_rules": rules, "administrator": workflow.config.instructions}, ensure_ascii=False, sort_keys=True))


def prepare_source_reading(workflow: Any, identifier: str) -> dict | None:
    job = workflow._job(identifier)
    if job.get("intent") == "official_price_lookup" or not job.get("upload_ids"):
        return None
    scope, sid = job.get("source_scope") or "default", job.get("supplier_id")
    context = supplier_profile_context(workflow.store, sid, scope)
    files = [{"path": workflow._location(workflow.state["uploads"][key]),
              "filename": workflow.state["uploads"][key]["filename"],
              "sha256": workflow.state["uploads"][key].get("sha256")}
             for key in job["upload_ids"]]
    snapshot = read_workbooks(files)
    baseline = workflow.store.get_supplier_baseline(sid, scope) if sid else None
    old = baseline["snapshot"] if baseline else {"version": 1, "files": [], "supported": True}
    comparison = compare_snapshots(old, snapshot)
    profile_fingerprint = context["fingerprint"] if context else profile_meaning_fingerprint(_empty_profile())
    requested = source_prompt_fingerprint(workflow, job)
    plan = build_reuse_plan(baseline, snapshot, comparison,
        profile_fingerprint=profile_fingerprint, prompt_fingerprint=requested,
        force_full=bool(job.get("full_reparse") or job.get("clarifications")))
    if not snapshot.get("supported"):
        plan.update(needs_model=True, reuse_mode="none", changed_context=None,
                    reused_bundle={"models": [], "official_prices": [], "create": [], "update": [], "delete": []},
                    reason="source_reader_requires_full_model")
    public_plan = {key: deepcopy(value) for key, value in plan.items()
                   if key not in {"reused_bundle", "changed_context", "comparison"}}
    workflow.update_job(identifier, profile_context=context, source_comparison=comparison,
        reading_plan=public_plan, _source_snapshot=snapshot, _source_reused_bundle=plan["reused_bundle"],
        _source_changed_context=plan.get("changed_context"), _source_prompt_fingerprint=requested,
        _source_prepared_attempt=job.get("attempt", 1),
        baseline_revision_used=baseline["revision"] if baseline else None)
    return plan


def merge_source_bundle(job: dict, candidate: dict) -> dict:
    reused = job.get("_source_reused_bundle") or {}
    result = {key: deepcopy(reused.get(key, [])) + deepcopy(candidate.get(key, []))
              for key in ("models", "official_prices", "create", "update", "delete")}
    # Metadata is entity-level: changed-region observations supersede reused
    # metadata for that same explicit canonical identity. Quote rows stay distinct.
    models = {}
    for model in result["models"]:
        models[model["model_id"]] = {**models.get(model["model_id"], {}), **model}
    result["models"] = list(models.values())
    return result


def source_coverage(job: dict, observations: dict | None) -> dict | None:
    plan = job.get("reading_plan")
    if not plan:
        return observations
    result = deepcopy(observations or {})
    coverage = plan.get("coverage", {})
    if not plan.get("needs_model"):
        result.update(coverage)
    elif plan.get("reuse_mode") == "partial":
        changed = job.get("_source_changed_context") or {}
        required = {region["new_sheet"] for region in changed.get("regions", [])
                    if any(row.get("parse") for row in region.get("new_rows", []))}
        reported = set(result.get("covered_sheets", []))
        result["complete_read"] = bool(result.get("complete_read")) and required.issubset(reported)
        if result["complete_read"]:
            result["covered_sheets"] = coverage.get("covered_sheets", [])
    return result


def freeze_source_baseline(workflow: Any, job: dict, previews: dict[str, dict]) -> None:
    job.pop("baseline_update", None)
    snapshot = job.get("_source_snapshot")
    if not snapshot or not snapshot.get("supported"):
        return
    coverage = job.get("source_read_coverage") or {}
    inventory = {sheet["name"] for file in snapshot["files"] for sheet in file["sheets"]}
    if (not coverage.get("complete_read") or coverage.get("unresolved_items", 0)
            or not inventory.issubset(set(coverage.get("covered_sheets", [])))):
        return  # An incomplete read must never become a complete zero-call baseline.
    bundle = deepcopy(job.get("proposal_bundle") or job.get("bundle") or {})
    if bundle.get("update") or bundle.get("delete"):
        return  # A direct operation recipe is not a full source interpretation.
    targets = {update["supplier_id"] for update in job.get("profile_updates", [])}
    if not targets and job.get("supplier_id"):
        targets.add(job["supplier_id"])
    if len(targets) != 1:
        return  # A mixed-supplier generic file has no unambiguous supplier baseline.
    sid = next(iter(targets))
    scope = job.get("source_scope") or "default"
    used = job.get("profile_context") or {}
    used_fingerprint = used.get("fingerprint") or profile_meaning_fingerprint(_empty_profile())
    interpreted_fingerprint, interpreted_revision = used_fingerprint, used.get("revision", 0)
    preview = previews.get(sid)
    if preview and preview["before"]["effective_fingerprint"] == used_fingerprint:
        interpreted_fingerprint = preview["after"]["effective_fingerprint"]
        interpreted_revision = preview["after"]["base_revision"] + (0 if preview["after"].get("already_recorded") else 1)
    elif sid.startswith("pending-name:"):
        update = next(item for item in job["profile_updates"] if item["supplier_id"] == sid)
        meaning = SupplierSourceProfile.model_validate({"model_mappings": update["model_mappings"],
            "rules": update["rules"], "default_prompt": update["default_prompt"]}).model_dump(mode="json")
        interpreted_fingerprint, interpreted_revision = profile_meaning_fingerprint(meaning), 1
    refs = [{"upload_id": key, "filename": workflow.state["uploads"][key]["filename"],
             "sha256": workflow.state["uploads"][key].get("sha256"),
             "archive_path": str(workflow.data_dir / job["id"] / workflow.state["uploads"][key]["stored_name"])}
            for key in job["upload_ids"]]
    current_baseline = None if sid.startswith("pending-name:") else workflow.store.get_supplier_baseline(sid, scope)
    final_prompt = source_prompt_fingerprint(workflow, job)
    used_prompt = job.get("_source_prompt_fingerprint") or final_prompt
    if final_prompt != used_prompt:
        # Global rules or final persistence choices changed after interpretation.
        # Keep that distinction rather than stamp old output as the new policy.
        final_prompt = prompt_fingerprint(json.dumps({"interpreted": used_prompt,
            "confirmed": final_prompt, "next_import_requires_interpretation": True}, sort_keys=True))
    job["baseline_update"] = {"supplier_id": sid, "source_scope": scope, "import_id": job["id"],
        "expected_revision": current_baseline["revision"] if current_baseline else 0,
        "payload": {"snapshot": deepcopy(snapshot), "bundle": PricingBundle.model_validate(bundle).model_dump(mode="json"),
            "file_refs": refs, "profile_revision": interpreted_revision,
            "profile_fingerprint": interpreted_fingerprint,
            "prompt_fingerprint": final_prompt,
            "parser_version": PARSER_VERSION, "coverage": deepcopy(coverage)}}


def record_source_baseline(workflow: Any, job: dict) -> None:
    if not job.get("baseline_update"):
        return
    update = deepcopy(job["baseline_update"])
    if update["supplier_id"].startswith("pending-name:"):
        name = update["supplier_id"][13:]
        update["supplier_id"] = _supplier_id(workflow.store, {"supplier": name})
        if not update["supplier_id"]:
            raise ValueError("confirmed baseline is awaiting its committed supplier")
    from .store import RevisionConflictError
    try:
        workflow.store.record_supplier_baseline(**update)
    except RevisionConflictError:
        latest = workflow.store.get_supplier_baseline(update["supplier_id"], update["source_scope"])
        if latest and latest["revision"] > update.get("expected_revision", 0):
            job["baseline_status"] = "superseded"
            job["baseline_superseded_by"] = latest["import_id"]
            return
        raise
    job["baseline_status"] = "recorded"


def rebind_source_officials(workflow: Any, job: dict, bundle: dict) -> dict:
    """Use the fresh catalog only for exact scenario identity and optimistic revision.

    Changed observed prices remain explicit human-reviewed proposals. Existing
    canonical values are never overwritten while preparing this view.
    """
    result = deepcopy(bundle)
    if not job.get("upload_ids") or job.get("intent") == "official_price_lookup":
        return result
    catalog = workflow.store.catalog_snapshot().get("official_prices", [])
    reviews, unchanged = [], []
    business = ("model_id", "billing_item", "billing_metric", "pricing_conditions", "conditions", "amount",
                "pricing_unit_id", "currency", "unit", "unit_quantity", "effective_date")
    for row in result.get("official_prices", []):
        canonical = workflow.store._official_payload(OfficialPriceCreate.model_validate(row))
        if row.get("official_price_id"):
            matches = [item for item in catalog if item["official_price_id"] == row["official_price_id"]]
            if matches and (matches[0]["model_id"] != row["model_id"] or scenario_key(matches[0]) != scenario_key(canonical)):
                raise ValueError("official source scenario changed in the catalog; re-read or revise its exact official_price_id")
        else:
            matches = [item for item in catalog if item["model_id"] == row["model_id"]
                       and item.get("billing_item") == row.get("billing_item")
                       and scenario_key(item) == scenario_key(canonical)]
        if len(matches) > 1:
            raise ValueError("official source scenario is ambiguous; review the exact official_price_id")
        if matches:
            current = matches[0]
            row.update(official_price_id=current["official_price_id"], expected_revision=current["revision"])
            current_fields = {key: value for key, value in current.items() if key in OfficialPriceCreate.model_fields}
            current_canonical = workflow.store._official_payload(OfficialPriceCreate.model_validate(current_fields))
            def equivalent(field: str) -> bool:
                left, right = canonical.get(field), current_canonical.get(field)
                if field in {"amount", "unit_quantity"} and left is not None and right is not None:
                    return Decimal(str(left)) == Decimal(str(right))
                return left == right
            changes = [field for field in business if not equivalent(field)]
            if changes:
                reviews.append({"official_price_id": current["official_price_id"], "before": deepcopy(current),
                                "after": deepcopy(row), "changed_fields": changes})
            else:
                unchanged.append(current["official_price_id"])
    job["_source_unchanged_official_ids"] = unchanged
    job["official_catalog_changes"] = reviews
    return result


def public_source_comparison(comparison: dict) -> dict:
    value = deepcopy(comparison)
    for item in value.get("groups", {}).get("presentation", []):
        left = {cell["column"]: cell for cell in (item.get("before") or {}).get("cells", [])}
        right = {cell["column"]: cell for cell in (item.get("after") or {}).get("cells", [])}
        if "presentation_columns" not in item:
            item["presentation_columns"] = sorted(column for column in left.keys() | right.keys()
                if left.get(column, {}).get("style") != right.get(column, {}).get("style"))
    omitted = {"style", "semantic_key", "text_key", "semantic_fingerprint", "presentation_fingerprint", "structure_fingerprint"}
    def compact(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: compact(part) for key, part in item.items() if key not in omitted}
        if isinstance(item, list):
            return [compact(part) for part in item]
        return item
    return compact(value)
