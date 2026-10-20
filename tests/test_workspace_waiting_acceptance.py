"""Independent waiting-for-input lifecycle acceptance against a scripted SDK, never production."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

from fastapi import FastAPI
import httpx
from openpyxl import Workbook
import pytest

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.agent_routes import create_agent_router
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import RevisionConflictError, WorkspaceStore


def excel_bytes() -> bytes:
    book = Workbook()
    book.active.append(["model_id", "discount", "unit"])
    book.active.append(["example-model", "8折", "Tokens"])
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class ScriptedSDK:
    """Model behavior is explicit; SDK history intentionally lags behind current replies."""
    def __init__(self, actions):
        self.actions = list(actions)
        self.posts = []
        self.sessions = []
        self.aborts = []
        self.directories = []
        self.snapshots = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, request):
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if request.url.path == "/session" and request.method == "POST":
            body = json.loads(request.content)
            self.sessions.append(body)
            return httpx.Response(200, json={"id": "ses_waiting_acceptance"})
        if request.url.path.endswith("/abort"):
            self.aborts.append(request.url.path)
            return httpx.Response(200, json=True)
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "OLD_USER_TURN"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "OLD_ASSISTANT_REPLY"}]},
            ])
        assert request.url.path.endswith("/message"), request.url.path
        body = json.loads(request.content)
        self.posts.append(body)
        assert self.actions, "unexpected model invocation"
        action = self.actions.pop(0)
        directory = Path(re.search(r"^Use only this run directory for writable output: (.+)$",
                                   body["system"], re.MULTILINE).group(1))
        snapshot = Path(re.search(r"^Read-only catalog snapshot: (.+)$",
                                  body["system"], re.MULTILINE).group(1))
        self.directories.append(directory)
        self.snapshots.append(json.loads(snapshot.read_text()))
        if action.get("block"):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not action.get("late"):
                    raise
                # A badly behaved upstream may complete despite local cancellation.
        kind = action["kind"]
        if kind in {"wait", "both", "error_wait"}:
            atomic_json(directory / "needs_input.json", action.get("needs", {
                "questions": ["Which token scale should apply?"],
                "summary": "A human clarification is required.",
            }))
        if kind in {"draft", "both"}:
            atomic_json(directory / "proposal.json", {"bundle": action.get("bundle", {
                "create": [{"model_id": "example-model", "discount_rate": "0.8", "currency": "USD"}],
            })})
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        info = {"role": "assistant"}
        if kind == "error_wait":
            info["error"] = {"name": "ProviderError", "message": "synthetic provider failure"}
        return httpx.Response(200, json={"info": info, "parts": [
            {"type": "text", "text": action.get("reply", "CURRENT_ASSISTANT_REPLY")},
        ]})


def setup(tmp_path, actions):
    sdk = ScriptedSDK(actions)
    cfg = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "db.sqlite3",
                      python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "worker",
                      input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                      data_dir=tmp_path / "data", instructions="ADMIN_BASE_INSTRUCTION")
    store = WorkspaceStore(cfg.database_path)
    manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(sdk))
    return manager, store, sdk


def uploaded_location(manager, job):
    item = manager.workflow.state["uploads"][job["upload_ids"][0]]
    return manager.workflow._location(item)


async def start_wait(manager, raw=None):
    files = [AgentUpload("acceptance.xlsx", raw)] if raw is not None else []
    run = await manager.submit(prompt="Check missing data and ask before drafting", files=files)
    await manager.wait(run.id)
    job = manager.workflow.get(run.id)
    assert job["status"] == "waiting_for_input", job
    return job


async def resume(manager, job, prompt="Use USD and one million tokens"):
    return await manager.workflow.respond(job["id"], job["revision"], prompt)


def test_waiting_is_durable_idle_and_zero_side_effect_with_original_excel_sha(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait", "reply": "CURRENT_QUESTION_1"}])
        try:
            raw = excel_bytes()
            before, memory = store.catalog_snapshot(), manager.memory.read()
            job = await start_wait(manager, raw)
            assert job["assistant_message"] == "CURRENT_QUESTION_1"
            assert job["questions"] == ["Which token scale should apply?"]
            assert job["error"] is None and job["bundle"] is None and job["preview"] is None
            assert job["review_required"] is False and job["attempt"] == 1
            assert job["files"][0]["status"] == "progress"
            assert hashlib.sha256(uploaded_location(manager, job).read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()
            assert job["files"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
            assert store.catalog_snapshot() == before and manager.memory.read() == memory
            assert job["id"] not in manager._tasks and job["id"] not in manager._active
            assert not manager._locks[job["conversation_id"]].locked()
            assert (await manager.status())["status"] == "ready"
            assert not sdk.aborts
            restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
            await restored.recover()
            recovered = restored.workflow.get(job["id"])
            assert recovered["status"] == "waiting_for_input"
            assert recovered["questions"] == job["questions"] and recovered["revision"] == job["revision"]
            assert uploaded_location(restored, recovered).read_bytes() == raw
            assert not restored._tasks
            with pytest.raises(ValueError):
                await restored.workflow.confirm(job["id"], recovered["revision"])
            assert store.catalog_snapshot() == before
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_multiround_same_job_session_current_answers_fresh_catalog_and_no_stale_proposal(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [
            {"kind": "wait", "reply": "CURRENT_QUESTION_1"},
            {"kind": "wait", "reply": "CURRENT_QUESTION_2", "needs": {
                "questions": ["Please confirm the supplier."], "summary": "Second clarification."}},
            {"kind": "draft", "reply": "CURRENT_FINAL_DRAFT"},
        ])
        try:
            raw = excel_bytes()
            job = await start_wait(manager, raw)
            old_directory = sdk.directories[0]
            # Stale same-session/run files must not satisfy the next attempt's output contract.
            atomic_json(old_directory / "proposal.json", {"bundle": {"create": [{"model_id": "STALE_MODEL"}]}})
            atomic_json(manager.config.agent_dir / "runs" / job["id"] / "proposal.json",
                        {"bundle": {"create": [{"model_id": "STALE_ROOT_MODEL"}]}})
            store.apply_bundle({"models": [{"model_id": "new-catalog-model"}]}, actor="acceptance-fixture")
            rules = manager.memory.read()
            manager.memory.update([{"scope": "global", "text": "LATEST_GLOBAL_PREF_SENTINEL", "enabled": True}],
                                  rules["revision"])
            queued = await resume(manager, job, "FIRST_ANSWER use USD and million tokens")
            assert queued["id"] == job["id"] and queued["conversation_id"] == job["conversation_id"]
            assert queued["status"] == "queued" and queued["attempt"] == 2
            await manager.wait(job["id"])
            second = manager.workflow.get(job["id"])
            assert second["status"] == "waiting_for_input" and second["assistant_message"] == "CURRENT_QUESTION_2"
            assert second["questions"] == ["Please confirm the supplier."]
            assert second["revision"] > job["revision"]
            assert second["bundle"] is None and second["preview"] is None
            assert len(sdk.sessions) == 1 and sdk.directories[0] != sdk.directories[1]
            assert {m["model_id"] for m in sdk.snapshots[1]["models"]} == {"new-catalog-model"}
            assert "LATEST_GLOBAL_PREF_SENTINEL" in sdk.posts[1]["system"]
            assert "ADMIN_BASE_INSTRUCTION" in sdk.posts[1]["system"]
            second_input = json.dumps(sdk.posts[1])
            assert "FIRST_ANSWER" in second_input and "Check missing data" in second_input
            await resume(manager, second, "SECOND_ANSWER supplier can remain null")
            await manager.wait(job["id"])
            final = manager.workflow.get(job["id"])
            assert final["status"] == "ready" and final["attempt"] == 3
            assert final["assistant_message"] == "CURRENT_FINAL_DRAFT"
            assert final["bundle"]["create"][0]["model_id"] == "example-model"
            assert final["bundle"]["create"][0]["discount_rate"] == "0.80"
            assert final["questions"] == [] and len(final["clarifications"]) == 2
            assert [item["prompt"] for item in final["clarifications"]] == [
                "FIRST_ANSWER use USD and million tokens", "SECOND_ANSWER supplier can remain null"]
            assert all(path != sdk.directories[0] for path in sdk.directories[1:])
            for directory in sdk.directories:
                copies = list((directory / "uploads").iterdir())
                assert len(copies) == 1 and copies[0].read_bytes() == raw
                assert copies[0].stat().st_mode & 0o222 == 0
            assert uploaded_location(manager, final).read_bytes() == raw
            assert store.catalog_snapshot()["quotes"] == []
            assert len(manager.memory.read()["rules"]) == 1
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [
    {"kind": "wait", "needs": {"questions": []}},
    {"kind": "wait", "needs": {"questions": [""]}},
    {"kind": "wait", "needs": {"questions": "not-a-list"}},
    {"kind": "wait", "needs": {"questions": ["ok"], "untrusted_extra": True}},
    {"kind": "both"},
    {"kind": "error_wait"},
    {"kind": "missing"},
    {"kind": "timeout"},
])
def test_only_valid_exclusive_question_artifact_waits_real_errors_still_fail(tmp_path, action):
    async def scenario():
        manager, store, _ = setup(tmp_path, [{**action, "reply": "CURRENT_FAILURE_REPLY"}])
        try:
            run = await manager.submit(prompt="Do not invent missing data")
            await manager.wait(run.id)
            job = manager.workflow.get(run.id)
            assert job["status"] == "failed", job
            assert job["bundle"] is None and job["preview"] is None
            assert not store.catalog_snapshot()["models"] and not manager.memory.read()["rules"]
            if action["kind"] != "timeout":
                assert job["assistant_message"] == "CURRENT_FAILURE_REPLY"
            else:
                assert job["assistant_message"] != "OLD_ASSISTANT_REPLY"
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_http_respond_revision_validation_double_click_and_waiting_confirm_conflict(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft", "block": True}])
        app = FastAPI()
        app.include_router(create_agent_router(manager))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                raw = excel_bytes()
                uploaded = await client.post("/api/workspace/imports/upload",
                    files=[("files", ("acceptance.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))])
                assert uploaded.status_code == 201
                upload_ids = [item["id"] for item in uploaded.json()["items"]]
                started = await client.post("/api/workspace/imports/start",
                    json={"upload_ids": upload_ids, "prompt": "Ask missing details first"})
                assert started.status_code == 202
                identifier = started.json()["id"]
                await manager.wait(identifier)
                job = (await client.get("/api/workspace/imports/" + identifier)).json()
                assert job["status"] == "waiting_for_input"
                url = "/api/workspace/imports/" + identifier
                assert (await client.post(url + "/confirm", json={"expected_revision": job["revision"]})).status_code == 409
                for prompt in ("", "  ", "x" * 20001):
                    assert (await client.post(url + "/respond", json={
                        "expected_revision": job["revision"], "prompt": prompt})).status_code == 422
                assert (await client.post(url + "/respond", json={
                    "expected_revision": job["revision"] - 1, "prompt": "stale answer"})).status_code == 409
                responses = await asyncio.gather(*[client.post(url + "/respond", json={
                    "expected_revision": job["revision"], "prompt": "Same human double-click"}) for _ in range(2)])
                assert sorted(response.status_code for response in responses) == [202, 409]
                accepted = next(response.json() for response in responses if response.status_code == 202)
                assert accepted["id"] == identifier and accepted["conversation_id"] == job["conversation_id"]
                await asyncio.wait_for(sdk.entered.wait(), 2)
                sdk.release.set()
                await manager.wait(identifier)
                ready = manager.workflow.get(identifier)
                assert ready["status"] == "ready" and len(ready["clarifications"]) == 1
                assert len(sdk.posts) == 2 and len(sdk.sessions) == 1
                assert (await client.post(url + "/respond", json={
                    "expected_revision": ready["revision"], "prompt": "not a waiting job"})).status_code == 409
                assert store.catalog_snapshot()["quotes"] == []
        finally:
            sdk.release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_cancel_waiting_returns_original_and_resume_after_cancel_conflicts(tmp_path):
    async def scenario():
        manager, store, _ = setup(tmp_path, [{"kind": "wait"}])
        try:
            raw = excel_bytes()
            before, memory = store.catalog_snapshot(), manager.memory.read()
            job = await start_wait(manager, raw)
            cancelled = await manager.workflow.cancel(job["id"])
            assert cancelled["status"] == "cancelled" and cancelled["questions"] == []
            assert cancelled["bundle"] is None and cancelled["preview"] is None
            assert cancelled["files"][0]["status"] == "input"
            assert uploaded_location(manager, cancelled).read_bytes() == raw
            assert not (manager.config.agent_dir / "runs" / job["id"]).exists()
            with pytest.raises(RevisionConflictError):
                await resume(manager, job)
            restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
            await restored.recover()
            assert restored.workflow.get(job["id"])["status"] == "cancelled"
            assert store.catalog_snapshot() == before and manager.memory.read() == memory
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_cancel_running_resumed_attempt_cannot_be_revived_by_late_sdk_draft(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft", "block": True, "late": True}])
        try:
            raw = excel_bytes()
            job = await start_wait(manager, raw)
            await resume(manager, job)
            await asyncio.wait_for(sdk.entered.wait(), 2)
            cancelled = await asyncio.wait_for(manager.workflow.cancel(job["id"]), 3)
            sdk.release.set()
            await asyncio.sleep(0)
            assert cancelled["status"] == manager.workflow.get(job["id"])["status"] == "cancelled"
            assert cancelled["files"][0]["status"] == "input"
            assert uploaded_location(manager, cancelled).read_bytes() == raw
            assert not manager._tasks and not manager._active
            assert not store.catalog_snapshot()["quotes"] and not manager.memory.read()["rules"]
            assert any(path.endswith("/abort") for path in sdk.aborts)
        finally:
            sdk.release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_concurrent_cancel_and_respond_finishes_cancelled_without_business_or_memory_writes(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft", "block": True}])
        try:
            raw = excel_bytes()
            job = await start_wait(manager, raw)
            outcomes = await asyncio.gather(resume(manager, job), manager.workflow.cancel(job["id"]),
                                            return_exceptions=True)
            sdk.release.set()
            await manager.wait(job["id"])
            final = manager.workflow.get(job["id"])
            assert final["status"] == "cancelled" and final["files"][0]["status"] == "input"
            assert any(isinstance(value, dict) and value.get("status") == "cancelled" for value in outcomes)
            assert uploaded_location(manager, final).read_bytes() == raw
            assert not store.catalog_snapshot()["models"] and not manager.memory.read()["rules"]
        finally:
            sdk.release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_resumed_timeout_is_failed_and_never_reuses_previous_question_as_current_reply(tmp_path):
    async def scenario():
        manager, store, _ = setup(tmp_path, [{"kind": "wait", "reply": "PREVIOUS_QUESTION"}, {"kind": "timeout"}])
        try:
            job = await start_wait(manager)
            await resume(manager, job)
            await manager.wait(job["id"])
            failed = manager.workflow.get(job["id"])
            assert failed["status"] == "failed" and failed["questions"] == []
            assert failed["assistant_message"] not in {"PREVIOUS_QUESTION", "OLD_ASSISTANT_REPLY"}
            assert failed["bundle"] is None and failed["preview"] is None
            assert not store.catalog_snapshot()["models"] and not manager.memory.read()["rules"]
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_waiting_official_lookup_resume_retains_target_and_rejects_supplier_scope(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft"}])
        try:
            source = tmp_path / "official.md"
            source.write_text("# Sources\nhttps://example.test/official\n")
            object.__setattr__(manager.config, "official_sources_path", source)
            started = await manager.lookup_official_prices(model_id="official-target", billing_item="input")
            await manager.wait(started["id"])
            job = manager.workflow.get(started["id"])
            assert job["status"] == "waiting_for_input" and job["intent"] == "official_price_lookup"
            permissions = {item["permission"]: item["action"] for item in sdk.sessions[0]["permission"]}
            assert permissions["webfetch"] == permissions["websearch"] == "allow"
            await resume(manager, job, "Try to import supplier quotes instead")
            await manager.wait(job["id"])
            failed = manager.workflow.get(job["id"])
            assert failed["status"] == "failed" and failed["intent"] == "official_price_lookup"
            assert failed["lookup_model_id"] == "official-target" and failed["lookup_billing_item"] == "input"
            assert failed["conversation_id"] == job["conversation_id"]
            assert len(sdk.sessions) == 1 and not store.catalog_snapshot()["quotes"]
            assert "official-target" in sdk.posts[1]["system"]
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_restart_waiting_resume_same_session_then_confirm_once_archives_original(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft"}])
        restored = None
        try:
            raw = excel_bytes()
            job = await start_wait(manager, raw)
            restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
            await restored.recover()
            recovered = restored.workflow.get(job["id"])
            await resume(restored, recovered, "Use CNY; ambiguous token scale remains null")
            await restored.wait(job["id"])
            ready = restored.workflow.get(job["id"])
            assert ready["status"] == "ready" and ready["conversation_id"] == job["conversation_id"]
            assert len(sdk.sessions) == 1 and len(sdk.posts) == 2
            assert not store.catalog_snapshot()["quotes"]
            confirmed = await restored.workflow.confirm(job["id"], ready["revision"], "acceptance-confirm-once")
            assert confirmed["status"] == "confirmed" and confirmed["changes"]["added"] == 1
            assert confirmed["files"][0]["status"] == "data"
            assert uploaded_location(restored, confirmed).read_bytes() == raw
            again = await restored.workflow.confirm(job["id"], ready["revision"], "acceptance-confirm-once")
            assert again["changes"] == confirmed["changes"] and len(store.catalog_snapshot()["quotes"]) == 1
            assert not (manager.config.agent_dir / "runs" / job["id"]).exists()
            assert list((manager.config.agent_dir / "catalogs").glob(job["id"] + "*")) == []
            assert restored.memory.read()["rules"] == []
        finally:
            if restored is not None:
                await restored.shutdown()
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_immediate_resume_before_old_done_callback_does_not_untrack_new_task(tmp_path):
    async def scenario():
        manager, store, sdk = setup(tmp_path, [{"kind": "wait"}, {"kind": "draft", "block": True}])
        second_task = None
        try:
            run = await manager.submit(prompt="Ask missing information first")
            # Observe the terminal status without awaiting the old task: this leaves
            # its done callback queued and reproduces a fast UI response race.
            for _ in range(100):
                job = manager.workflow.get(run.id)
                if job["status"] == "waiting_for_input":
                    break
                await asyncio.sleep(0)
            assert job["status"] == "waiting_for_input"
            old_task = manager._tasks.get(run.id)
            await resume(manager, job)
            second_task = manager._tasks[run.id]
            assert second_task is not old_task
            await asyncio.wait_for(sdk.entered.wait(), 2)
            assert manager._tasks.get(run.id) is second_task
            assert (await manager.status())["status"] == "busy"
            sdk.release.set()
            await second_task
            assert manager.workflow.get(run.id)["status"] == "ready"
        finally:
            sdk.release.set()
            if second_task is not None:
                await asyncio.gather(second_task, return_exceptions=True)
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())
