from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import time

import httpx
import pytest

from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.official_lookup import validate_lookup_bundle
from tokensql.workspace.store import WorkspaceStore


def setup_manager(tmp_path, handler, **kwargs):
    source = tmp_path / "official_sources.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Official sources\nhttps://vendor.example/prices\n", encoding="utf-8")
    cfg = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                      python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "worker",
                      official_sources_path=source, **kwargs)
    store = WorkspaceStore(cfg.database_path)
    manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
    return manager, store


@pytest.mark.parametrize("failure", ["missing_proposal", "invalid_schema", "model_error", "http_error_body",
                                     "history_failure", "history_stale", "abort_failure"])
def test_actual_reply_is_persisted_before_any_draft_failure(tmp_path, failure):
    async def scenario():
        manager = None
        def handler(request):
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_failure"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(503 if failure == "abort_failure" else 200, json=True)
            if request.method == "GET":
                if failure == "history_failure":
                    return httpx.Response(503, json={"message": "history unavailable"})
                if failure == "history_stale":
                    return httpx.Response(200, json=[{"info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": "Older answer"}]}])
                return httpx.Response(200, json=[])
            if failure == "invalid_schema":
                directory = next((manager.config.agent_dir / "runs").iterdir())
                atomic_json(directory / "proposal.json", {"bundle": {"create": [{"amount": "1"}]}})
            info = {"role": "assistant"}
            if failure in {"model_error", "abort_failure"}:
                info["error"] = {"name": "ProviderError"}
            return httpx.Response(503 if failure == "http_error_body" else 200,
                json={"info": info, "parts": [{"type": "text", "text": "Tokens scale is unknown; I kept the source value."}]})
        manager, store = setup_manager(tmp_path, handler)
        run = await manager.submit(prompt="Preserve unknown fields")
        result = await manager.wait(run.id)
        assert result.status == "failed"
        assert result.assistant_message == "Tokens scale is unknown; I kept the source value."
        assert result.messages[-1]["text"] == result.assistant_message
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        assert restored.get_run(run.id).assistant_message == result.assistant_message
        assert store.catalog_snapshot()["quotes"] == []
        if failure == "abort_failure":
            assert manager._sessions["ses_failure"]["needs_abort"]
    asyncio.run(scenario())


def test_history_timeout_cannot_block_answer_or_valid_ready_draft(tmp_path):
    async def scenario():
        manager = None
        async def handler(request):
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_history_timeout"})
            if request.method == "GET":
                await asyncio.sleep(30)
            directory = next((manager.config.agent_dir / "runs").iterdir())
            atomic_json(directory / "proposal.json", {"bundle": {}})
            return httpx.Response(200, json={"info": {"role": "assistant"},
                                           "parts": [{"type": "text", "text": "Nothing invented."}]})
        manager, _ = setup_manager(tmp_path, handler)
        started = time.monotonic()
        run = await manager.submit(prompt="Explain only")
        result = await manager.wait(run.id)
        assert time.monotonic() - started < 3.5
        assert result.status == "ready" and result.assistant_message == "Nothing invented."
        assert manager.workflow.get(run.id)["history_unavailable"]
    asyncio.run(scenario())


def test_request_error_never_reuses_old_assistant_answer(tmp_path):
    async def scenario():
        def handler(request):
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Previous request"}]},
                    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Old answer"}]}])
            return httpx.Response(503, json={"message": "Transport is unavailable"})
        manager, _ = setup_manager(tmp_path, handler)
        run = await manager.submit(prompt="New request", conversation_id="ses_existing")
        result = await manager.wait(run.id)
        assert result.status == "failed" and result.assistant_message is None
        assert result.messages[-1]["text"] == "Old answer"  # actual session history, not this request's reply
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"message": "Request failed"},
    {"parts": [{"type": "text", "text": "Not an assistant response"}]},
    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Human request"}]},
])
def test_request_error_body_is_never_fabricated_as_assistant(tmp_path, body):
    async def scenario():
        def handler(request):
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_no_fake"})
            if request.method == "GET":
                return httpx.Response(200, json=[])
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            return httpx.Response(503, json=body)
        manager, _ = setup_manager(tmp_path, handler)
        result = await manager.wait((await manager.submit(prompt="Request")).id)
        assert result.status == "failed" and result.assistant_message is None
    asyncio.run(scenario())


