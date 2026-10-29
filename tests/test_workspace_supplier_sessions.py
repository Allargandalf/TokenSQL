"""Persistent per-supplier routing, waiting recovery, new rounds and measured usage."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tokensql.workspace.agent import OpenCodeAgentManager
from tokensql.workspace.agent_routes import create_agent_router
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore, RevisionConflictError
from tests.test_workspace_agent import config


def fixture(tmp_path):
    cfg = config(tmp_path)
    store = WorkspaceStore(cfg.database_path)
    calls, session_ids = [], []
    def handler(request):
        calls.append(request)
        if request.url.path == "/session" and request.method == "POST":
            sid = "ses_supplier_" + str(len(session_ids) + 1)
            session_ids.append(sid)
            return httpx.Response(200, json={"id": sid})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        if request.url.path.endswith("/message"):
            job = next(job for job in manager.workflow.state["jobs"].values()
                       if job["status"] == "running" and job["conversation_id"] in request.url.path)
            root = cfg.agent_dir / "runs" / job["id"]
            directory = root if job["attempt"] == 1 else root / "attempts" / str(job["attempt"])
            atomic_json(directory / "proposal.json", {"bundle": {"create": [{"model_id": "m1", "supplier_id": job["supplier_id"]}]}})
            return httpx.Response(200, json={"info": {"role": "assistant", "tokens": {"input": 123, "output": 45, "cache": {"read": 8, "write": 0}}},
                                            "parts": [{"type": "text", "text": "Ready"}]})
        raise AssertionError(str(request.url))
    manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
    a = store.confirm_supplier({"name": "Supplier A"})["supplier_id"]
    b = store.confirm_supplier({"name": "Supplier B"})["supplier_id"]
    return manager, store, a, b, calls


def test_default_routes_persist_distinct_suppliers_and_concurrent_open(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        first, same = await asyncio.gather(manager.supplier_session(a), manager.supplier_session(a))
        other = await manager.supplier_session(b)
        assert first["conversation_id"] == same["conversation_id"]
        assert first["conversation_id"] != other["conversation_id"]
        assert first["title"] == "Supplier A"
        assert len([r for r in calls if r.url.path == "/session" and r.method == "POST"]) == 2
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await restored.recover()
        assert (await restored.supplier_session(a))["conversation_id"] == first["conversation_id"]
        assert (await restored.supplier_session(b))["conversation_id"] == other["conversation_id"]
    asyncio.run(scenario())


def test_switch_restores_waiting_same_job_after_restart_and_blocks_new_round(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        route = await manager.supplier_session(a)
        manager.schedule = lambda identifier: None
        job = await manager.workflow.start(upload_ids=[], prompt="Ask", supplier_id=a, source_scope="api")
        manager.workflow.update_job(job["id"], status="running")
        manager.workflow.accept_questions(job["id"], {"questions": ["Which unit?"], "summary": "Unit ambiguous"})
        await manager.supplier_session(b)
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await restored.recover()
        current = await restored.supplier_session(a, source_scope="api")
        assert current["waiting_job"]["id"] == job["id"] and current["recent_job"]["questions"] == ["Which unit?"]
        assert current["conversation_id"] == route["conversation_id"]
        with pytest.raises(RevisionConflictError, match="active task"):
            await restored.supplier_session(a, new_round=True)
        with pytest.raises(RevisionConflictError, match="waiting for input"):
            await restored.workflow.start(upload_ids=[], prompt="Cannot fork waiting", supplier_id=a)
        ready = await restored.workflow.respond(job["id"], current["waiting_job"]["revision"], "million tokens")
        await restored.wait(job["id"])
        assert restored.workflow.get(job["id"])["conversation_id"] == route["conversation_id"]
        assert restored.workflow.get(job["id"])["attempt"] == 2
        assert len(restored.workflow.state["jobs"]) == 1
    asyncio.run(scenario())


def test_explicit_cross_supplier_or_official_session_cannot_rebind(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        route = await manager.supplier_session(a)
        with pytest.raises(ValueError, match="another supplier"):
            await manager.workflow.start(upload_ids=[], prompt="Wrong supplier", supplier_id=b,
                                         conversation_id=route["conversation_id"])
        manager._remember_session("ses_official")
        manager._sessions["ses_official"].update(intent="official_price_lookup", lookup_model_id="m1")
        with pytest.raises(ValueError, match="official lookup"):
            await manager.workflow.start(upload_ids=[], prompt="Wrong scope", supplier_id=a, conversation_id="ses_official")
        assert not manager.workflow.state["jobs"]
        run = await manager.submit(prompt="Bound supplier inferred", conversation_id=route["conversation_id"])
        await manager.wait(run.id)
        assert manager.workflow.get(run.id)["supplier_id"] == a
    asyncio.run(scenario())


def test_new_round_reloads_profile_carries_outcomes_and_actual_sdk_usage(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        first = await manager.submit(prompt="Batch ephemeral", supplier_id=a)
        await manager.wait(first.id)
        job = manager.workflow.get(first.id)
        usage = job["context_usage"]
        assert usage["model_calls"] == 1 and usage["prompt_chars"] > 0
        assert usage["tokens"]["input"] == 123 and usage["token_measurement"] == "sdk_last_turn"
        await manager.workflow.confirm(first.id, job["revision"])
        profile = store.get_supplier_profile(a)
        store.update_supplier_profile(a, {"expected_revision": profile["revision"], "rules": [{"text": "Latest meaning"}]})
        route = await manager.supplier_session(a, new_round=True)
        assert route["conversation_id"] != first.conversation_id
        assert first.id in route["session_summary"] and "confirmed" in route["session_summary"]
        assert "Batch ephemeral" not in route["session_summary"]
        assert "Latest meaning" in json.dumps(route["profile"]["effective"])
        next_run = await manager.submit(prompt="New round", supplier_id=a)
        await manager.wait(next_run.id)
        assert next_run.conversation_id == route["conversation_id"]
        sent = [r for r in calls if r.method == "POST" and r.url.path.endswith("/message")][-1]
        assert "Latest meaning" in json.loads(sent.content)["system"]
        assert first.id in json.loads(sent.content)["system"]
    asyncio.run(scenario())


def test_router_route_payload_and_active_conflict(tmp_path):
    manager, store, a, b, calls = fixture(tmp_path)
    app = FastAPI()
    app.include_router(create_agent_router(manager))
    with TestClient(app) as client:
        route = client.post(f"/api/workspace/agent/suppliers/{a}/session", json={"source_scope": "api"})
        assert route.status_code == 200 and route.json()["source_scope"] == "api"
        assert client.post("/api/workspace/agent/suppliers/missing/session", json={}).status_code == 422


def test_history_usage_aggregates_only_current_attempt_llm_steps(tmp_path):
    manager, store, a, b, calls = fixture(tmp_path)
    def entry(role, text="", **info):
        return {"info": {"role": role, **info}, "parts": [{"type": "text", "text": text}]}
    manager._history_raw["ses_usage"] = [
        entry("user", "Older"), entry("assistant", id="old", tokens={"input": 999, "output": 99}),
        entry("user", "Current"),
        entry("assistant", id="step1", tokens={"input": 100, "output": 10, "cache": {"read": 5}}),
        entry("assistant", id="step2", tokens={"input": 150, "output": 20, "cache": {"write": 3}}),
        entry("assistant", id="step2", tokens={"input": 150, "output": 20}),
        entry("user", "Later"), entry("assistant", id="later", tokens={"input": 999})]
    usage = manager._history_token_usage("ses_usage", "Current")
    assert usage["llm_steps"] == 2 and usage["task_token_measurement"] == "history"
    assert usage["task_tokens"] == {"input": 250, "output": 30, "reasoning": 0, "cache": {"read": 5, "write": 3}}
    assert manager._history_token_usage("ses_usage", "Absent") is None
    manager._history_raw["ses_usage"][4]["info"].pop("tokens")
    assert manager._history_token_usage("ses_usage", "Current")["task_token_measurement"] == "partial_history"


def test_new_round_does_not_restore_old_ready_job(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        run = await manager.submit(prompt="Ready old review", supplier_id=a)
        await manager.wait(run.id)
        route = await manager.supplier_session(a, new_round=True)
        assert route["conversation_id"] != run.conversation_id
        assert route["recent_job"] is None and route["waiting_job"] is None
        assert route["recent_supplier_result"]["id"] == run.id
        again = await manager.supplier_session(a)
        assert again["conversation_id"] == route["conversation_id"] and again["recent_job"] is None
    asyncio.run(scenario())


def test_first_confirmed_supplier_adopts_original_clean_session(tmp_path):
    async def scenario():
        from tests.test_workspace_agent import mocked
        manager, store, calls = mocked(tmp_path)
        run = await manager.submit(prompt="First supplier from file")
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        assert job.get("supplier_id") is None
        await manager.workflow.confirm(run.id, job["revision"])
        sid = store.catalog_snapshot()["quotes"][0]["supplier_id"]
        confirmed = manager.workflow.get(run.id)
        assert confirmed["supplier_id"] == sid
        assert (await manager.supplier_session(sid))["conversation_id"] == job["conversation_id"]
    asyncio.run(scenario())


def test_selected_supplier_scope_rejects_other_supplier_candidate(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        manager.schedule = lambda identifier: None
        job = await manager.workflow.start(upload_ids=[], prompt="A only", supplier_id=a)
        with pytest.raises(ValueError, match="another supplier"):
            manager.workflow.accept_proposal(job["id"], {"create": [{"supplier_id": b, "model_id": "m1"}]})
        with pytest.raises(ValueError, match="conflicts"):
            manager.workflow.accept_proposal(job["id"], {"create": [{"supplier": "Supplier B", "model_id": "m1"}]})
        assert store.catalog_snapshot()["quotes"] == []
    asyncio.run(scenario())


def test_confirmed_supplier_alias_matches_selected_session_without_new_supplier(tmp_path):
    async def scenario():
        manager, store, a, b, calls = fixture(tmp_path)
        alias = store.confirm_supplier({"name": "Canonical supplier", "aliases": ["Confirmed channel alias"]})
        sid = alias["supplier_id"]
        manager.schedule = lambda identifier: None
        job = await manager.workflow.start(upload_ids=[], prompt="Known channel", supplier_id=sid)
        manager.workflow.accept_proposal(job["id"], {"create": [{"supplier": "Confirmed channel alias", "model_id": "m1"}]})
        ready = manager.workflow.get(job["id"])
        await manager.workflow.confirm(job["id"], ready["revision"])
        assert store.catalog_snapshot()["quotes"][0]["supplier_id"] == sid
        assert store.get_supplier_profile(sid)["revision"] == 1
        assert len(store.list_suppliers()) == 3
    asyncio.run(scenario())


def test_new_supplier_spacing_normalization_finishes_profile_and_original_session(tmp_path):
    async def scenario():
        from tests.test_workspace_agent import mocked
        manager, store, calls = mocked(tmp_path)
        run = await manager.submit(prompt="First supplier from file")
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        revised = deepcopy(job["bundle"])
        revised["create"][0]["supplier"] = "  New   Supplier  "
        ready = await manager.workflow.revise(job["id"], job["revision"], revised)
        confirmed = await manager.workflow.confirm(job["id"], ready["revision"])
        assert confirmed["status"] == "confirmed"
        supplier = store.list_suppliers()[0]
        assert supplier["name"] == "New Supplier"
        assert store.get_supplier_profile(supplier["supplier_id"])["revision"] == 1
        assert (await manager.supplier_session(supplier["supplier_id"]))["conversation_id"] == job["conversation_id"]
    asyncio.run(scenario())
