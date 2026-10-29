"""Supplier profile scope and confirmation lifecycle acceptance with isolated data."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from tokensql.workspace.agent import OpenCodeAgentManager
from tokensql.workspace.memory import atomic_json, supplier_profile_context, supplier_profile_prompt
from tests.test_workspace_agent import mocked


def supplier(store, name):
    return store.confirm_supplier({"name": name})["supplier_id"]


def seed_profile(store, sid, scope, text):
    profile = store.get_supplier_profile(sid)
    sources = deepcopy(profile["sources"])
    sources[scope] = {"rules": [{"text": text}], "model_mappings": [], "default_prompt": "default " + text}
    return store.update_supplier_profile(sid, {"expected_revision": profile["revision"], "sources": sources})


def test_supplier_and_series_context_never_load_other_profile(tmp_path):
    manager, store, requests = mocked(tmp_path)
    a, b = supplier(store, "A"), supplier(store, "B")
    seed_profile(store, a, "api", "A API meaning")
    seed_profile(store, a, "channel", "A CHANNEL secret")
    seed_profile(store, b, "api", "B API secret")
    async def scenario():
        run = await manager.submit(prompt="Current batch", supplier_id=a, source_scope="api")
        await manager.wait(run.id)
        message = next(req for req in requests if req.method == "POST" and req.url.path.endswith("/message"))
        policy = json.loads(message.content)["system"]
        assert "A API meaning" in policy and "default A API meaning" in policy
        assert "A CHANNEL secret" not in policy and "B API secret" not in policy
        context = manager.workflow.get(run.id)["profile_context"]
        assert context["revision"] == 2 and context["source_scope"] == "api"
        assert context["fingerprint"] == store.get_supplier_profile(a, "api")["effective_fingerprint"]
        assert "A API meaning" not in manager.memory.agent_path.read_text()
    asyncio.run(scenario())


def test_confirm_answers_mapping_corrections_and_explicit_default_recover_once(tmp_path):
    manager, store, _ = mocked(tmp_path)
    sid = supplier(store, "Confirmed A")
    manager.schedule = lambda identifier: None
    async def scenario():
        job = await manager.workflow.start(upload_ids=[], prompt="Reuse input units", supplier_id=sid,
                                           source_scope="api", remember_prompt=True)
        manager.workflow.update_job(job["id"], status="running")
        manager.workflow.accept_questions(job["id"], {"questions": ["Unit?"]})
        waiting = manager.workflow.get(job["id"])
        await manager.workflow.respond(job["id"], waiting["revision"], "Million tokens")
        assert store.get_supplier_profile(sid)["revision"] == 0
        manager.workflow.accept_proposal(job["id"], {"create": [{"model_id": "raw-name", "supplier_id": sid,
                                                                  "supplier_model_id": "Channel Model", "amount": "1"}]})
        ready = manager.workflow.get(job["id"])
        revised = deepcopy(ready["bundle"])
        revised["create"][0]["model_id"] = "canonical-model"
        revised["create"][0]["pricing_unit_id"] = "cny_mtoken"
        ready = await manager.workflow.revise(job["id"], ready["revision"], revised)
        assert store.get_supplier_profile(sid)["revision"] == 0
        confirmed = await manager.workflow.confirm(job["id"], ready["revision"])
        assert confirmed["status"] == "confirmed"
        profile = store.get_supplier_profile(sid, "api")
        assert profile["effective"]["default_prompt"] == "Reuse input units"
        assert any(item["raw_name"] == "Channel Model" and item["model_id"] == "canonical-model"
                   for item in profile["effective"]["model_mappings"])
        assert "Million tokens" in json.dumps(profile["effective"]["rules"])
        assert "pricing_unit_id" in json.dumps(profile["effective"]["rules"])
        assert store.get_supplier_profile(sid, "other")["effective"]["rules"] == []
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await restored.recover()
        assert store.get_supplier_profile(sid)["revision"] == profile["revision"]
        await restored.workflow.confirm(job["id"], ready["revision"])
        assert store.get_supplier_profile(sid)["revision"] == profile["revision"]
    asyncio.run(scenario())


def test_batch_only_and_cancel_never_learn_business_preferences(tmp_path):
    manager, store, _ = mocked(tmp_path)
    sid = supplier(store, "Batch A")
    manager.schedule = lambda identifier: None
    async def scenario():
        job = await manager.workflow.start(upload_ids=[], prompt="Batch-only literal", supplier_id=sid,
                                           persist_profile=False)
        manager.workflow.update_job(job["id"], status="running")
        manager.workflow.accept_questions(job["id"], {"questions": ["Meaning?"]})
        job = manager.workflow.get(job["id"])
        await manager.workflow.respond(job["id"], job["revision"], "Batch answer")
        manager.workflow.accept_proposal(job["id"], {"create": [{"model_id": "m1", "supplier_id": sid}]})
        ready = manager.workflow.get(job["id"])
        revised = deepcopy(ready["bundle"])
        revised["create"][0]["note"] = "Batch correction"
        ready = await manager.workflow.revise(job["id"], ready["revision"], revised)
        await manager.workflow.confirm(job["id"], ready["revision"])
        profile = store.get_supplier_profile(sid, "default")
        assert profile["effective"]["rules"] == [] and profile["effective"]["model_mappings"] == []
        assert profile["effective"]["default_prompt"] is None
        new = await manager.workflow.start(upload_ids=[], prompt="Cancel preferences", supplier_id=sid,
                                           remember_prompt=True)
        await manager.workflow.cancel(new["id"])
        assert store.get_supplier_profile(sid)["revision"] == profile["revision"]
    asyncio.run(scenario())


def test_legacy_only_attributable_confirmed_memory_migrates_no_cross_scope(tmp_path):
    manager, store, _ = mocked(tmp_path)
    sid = supplier(store, "Legacy A")
    job = manager.workflow.create_draft({"create": [{"supplier_id": sid, "model_id": "m1"}]})
    asyncio.run(manager.workflow.confirm(job["id"], job["revision"]))
    memory = manager.memory.read()
    manager.memory.update([{"id": "legacy-source", "scope": "sources:same.xlsx", "text": "Attributed meaning",
                            "import_id": job["id"], "source": "confirmed_review"},
                           {"id": "unknown-source", "scope": "sources:same.xlsx", "text": "Unattributed secret"},
                           {"id": "global", "scope": "workspace", "text": "Global preference"}], memory["revision"])
    restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
    context = supplier_profile_context(store, sid)
    assert "Attributed meaning" in supplier_profile_prompt(context)
    assert "Unattributed secret" not in supplier_profile_prompt(context)
    assert "Attributed meaning" not in restored.memory.prompt()
    assert "Global preference" in restored.memory.prompt()
    revision = store.get_supplier_profile(sid)["revision"]
    OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
    assert store.get_supplier_profile(sid)["revision"] == revision


def test_volatile_price_edits_are_receipt_only_and_wrong_model_is_not_an_alias(tmp_path):
    manager, store, _ = mocked(tmp_path)
    sid = supplier(store, "Durable A")
    job = manager.workflow.create_draft({"create": [{"supplier_id": sid, "model_id": "assistant-mistake", "amount": "1", "discount_rate": "0.5"}]})
    revised = deepcopy(job["bundle"])
    revised["create"][0].update(model_id="canonical-model", amount="2", discount_rate="0.7")
    ready = asyncio.run(manager.workflow.revise(job["id"], job["revision"], revised))
    asyncio.run(manager.workflow.confirm(job["id"], ready["revision"]))
    profile = store.get_supplier_profile(sid, "default")["effective"]
    assert profile["model_mappings"] == []
    texts = json.dumps(profile["rules"])
    assert "model_id" in texts and '/amount' not in texts and '/discount_rate' not in texts
    assert store.catalog_snapshot()["quotes"][0]["amount"] == "2"


def test_profile_retry_replays_exact_frozen_precommit_interpretation(tmp_path, monkeypatch):
    manager, store, _ = mocked(tmp_path)
    sid = supplier(store, "Frozen A")
    job = manager.workflow.create_draft({"create": [{"supplier_id": sid, "model_id": "m1", "billing_item": "input"}]})
    revised = deepcopy(job["bundle"])
    revised["create"][0]["billing_item"] = "output"
    ready = asyncio.run(manager.workflow.revise(job["id"], job["revision"], revised))
    original = store.record_confirmed_supplier_profile
    def unavailable(*args, **kwargs):
        raise OSError("profile outage")
    monkeypatch.setattr(store, "record_confirmed_supplier_profile", unavailable)
    with pytest.raises(OSError):
        asyncio.run(manager.workflow.confirm(job["id"], ready["revision"]))
    internal = manager.workflow._job(job["id"])
    frozen = deepcopy(internal["profile_updates"])
    internal["bundle"]["create"][0]["billing_item"] = "malicious later mutation"
    manager.workflow._save()
    monkeypatch.setattr(store, "record_confirmed_supplier_profile", original)
    asyncio.run(manager.workflow.confirm(job["id"], ready["revision"]))
    assert "profile_updates" not in manager.workflow._job(job["id"])
    rules = json.dumps(store.get_supplier_profile(sid, "default")["effective"]["rules"])
    assert "output" in rules and "malicious later mutation" not in rules