def test_timeout_recovers_current_turn_answer_and_retains_sdk_abort(tmp_path):
    async def scenario():
        user_text = None
        aborted = False
        def handler(request):
            nonlocal user_text, aborted
            if request.url.path.endswith("/abort"):
                aborted = True
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": user_text}]},
                    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "I need source evidence."}]}])
            user_text = json.loads(request.content)["parts"][0]["text"]
            raise httpx.ReadTimeout("provider timeout", request=request)
        manager, _ = setup_manager(tmp_path, handler)
        run = await manager.submit(prompt="Find evidence", conversation_id="ses_current")
        result = await manager.wait(run.id)
        assert result.status == "failed" and result.assistant_message == "I need source evidence."
        assert "timed out" in result.error and aborted
        assert not manager._sessions["ses_current"]["needs_abort"]
    asyncio.run(scenario())


def price(model_id="m1", billing_item="input"):
    return {"model_id": model_id, "billing_item": billing_item, "amount": "2",
            "pricing_unit_id": "usd_mtoken", "explicit_official": True,
            "source": "https://vendor.example/pricing",
            "source_ref": json.dumps({"retrieved_at": date.today().isoformat(), "region": "not stated",
                                     "tier": "standard", "evidence": "Input 2 USD per million tokens"})}


def test_lookup_is_new_independent_review_and_cannot_modify_quotes(tmp_path):
    async def scenario():
        requests = []
        manager = None
        serial = 0
        def handler(request):
            nonlocal serial
            requests.append(request)
            if request.url.path == "/session":
                serial += 1
                return httpx.Response(200, json={"id": "ses_lookup_" + str(serial)})
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            directory = next(path for path in (manager.config.agent_dir / "runs").iterdir()
                             if not (path / "proposal.json").exists())
            atomic_json(directory / "proposal.json", {"bundle": {"official_prices": [price()]}})
            return httpx.Response(200, json={"info": {"role": "assistant"},
                                           "parts": [{"type": "text", "text": "Official evidence prepared; not committed."}]})
        manager, store = setup_manager(tmp_path, handler)
        store.apply_bundle({"models": [{"model_id": "m1", "vendor": "Vendor", "model_name": "Model"}]})
        first = await manager.lookup_official_prices(model_id="m1", billing_item="input")
        assert first["intent"] == "official_price_lookup" and first["files"] == []
        result = await manager.wait(first["id"])
        assert result.status == "ready" and store.catalog_snapshot()["official_prices"] == []
        job = manager.workflow.get(first["id"])
        session_request = next(r for r in requests if r.url.path == "/session")
        permissions = {p["permission"]: p["action"] for p in json.loads(session_request.content)["permission"]}
        assert permissions["webfetch"] == permissions["websearch"] == "allow"
        reference = manager.config.agent_dir / "catalogs" / (first["id"] + ".official-sources.md")
        assert reference.read_text() == manager.config.official_sources_path.read_text()
        assert reference.stat().st_mode & 0o222 == 0
        message = next(r for r in requests if r.url.path.endswith("/message") and r.method == "POST")
        assert str(reference) in json.loads(message.content)["system"]
        revised = deepcopy(job["bundle"])
        revised["create"] = [{"model_id": "m1"}]
        with pytest.raises(ValueError, match="supplier"):
            await manager.workflow.revise(first["id"], job["revision"], revised)
        confirmed = await manager.workflow.confirm(first["id"], job["revision"])
        assert confirmed["status"] == "confirmed" and len(store.catalog_snapshot()["official_prices"]) == 1
        assert store.catalog_snapshot()["quotes"] == [] and not reference.exists()
        registry = manager._sessions[result.conversation_id]
        assert registry["intent"] == "official_price_lookup" and registry["lookup_model_id"] == "m1"
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        restored.schedule = lambda identifier: None
        followup = await restored.workflow.start(upload_ids=[], prompt="Clarify the listed tier",
                                                 conversation_id=result.conversation_id)
        assert followup["intent"] == "official_price_lookup" and followup["lookup_model_id"] == "m1"
        assert followup["lookup_billing_item"] == "input"
        from tokensql.workspace.agent import AgentUpload
        upload = restored.workflow.upload([AgentUpload("not-moved.xlsx", b"original")])[0]
        with pytest.raises(ValueError, match="attachments"):
            await restored.workflow.start(upload_ids=[upload["id"]], prompt="Attach",
                                           conversation_id=result.conversation_id)
        assert restored.workflow.state["uploads"][upload["id"]]["status"] == "input"
        count = len(restored.workflow.state["uploads"])
        with pytest.raises(ValueError, match="attachments"):
            await restored.submit(prompt="Attach", files=[AgentUpload("not-uploaded.xlsx", b"new")],
                                   conversation_id=result.conversation_id)
        assert len(restored.workflow.state["uploads"]) == count
        second = await manager.lookup_official_prices(model_id="m1", billing_item="input")
        await manager.wait(second["id"])
        assert manager.workflow.get(second["id"])["conversation_id"] != result.conversation_id
        await manager.workflow.cancel(second["id"])
    asyncio.run(scenario())


