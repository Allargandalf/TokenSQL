"""Structured prices across draft, review, clarification and immutable legacy receipts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.draft_cli import prepare
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.schemas import PricingBundle
from tokensql.workspace.store import IdempotencyConflictError, RevisionConflictError, WorkspaceStore
from tokensql.workspace.workflow import PRICING_POLICY_VERSION


def manager_for(tmp_path, handler=None):
    config = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                         python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "agent",
                         input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                         data_dir=tmp_path / "data")
    store = WorkspaceStore(config.database_path)
    transport = httpx.MockTransport(handler) if handler else None
    return OpenCodeAgentManager(config=config, store=store, transport=transport), store


def official_rows():
    specifications = [
        ("Input", "input", "3", {"input_tokens": {"max": 200000, "max_inclusive": True}}),
        ("Output", "output", "15", {"output_modality": "text"}),
        ("Cache Read", "cache_read", "0.3", {}),
        ("Cache Write 5m", "cache_write", "3.75", {"cache_ttl_seconds": 300}),
        ("Cache Write 1h", "cache_write", "6", {"cache_ttl_seconds": 3600}),
    ]
    return [{"model_id": "claude-test", "billing_item": label, "billing_metric": metric,
             "amount": amount, "pricing_unit_id": "usd_mtoken", "explicit_official": True,
             "pricing_conditions": {"market": "international", "platform": "original", **conditions},
             "source": "synthetic.xlsx", "source_ref": f"Official!A{index}:G{index}; headers A1:G1"}
            for index, (label, metric, amount, conditions) in enumerate(specifications, 2)]


def test_schema_to_draft_review_confirm_keeps_five_prices_and_one_model_discount(tmp_path):
    manager, store = manager_for(tmp_path)
    source = {"official_prices": official_rows(), "create": [
        {"model_id": "claude-test", "supplier": "Lab", "discount_rate": "0.70",
         "discount_scope": "model", "conditions": {"折扣口径": "原厂定价"},
         "source": "synthetic.xlsx", "source_ref": "Discount!A2:C2; header C1=原厂折扣"}]}
    schema = PricingBundle.model_json_schema()
    for field in ("official_price_id", "billing_metric", "pricing_conditions", "discount_scope",
                  "discount_items", "input_tokens", "max_inclusive"):
        assert field in json.dumps(schema)
    prepared = prepare(source, store.catalog_snapshot())
    assert len(prepared["bundle"]["official_prices"]) == 5
    assert prepared["bundle"]["official_prices"][0]["billing_item"] == "Input"
    assert prepared["bundle"]["create"][0]["amount"] is None
    job = manager.workflow.create_draft(prepared["bundle"])
    assert job["pricing_policy_version"] == PRICING_POLICY_VERSION
    projected = job["preview"]["items"][0]
    assert projected["official_price"] is None
    assert len(projected["official_prices"]) == 5
    assert projected["official_match"]["status"] == "multiple"
    assert projected["derived_amount"] is None
    assert store.catalog_snapshot()["quotes"] == []
    confirmed = asyncio.run(manager.workflow.confirm(job["id"], job["revision"]))
    snapshot = store.catalog_snapshot()
    assert confirmed["status"] == "confirmed"
    assert len(snapshot["models"]) == len(snapshot["quotes"]) == 1
    assert len(snapshot["official_prices"]) == 5
    assert len({price["official_price_id"] for price in snapshot["official_prices"]}) == 5
    row = snapshot["quotes"][0]
    assert row["discount_scope"] == "model" and row["discount_rate"] == "0.70"
    assert row["amount"] is None and row["conditions"] == source["create"][0]["conditions"]
    assert row["source_ref"] == source["create"][0]["source_ref"]
    assert manager.memory.read()["rules"] == []
    replay = asyncio.run(manager.workflow.confirm(job["id"], job["revision"]))
    assert replay["changes"] == confirmed["changes"] and len(store.catalog_snapshot()["quotes"]) == 1
    store.close()


def test_unknown_scope_is_preserved_until_a_human_review_correction(tmp_path):
    manager, store = manager_for(tmp_path)
    store.apply_bundle({"official_prices": official_rows()})
    job = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "claude-test", "discount_rate": "0.70"}]})
    projected = job["preview"]["items"][0]
    assert projected["official_match"]["status"] == "needs_scope"
    assert len(projected["official_match"]["candidate_ids"]) == 5 and projected["official_prices"] == []
    revised = deepcopy(job["bundle"])
    revised["create"][0]["discount_scope"] = "items"
    selected = store.catalog_snapshot()["official_prices"][0]
    revised["create"][0]["discount_items"] = [{"official_price_id": selected["official_price_id"]}]
    review = asyncio.run(manager.workflow.revise(job["id"], job["revision"], revised))
    assert review["preview"]["items"][0]["official_match"]["status"] == "matched"
    assert manager.memory.read()["rules"] == []
    asyncio.run(manager.workflow.confirm(job["id"], review["revision"]))
    rule = manager.memory.read()["rules"][0]
    assert rule["source"] == "confirmed_review" and '"discount_scope"' in rule["text"]
    assert "official_price_id" in rule["text"]
    store.close()


def test_scope_clarification_survives_restart_and_uses_same_session_fresh_attempt(tmp_path):
    async def scenario():
        manager = None
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_scope"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)
            directory = Path(re.search(r"Use only this run directory for writable output: (.+)", body["system"])[1])
            if "Current attempt: 1" in body["parts"][0]["text"]:
                atomic_json(directory / "needs_input.json",
                            {"questions": ["七折适用整个模型原厂全部计费项，还是指定条目？"],
                             "summary": "已有五项官网价，原行没有明确适用范围。"})
                text = "请确认七折范围。"
            else:
                assert "整个模型原厂全部计费项" in body["parts"][0]["text"]
                atomic_json(directory / "proposal.json", {"bundle": {
                    "create": [{"supplier": "Synthetic Supplier", "model_id": "claude-test", "discount_rate": "0.70", "discount_scope": "model",
                                "source": "synthetic.xlsx", "source_ref": "Sheet1!A2:B2"}]}})
                text = "已按明确答复准备整模折扣草案，等待审核。"
            return httpx.Response(200, json={"info": {"role": "assistant"},
                                           "parts": [{"type": "text", "text": text}]})
        manager, store = manager_for(tmp_path, handler)
        store.apply_bundle({"official_prices": official_rows()})
        raw = b"immutable synthetic workbook"
        run = await manager.submit(prompt="按七折与官网价计算，若范围不明确请反问",
                                   files=[AgentUpload("synthetic.xlsx", raw)])
        assert (await manager.wait(run.id)).status == "waiting_for_input"
        waiting = manager.workflow.get(run.id)
        assert waiting["preview"] is None and store.catalog_snapshot()["quotes"] == []
        assert manager.memory.read()["rules"] == []
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        assert restored.workflow.get(run.id)["questions"] == waiting["questions"]
        with pytest.raises(RevisionConflictError):
            await restored.workflow.respond(run.id, waiting["revision"] - 1, "整个模型")
        # The mock writes only the current attempt's artifacts after the restart.
        manager = restored
        await restored.workflow.respond(run.id, waiting["revision"], "七折适用整个模型原厂全部计费项")
        ready = await restored.wait(run.id)
        assert ready.status == "ready" and ready.attempt == 2 and ready.conversation_id == "ses_scope"
        job = restored.workflow.get(run.id)
        assert len(job["preview"]["items"][0]["official_prices"]) == 5
        root = restored.config.agent_dir / "runs" / run.id
        assert (root / "needs_input.json").exists()
        assert (root / "attempts" / "2" / "proposal.json").exists()
        assert len([call for call in calls if call.url.path == "/session" and call.method == "POST"]) == 1
        item = restored.workflow.state["uploads"][job["upload_ids"][0]]
        assert restored.workflow._location(item).read_bytes() == raw
        assert item["sha256"] == hashlib.sha256(raw).hexdigest()
        assert restored.memory.read()["rules"] == []
        await restored.workflow.confirm(run.id, job["revision"])
        assert len(store.catalog_snapshot()["quotes"]) == 1
        assert restored.memory.read()["rules"] == []
        store.close()
    asyncio.run(scenario())


def test_legacy_ready_policy_refresh_is_once_only_and_never_guesses_scope_or_memory(tmp_path):
    manager, store = manager_for(tmp_path)
    store.apply_bundle({"official_prices": official_rows()})
    raw = {"create": [{"supplier": "Synthetic Supplier", "model_id": "claude-test", "discount_rate": "0.70",
                        "conditions": {"tier": "source literal"}, "note": "unresolved scope"}]}
    job = manager.workflow.create_draft(raw)
    internal = manager.workflow._job(job["id"])
    internal.pop("pricing_policy_version")
    internal.update(assistant_message="Source literal reply",
                    messages=[{"role": "assistant", "text": "Source literal reply"}])
    manager.workflow._save()
    memory = manager.memory.read()
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    upgraded = restored.workflow.get(job["id"])
    assert upgraded["revision"] == job["revision"] + 1 and upgraded["status"] == "ready"
    assert upgraded["bundle"] == raw and restored.workflow._job(job["id"])["original_bundle"] == raw
    assert upgraded["preview"]["items"][0]["official_match"]["status"] == "needs_scope"
    assert upgraded["messages"] == internal["messages"] and upgraded["assistant_message"] == internal["assistant_message"]
    assert restored.memory.read() == memory and store.catalog_snapshot()["quotes"] == []
    again = OpenCodeAgentManager(config=manager.config, store=store)
    assert again.workflow.get(job["id"]) == upgraded
    with pytest.raises(ValueError, match="revision conflict"):
        asyncio.run(again.workflow.confirm(job["id"], job["revision"]))
    asyncio.run(again.workflow.confirm(job["id"], upgraded["revision"]))
    assert store.catalog_snapshot()["quotes"][0]["discount_scope"] == "unknown"
    assert again.memory.read()["rules"] == []
    store.close()


def test_policy_upgrade_never_rewrites_historical_or_inflight_artifacts(tmp_path):
    manager, store = manager_for(tmp_path)
    job = manager.workflow.create_draft({})
    seed = deepcopy(manager.workflow._job(job["id"]))
    seed["bundle"] = {"models": [{"model_id": "m", "physical_key": "historical literal"}]}
    seed["original_bundle"] = deepcopy(seed["bundle"])
    seed["commit_result"] = {"catalog": {"models": [{"model_id": "m", "physical_key": "receipt literal"}]}}
    seed.pop("pricing_policy_version")
    seed.pop("discount_policy_version")
    states = {}
    for status in ("confirmed", "committed", "confirming", "failed", "cancelled", "waiting_for_input"):
        record = {**deepcopy(seed), "id": status, "status": status}
        manager.workflow.state["jobs"][status] = record
        states[status] = deepcopy(record)
    manager.workflow.state.pop("model_id_schema_version")
    manager.workflow._save()
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    for identifier, expected in states.items():
        assert restored.workflow._job(identifier) == expected
    assert restored.memory.read()["rules"] == [] and store.catalog_snapshot()["quotes"] == []
    store.close()


def test_invalid_legacy_conditions_require_manual_revision_then_clear_warning(tmp_path):
    manager, store = manager_for(tmp_path)
    job = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m"}]})
    internal = manager.workflow._job(job["id"])
    internal.pop("pricing_policy_version")
    internal["bundle"]["create"][0]["pricing_conditions"] = {"input_tokens": {"min": 20, "max": 10}}
    manager.workflow._save()
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    upgraded = restored.workflow.get(job["id"])
    assert upgraded["pricing_policy_warning"] and upgraded["preview"] is None
    assert upgraded["revision"] == job["revision"] + 1
    assert OpenCodeAgentManager(config=manager.config, store=store).workflow.get(job["id"]) == upgraded
    with pytest.raises(ValueError, match="manual revision"):
        asyncio.run(restored.workflow.confirm(job["id"], upgraded["revision"]))
    revised = asyncio.run(restored.workflow.revise(job["id"], upgraded["revision"],
                                                  {"create": [{"supplier": "Synthetic Supplier", "model_id": "m"}]}))
    assert "pricing_policy_warning" not in revised and store.catalog_snapshot()["quotes"] == []
    store.close()


def legacy_fingerprint(bundle):
    serialized = PricingBundle.model_validate(bundle).model_dump(mode="json")
    fields = {"official_price_id", "billing_metric", "pricing_conditions", "discount_scope", "discount_items"}
    for section in ("official_prices", "create"):
        for item in serialized[section]:
            for field in fields:
                item.pop(field, None)
            if section == "official_prices":
                item.pop("conditions", None)
            if item.get("billing_item") is not None:
                item["billing_item"] = item["billing_item"].casefold()
    text = json.dumps(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def test_old_confirming_receipt_recovers_exact_fingerprint_without_accepting_changed_semantics(tmp_path):
    manager, store = manager_for(tmp_path)
    raw = {"create": [{"supplier": "Synthetic Supplier", "model_id": "legacy", "billing_item": "input", "discount_rate": "0.70"}]}
    job = manager.workflow.create_draft(raw)
    identifier = "review:" + job["id"]
    store.apply_bundle(raw, import_id=identifier)
    store._conn.execute("UPDATE pricing_import SET payload_sha256=? WHERE import_id=?",
                        (legacy_fingerprint(raw), identifier))
    receipt = dict(store._conn.execute("SELECT * FROM pricing_import WHERE import_id=?", (identifier,)).fetchone())
    changed = deepcopy(raw)
    changed["create"][0]["discount_scope"] = "model"
    with pytest.raises(IdempotencyConflictError):
        store.apply_bundle(changed, import_id=identifier)
    changed = deepcopy(raw)
    changed["create"][0]["discount_rate"] = "0.60"
    with pytest.raises(IdempotencyConflictError):
        store.apply_bundle(changed, import_id=identifier)
    internal = manager.workflow._job(job["id"])
    internal.update(status="confirming", confirmation_key=job["id"])
    internal.pop("pricing_policy_version")
    manager.workflow._save()
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    assert restored.workflow._job(job["id"])["bundle"] == raw
    assert "pricing_policy_version" not in restored.workflow._job(job["id"])
    asyncio.run(restored.recover())
    confirmed = restored.workflow.get(job["id"])
    assert confirmed["status"] == "confirmed" and len(store.catalog_snapshot()["quotes"]) == 1
    assert dict(store._conn.execute("SELECT * FROM pricing_import WHERE import_id=?", (identifier,)).fetchone()) == receipt
    assert restored.memory.read()["rules"] == []
    asyncio.run(restored.workflow.confirm(job["id"], job["revision"]))
    assert len(store.catalog_snapshot()["quotes"]) == 1
    store.close()


def test_official_lookup_policy_preserves_typed_conditions_and_original_item_labels(tmp_path):
    manager, _ = manager_for(tmp_path)
    prompt = manager._system_prompt(Path("/run"), Path("/catalog.json"))
    lookup = manager._lookup_prompt({"intent": "official_price_lookup", "lookup_model_id": "m",
                                     "lookup_billing_item": None}, Path("/catalog.json"))
    for token in ("billing_metric", "pricing_conditions", "discount_scope", "discount_items",
                  "official_price_id", "min_inclusive", "max_inclusive", "mtoken_hour", "account_month"):
        assert token in prompt
    assert "notes alone cannot carry applicable conditions" in lookup
    assert "pricing_conditions.market/customer_tier" in lookup
    assert "same model_id" in lookup
    assert "Keep conditions and ambiguity in note/issues" not in lookup

def test_prompts_separate_price_applicability_conditions_from_provenance(tmp_path):
    manager, store = manager_for(tmp_path)
    system = manager._system_prompt(Path("/run"), Path("/catalog.json"))
    lookup = manager._lookup_prompt({"intent": "official_price_lookup", "lookup_model_id": "m",
                                     "lookup_billing_item": None}, Path("/catalog.json"))
    for token in ("header_cell", "header_text", "currency_cell", "unit_cell", "source_ref",
                  "raw={}", "cache_ttl_seconds", "without a duplicate TTL literal"):
        assert token in system
    assert "only for unstructured conditions that affect which price applies" in system
    assert "If typed fields are complete, raw must be {} or omitted" in lookup
    assert "provenance, header/cell references, currency/unit evidence and already-typed TTL must not go there" in lookup
    # Trusted prompts guide extraction; unknown original business values are never scrubbed.
    raw = {"header_cell": "A1", "unknown_pricing_term": "literal business text"}
    prepared = prepare({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "pricing_conditions": {"raw": raw}}]},
                       store.catalog_snapshot())
    assert prepared["bundle"]["create"][0]["pricing_conditions"]["raw"] == raw
    store.close()

def test_quote_structural_defaults_do_not_become_confirmed_corrections(tmp_path):
    from tokensql.workspace.memory import AgentMemory
    memory = AgentMemory(tmp_path / "memory")
    original = {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.70"}]}
    revised = deepcopy(original)
    revised["create"][0].update(billing_metric=None, pricing_conditions={}, discount_scope="unknown",
                                discount_items=[], conditions={})
    memory.remember_confirmed("default-only", original, revised, ["synthetic.xlsx"])
    assert memory.read()["rules"] == []
    memory.remember_confirmed("default-reverse", revised, original, ["synthetic.xlsx"])
    assert memory.read()["rules"] == []
    changed = deepcopy(revised)
    changed["create"][0]["discount_scope"] = "model"
    memory.remember_confirmed("human-scope", revised, changed, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 1
    assert "discount_scope" in memory.read()["rules"][0]["text"]
    changed["create"][0]["conditions"] = {"discount_scope": "unknown"}
    corrected = deepcopy(changed)
    corrected["create"][0]["conditions"]["discount_scope"] = None
    memory.remember_confirmed("human-raw-literal", changed, corrected, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 2


def test_nested_selector_and_patch_defaults_are_not_remembered_but_real_conditions_are(tmp_path):
    from tokensql.workspace.memory import AgentMemory
    memory = AgentMemory(tmp_path / "memory")
    original = {"update": [{"id": 1, "expected_revision": 1, "changes": {
        "discount_items": [{"official_price_id": "op-known"}],
        "pricing_conditions": {"input_tokens": {"max": 100}}}}]}
    revised = deepcopy(original)
    changes = revised["update"][0]["changes"]
    changes.update(discount_scope="unknown")
    changes["discount_items"][0].update(billing_metric=None, pricing_conditions={"raw": {}})
    changes["pricing_conditions"]["input_tokens"].update(min=None, min_inclusive=True, max_inclusive=True)
    memory.remember_confirmed("patch-defaults", original, revised, ["synthetic.xlsx"])
    assert memory.read()["rules"] == []
    amended = deepcopy(revised)
    amended["update"][0]["changes"]["pricing_conditions"]["input_tokens"]["max_inclusive"] = False
    memory.remember_confirmed("human-boundary", revised, amended, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 1


def test_official_absent_to_empty_typed_conditions_is_real_correction_when_raw_scope_changes(tmp_path):
    from tokensql.workspace.memory import AgentMemory
    memory = AgentMemory(tmp_path / "memory")
    original = {"official_prices": [{"model_id": "m", "conditions": {"tier": "old raw"}}]}
    revised = deepcopy(original)
    revised["official_prices"][0]["pricing_conditions"] = {}
    memory.remember_confirmed("human-evidence-only", original, revised, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 1
    plain = {"official_prices": [{"model_id": "m"}]}
    defaulted = deepcopy(plain)
    defaulted["official_prices"][0].update(pricing_conditions={}, conditions={}, billing_metric=None)
    memory.remember_confirmed("official-default-only", plain, defaulted, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 1