@pytest.mark.parametrize("mutator,match", [
    (lambda b: b.update(create=[{"model_id": "m1"}]), "supplier"),
    (lambda b: b["official_prices"][0].update(model_id="m2"), "model_id"),
    (lambda b: b["official_prices"][0].update(billing_item="output"), "billing"),
    (lambda b: b["official_prices"][0].update(source="http://localhost/prices"), "HTTPS"),
    (lambda b: b["official_prices"][0].update(source="https://127.0.0.1/prices"), "private"),
    (lambda b: b["official_prices"][0].update(source_ref="Search result"), "JSON"),
    (lambda b: b["official_prices"][0].update(explicit_official=None), "explicit_official"),
    (lambda b: b.update(models=[{"model_id": "m2"}]), "model_id"),
])
def test_lookup_scope_and_evidence_are_checked(mutator, match):
    job = {"intent": "official_price_lookup", "lookup_model_id": "m1", "lookup_billing_item": "input"}
    bundle = {"official_prices": [price()]}
    mutator(bundle)
    snapshot = {"models": [{"model_id": "m1"}]}
    with pytest.raises(ValueError, match=match):
        validate_lookup_bundle(job, bundle, snapshot)


def test_lookup_does_not_invent_a_price_if_evidence_missing():
    job = {"intent": "official_price_lookup", "lookup_model_id": "m1", "lookup_billing_item": None}
    validate_lookup_bundle(job, {"official_prices": [{"model_id": "m1", "amount": None}]}, {"models": []})
    validate_lookup_bundle(job, {}, {"models": []})


def test_lookup_http_contract_forbids_session_injection(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokensql.workspace.agent_routes import create_agent_router
    manager, _ = setup_manager(tmp_path, lambda r: httpx.Response(200, json={}))
    captured = []
    async def lookup(**values):
        captured.append(values)
        return {"id": "job1", "run_id": "job1", "conversation_id": None,
                "status": "queued", "intent": "official_price_lookup", "files": []}
    manager.lookup_official_prices = lookup
    app = FastAPI()
    app.include_router(create_agent_router(manager))
    with TestClient(app) as client:
        result = client.post("/api/workspace/official-prices/lookup",
                             json={"model_id": "m1", "billing_item": None})
        assert result.status_code == 202 and result.json()["intent"] == "official_price_lookup"
        assert captured == [{"model_id": "m1", "billing_item": None, "prompt": ""}]
        injected = client.post("/api/workspace/official-prices/lookup",
                               json={"model_id": "m1", "conversation_id": "ses_injected"})
        assert injected.status_code == 422 and len(captured) == 1


def test_latest_policy_preserves_nullable_raw_business_fields(tmp_path):
    manager, _ = setup_manager(tmp_path, lambda r: httpx.Response(200, json={}))
    prompt = manager._system_prompt(Path("/run"), Path("/catalog.json"))
    assert "Supplier quotations require both model_id and a confirmed supplier" in prompt
    assert "Pure raw discounts are valid" in prompt
    assert "Keep identified rows with null unresolved values" in prompt
    assert "Skip unresolved rows" not in prompt
    assert "Never calculate a missing raw amount" in prompt
