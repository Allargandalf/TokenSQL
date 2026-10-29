from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.draft_cli import prepare, main
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore

def bundle() -> dict:
    return {"models": [{"model_id": "m1", "vendor": "Vendor", "model_name": "Model One",
                         "aliases": ["model-1"]}],
            "official_prices": [{"model_id": "m1", "billing_item": "input", "amount": "10",
                                 "unit": "token", "unit_quantity": "1000000",
                                 "explicit_official": True, "source": "prices.xlsx", "source_ref": "Official!D2"}],
            "create": [{"model_id": "m1", "billing_item": "input", "supplier": "Supplier",
                        "discount_rate": "0.25", "unit": "token", "unit_quantity": "1000000",
                        "source": "prices.xlsx", "source_ref": "Sheet1!E2"}]}

def config(tmp_path: Path, **kwargs) -> AgentConfig:
    return AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                       python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "worker",
                       input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                       data_dir=tmp_path / "data", **kwargs)

def mocked(tmp_path: Path, *, fail: bool = False):
    requests = []
    cfg = config(tmp_path)
    store = WorkspaceStore(cfg.database_path)
    def handler(request):
        requests.append(request)
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_review"} if request.method == "POST"
                                  else [{"id": "ses_review", "directory": str(cfg.agent_dir), "title": "Review"}])
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        if request.method == "GET":
            return httpx.Response(200, json=[{"info": {"role": "assistant"},
                                  "parts": [{"type": "text", "text": "Draft prepared for review"}]}])
        if fail:
            return httpx.Response(200, json={"info": {"role": "assistant", "error": {"name": "ProviderError"}}, "parts": []})
        directory = next(path for path in (cfg.agent_dir / "runs").iterdir() if not (path / "proposal.json").exists())
        atomic_json(directory / "proposal.json", {"bundle": bundle()})
        return httpx.Response(200, json={"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Draft prepared for review"}]})
    manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
    return manager, store, requests

def test_model_only_prepares_and_server_recomputes_before_ready(tmp_path):
    async def scenario():
        manager, store, requests = mocked(tmp_path)
        run = await manager.submit(prompt="Import proposal", files=[AgentUpload("../prices.xlsx", b"original workbook")])
        ready = await manager.wait(run.id)
        assert ready.status == "ready" and ready.review_required
        assert not store.catalog_snapshot()["quotes"]
        assert ready.changes["total"] == 0
        job = manager.workflow.get(run.id)
        assert job["preview"]["items"][0]["derived_amount"] is None
        assert job["preview"]["items"][0]["discount_rate"] == "0.25"
        assert manager.config.database_path.as_posix() not in requests[1].content.decode()
        message = next(r for r in requests if r.method == "POST" and r.url.path.endswith("/message"))
        policy = json.loads(message.content)["system"]
        assert "workspace.cli" not in policy and "--db" not in policy and "draft_cli" in policy
        assert str(manager.config.agent_dir / "catalogs") in policy
        assert job["files"][0]["status"] == "progress"
        assert manager.memory.read()["rules"] == []
    asyncio.run(scenario())

def test_cancel_ready_restores_untouched_files_and_no_memory(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        raw = b"untouched workbook"
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", raw)])
        await manager.wait(run.id)
        result = await manager.workflow.cancel(run.id)
        assert result["status"] == "cancelled" and result["bundle"] is None
        item = manager.workflow.state["uploads"][result["upload_ids"][0]]
        assert manager.workflow._location(item).read_bytes() == raw
        assert item["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not (manager.config.agent_dir / "runs" / run.id).exists()
        assert not store.catalog_snapshot()["models"] and manager.memory.read()["rules"] == []
        assert (await manager.workflow.cancel(run.id))["status"] == "cancelled"
    asyncio.run(scenario())

def test_confirm_revision_idempotency_and_confirmed_correction(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", b"original")])
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        revised = deepcopy(job["bundle"])
        revised["create"][0]["supplier"] = "Corrected Supplier"
        with pytest.raises(ValueError, match="conflict"):
            await manager.workflow.revise(run.id, job["revision"] - 1, revised)
        job = await manager.workflow.revise(run.id, job["revision"], revised)
        assert manager.memory.read()["rules"] == []
        confirmed = await manager.workflow.confirm(run.id, job["revision"], "click-1")
        assert confirmed["status"] == "confirmed" and confirmed["changes"]["added"] == 1
        assert confirmed["files"][0]["status"] == "data"
        assert store.catalog_snapshot()["quotes"][0]["supplier"] == "Corrected Supplier"
        replay = await manager.workflow.confirm(run.id, job["revision"], "click-1")
        assert replay["changes"] == confirmed["changes"]
        assert len(store.catalog_snapshot()["quotes"]) == 1
        assert "Corrected Supplier" not in manager.memory.prompt()
        supplier_id = store.catalog_snapshot()["quotes"][0]["supplier_id"]
        profile = store.get_supplier_profile(supplier_id, "default")
        assert "Corrected Supplier" in json.dumps(profile["effective"]["rules"])
        assert profile["effective"]["rules"][0]["evidence"]["source"] == "confirmed_review"
        assert manager.memory.read()["rules"] == []
        with pytest.raises(ValueError):
            await manager.workflow.cancel(run.id)
    asyncio.run(scenario())

def test_archive_failure_does_not_commit_and_can_retry(tmp_path, monkeypatch):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", b"original")])
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        original = Path.rename
        def fail_archive(path, destination):
            if Path(destination).is_relative_to(manager.config.data_dir):
                raise OSError("test disk failure")
            return original(path, destination)
        monkeypatch.setattr(Path, "rename", fail_archive)
        with pytest.raises(OSError):
            await manager.workflow.confirm(run.id, job["revision"])
        assert manager.workflow.get(run.id)["status"] == "ready"
        assert not store.catalog_snapshot()["quotes"]
        assert manager.memory.read()["rules"] == []
        monkeypatch.setattr(Path, "rename", original)
        assert (await manager.workflow.confirm(run.id, job["revision"]))["status"] == "confirmed"
    asyncio.run(scenario())

def test_db_failure_compensates_archive(tmp_path, monkeypatch):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", b"original")])
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        original = store.apply_bundle
        def reject(*args, **kwargs):
            raise ValueError("test transaction rejection")
        monkeypatch.setattr(store, "apply_bundle", reject)
        with pytest.raises(ValueError):
            await manager.workflow.confirm(run.id, job["revision"])
        assert manager.workflow.get(run.id)["files"][0]["status"] == "progress"
        assert not store.catalog_snapshot()["quotes"]
        monkeypatch.setattr(store, "apply_bundle", original)
    asyncio.run(scenario())

def test_restart_finishes_committed_receipt_without_duplicate(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        job = manager.workflow.create_draft(bundle())
        internal = manager.workflow._job(job["id"])
        internal["status"] = "confirming"
        manager.workflow._save()
        store.apply_bundle(internal["bundle"], actor="review:" + job["id"], import_id="review:" + job["id"])
        replacement = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await replacement.recover()
        assert replacement.workflow.get(job["id"])["status"] == "confirmed"
        assert len(store.catalog_snapshot()["quotes"]) == 1
    asyncio.run(scenario())

def test_running_cancel_aborts_sdk_then_restores_files(tmp_path):
    async def scenario():
        cfg = config(tmp_path)
        store = WorkspaceStore(cfg.database_path)
        entered, blocked = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_running"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            entered.set()
            await blocked.wait()
            return httpx.Response(200, json={"info": {"role": "assistant"}, "parts": []})
        manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", b"original")])
        await asyncio.wait_for(entered.wait(), 2)
        result = await manager.workflow.cancel(run.id)
        assert result["status"] == "cancelled"
        assert "/session/ses_running/abort" in calls
        assert result["files"][0]["status"] == "input"
        assert not store.catalog_snapshot()["quotes"] and manager.memory.read()["rules"] == []
    asyncio.run(scenario())

def test_error_response_never_becomes_ready_and_cancel_cleans(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path, fail=True)
        run = await manager.submit(prompt="Draft")
        result = await manager.wait(run.id)
        assert result.status == "failed" and result.changes["total"] == 0
        assert "model response failed" in result.error
        assert (await manager.workflow.cancel(run.id))["status"] == "cancelled"
        assert not store.catalog_snapshot()["models"]
    asyncio.run(scenario())

def test_session_history_restores_and_memory_replays_latest(tmp_path):
    async def scenario():
        manager, _, requests = mocked(tmp_path)
        first = await manager.submit(prompt="Draft")
        await manager.wait(first.id)
        memory = manager.memory.read()
        manager.memory.update([{"scope": "supplier:Supplier", "text": "Use the stated supplier channel", "enabled": True}],
                              memory["revision"])
        restored = OpenCodeAgentManager(config=manager.config, store=manager.store, transport=manager._transport)
        assert (await restored.sessions())[0]["id"] == "ses_review"
        assert (await restored.history("ses_review"))[0]["text"] == "Draft prepared for review"
        second = await restored.submit(prompt="Continue", conversation_id="ses_review")
        await restored.wait(second.id)
        posts = [r for r in requests if r.method == "POST" and r.url.path == "/session"]
        assert len(posts) == 1
        last = [r for r in requests if r.method == "POST" and r.url.path.endswith("/message")][-1]
        assert "Use the stated supplier channel" not in json.loads(last.content)["system"]
    asyncio.run(scenario())

def test_draft_tool_decimal_fx_and_no_commit_operation(tmp_path, capsys):
    snapshot = WorkspaceStore(":memory:").catalog_snapshot()
    value = bundle()
    value["official_prices"][0].update(amount="2", currency="USD")
    value["create"][0].update(amount="0.5", discount_rate=None, currency="USD")
    output = prepare(value, snapshot)
    assert output["bundle"]["official_prices"][0]["amount"] == "2"
    assert output["bundle"]["official_prices"][0]["currency"] == "USD"
    assert output["preview"]["items"][0]["amount"] == "0.5"
    assert output["preview"]["items"][0]["normalized"] == {}
    main(["fx", "--original-amount", "2", "--original-currency", "USD",
          "--exchange-rate", "7.123456789", "--rate-source", "human prompt"])
    calculated = json.loads(capsys.readouterr().out)
    assert calculated["raw"] == {"amount": "2", "currency": "USD"}
    assert calculated["derived"]["amount"] == "14.246913578"
    with pytest.raises(SystemExit):
        main(["apply", "anything.json"])
    value = bundle()
    del value["official_prices"][0]["explicit_official"]
    assert prepare(value, snapshot)["bundle"]["official_prices"][0]["explicit_official"] is None

def test_router_upload_manual_review_memory_and_bounded_reads(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokensql.workspace.agent_routes import create_agent_router
    manager, store, _ = mocked(tmp_path)
    api = FastAPI()
    api.include_router(create_agent_router(manager))
    with TestClient(api) as client:
        uploaded = client.post("/api/workspace/imports/upload", files={"files": ("../prices.xlsx", b"original")})
        assert uploaded.status_code == 201
        assert client.get("/api/workspace/imports/files").json()["items"][0]["status"] == "input"
        draft = client.post("/api/workspace/imports/drafts", json={"bundle": bundle()})
        assert draft.status_code == 201 and not store.catalog_snapshot()["quotes"]
        job = draft.json()
        assert client.post("/api/workspace/imports/" + job["id"] + "/confirm",
                           json={"expected_revision": job["revision"] - 1}).status_code == 409
        memory = client.get("/api/workspace/agent/memory").json()
        response = client.patch("/api/workspace/agent/memory",
                     json={"expected_revision": memory["revision"], "rules": [{"scope": "workspace", "text": "Ask if unclear"}]})
        assert response.status_code == 200
        assert client.get("/api/workspace/agent/sessions").json()["items"]
    limited = OpenCodeAgentManager(config=config(tmp_path / "limited", max_file_bytes=3, max_total_bytes=4),
                                   store=WorkspaceStore(":memory:"))
    api = FastAPI()
    api.include_router(create_agent_router(limited))
    with TestClient(api) as client:
        assert client.post("/api/workspace/imports/upload", files={"files": ("a.csv", b"1234")}).status_code == 413
        assert limited.workflow.list_files() == []

def test_same_session_queue_cancel_does_not_abort_other_run(tmp_path):
    async def scenario():
        cfg = config(tmp_path)
        store = WorkspaceStore(cfg.database_path)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            entered.set()
            await release.wait()
            directory = next((cfg.agent_dir / "runs").iterdir())
            atomic_json(directory / "proposal.json", {"bundle": bundle()})
            return httpx.Response(200, json={"info": {"role": "assistant"}, "parts": []})
        manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
        first = await manager.submit(prompt="First", conversation_id="ses_same")
        await entered.wait()
        second = await manager.submit(prompt="Second", conversation_id="ses_same")
        await asyncio.sleep(0)
        assert manager.workflow.get(second.id)["status"] == "queued"
        assert (await manager.workflow.cancel(second.id))["status"] == "cancelled"
        assert not any(path.endswith("/abort") for path in calls)
        release.set()
        assert (await manager.wait(first.id)).status == "ready"
        assert not store.catalog_snapshot()["quotes"]
    asyncio.run(scenario())

def test_concurrent_confirm_cancel_and_restart_interruption(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        job = manager.workflow.create_draft(bundle())
        result = await asyncio.gather(manager.workflow.confirm(job["id"], job["revision"]),
                                      manager.workflow.cancel(job["id"]), return_exceptions=True)
        assert result[0]["status"] == "confirmed" and isinstance(result[1], ValueError)
        assert len(store.catalog_snapshot()["quotes"]) == 1
        uploads = manager.workflow.upload([AgentUpload("later.xlsx", b"raw")])
        manager.schedule = lambda identifier: None
        interrupted = await manager.workflow.start(upload_ids=[uploads[0]["id"]], prompt="Later",
                                                   conversation_id="ses_interrupted")
        manager.workflow.update_job(interrupted["id"], status="running")
        recovered = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await recovered.recover()
        state = recovered.workflow.get(interrupted["id"])
        assert state["status"] == "failed" and state["files"][0]["status"] == "input"
        assert not (manager.config.agent_dir / "runs" / interrupted["id"]).exists()
    asyncio.run(scenario())

def test_pending_request_abort_failure_is_retried_before_session_reuse(tmp_path):
    async def scenario():
        cfg = config(tmp_path)
        store = WorkspaceStore(cfg.database_path)
        calls = []
        abort_available = False
        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/abort"):
                return httpx.Response(200 if abort_available else 503, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            raise httpx.ReadTimeout("test stalled provider", request=request)
        manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
        first = await manager.submit(prompt="First", conversation_id="ses_pending")
        assert (await manager.wait(first.id)).status == "failed"
        assert manager._sessions["ses_pending"]["needs_abort"]
        second = await manager.submit(prompt="Second", conversation_id="ses_pending")
        assert (await manager.wait(second.id)).status == "failed"
        assert sum(method == "POST" and path.endswith("/message") for method, path in calls) == 1
        abort_available = True
        assert (await manager.workflow.cancel(first.id))["status"] == "cancelled"
        assert not manager._sessions["ses_pending"]["needs_abort"]
    asyncio.run(scenario())

def test_postcommit_memory_failure_direct_retry_is_idempotent(tmp_path, monkeypatch):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        job = manager.workflow.create_draft(bundle())
        original = store.record_confirmed_supplier_profile
        def unavailable(*args, **kwargs):
            raise OSError("test memory disk outage")
        monkeypatch.setattr(store, "record_confirmed_supplier_profile", unavailable)
        with pytest.raises(OSError):
            await manager.workflow.confirm(job["id"], job["revision"])
        assert manager.workflow.get(job["id"])["status"] == "committed"
        assert len(store.catalog_snapshot()["quotes"]) == 1
        monkeypatch.setattr(store, "record_confirmed_supplier_profile", original)
        result = await manager.workflow.confirm(job["id"], job["revision"])
        assert result["status"] == "confirmed" and len(store.catalog_snapshot()["quotes"]) == 1
    asyncio.run(scenario())

def test_confirm_preserves_original_hash_and_store_unique_audit_total(tmp_path):
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        run = await manager.submit(prompt="Draft", files=[AgentUpload("prices.xlsx", b"original")])
        await manager.wait(run.id)
        job = manager.workflow.get(run.id)
        item = manager.workflow.state["uploads"][job["upload_ids"][0]]
        manager.workflow._location(item).write_bytes(b"external accidental change")
        with pytest.raises(OSError, match="hash changed"):
            await manager.workflow.confirm(run.id, job["revision"])
        assert not store.catalog_snapshot()["quotes"]
        manager.workflow._location(item).write_bytes(b"original")
        assert (await manager.workflow.confirm(run.id, job["revision"]))["status"] == "confirmed"
        first = store.catalog_snapshot()["quotes"][0]
        # An official-price edit preserves raw quote fields; explicit delete permanently removes the quotation ID.
        official = deepcopy(store.catalog_snapshot()["official_prices"][0])
        official = {key: official[key] for key in ("model_id", "billing_item", "amount", "unit",
                    "unit_quantity", "explicit_official", "source", "source_ref")}
        official["amount"] = "20"
        official["expected_revision"] = 1
        second = manager.workflow.create_draft({"official_prices": [official],
                    "delete": [{"id": first["id"], "expected_revision": first["revision"]}]})
        done = await manager.workflow.confirm(second["id"], second["revision"])
        assert done["changes"]["total"] == 1
    asyncio.run(scenario())

def test_catalog_drift_refreshes_preview_and_requires_new_confirmation(tmp_path):
    from tokensql.workspace.store import RevisionConflictError
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        initial = bundle()
        store.apply_bundle({"models": initial["models"], "official_prices": initial["official_prices"]})
        draft = manager.workflow.create_draft({"create": initial["create"]})
        assert draft["preview"]["items"][0]["derived_amount"] is None
        changed = deepcopy(initial["official_prices"][0])
        changed.update(amount="20", expected_revision=1)
        store.apply_bundle({"official_prices": [changed]})
        with pytest.raises(RevisionConflictError):
            await manager.workflow.confirm(draft["id"], draft["revision"])
        refreshed = manager.workflow.get(draft["id"])
        assert refreshed["status"] == "ready" and refreshed["revision"] == draft["revision"] + 1
        assert refreshed["preview"]["items"][0]["derived_amount"] is None
        assert refreshed["preview"]["items"][0]["discount_rate"] == "0.25"
        assert not store.catalog_snapshot()["quotes"]
        assert (await manager.workflow.confirm(draft["id"], refreshed["revision"]))["status"] == "confirmed"
    asyncio.run(scenario())

def test_catalog_race_at_commit_returns_to_review(tmp_path, monkeypatch):
    from tokensql.workspace.store import RevisionConflictError
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        initial = bundle()
        store.apply_bundle({"models": initial["models"], "official_prices": initial["official_prices"]})
        job = manager.workflow.create_draft({"create": initial["create"]})
        original = store.apply_bundle
        once = True
        def concurrent_write(value, *args, **kwargs):
            nonlocal once
            if once and kwargs.get("expected_catalog_token"):
                once = False
                changed = deepcopy(initial["official_prices"][0])
                changed.update(amount="20", expected_revision=1)
                original({"official_prices": [changed]})
            return original(value, *args, **kwargs)
        monkeypatch.setattr(store, "apply_bundle", concurrent_write)
        with pytest.raises(RevisionConflictError):
            await manager.workflow.confirm(job["id"], job["revision"])
        result = manager.workflow.get(job["id"])
        assert result["status"] == "ready" and result["preview"]["items"][0]["derived_amount"] is None
        assert not store.catalog_snapshot()["quotes"]
    asyncio.run(scenario())

def test_draft_wrapper_preserves_unresolved_sheet_issues(tmp_path):
    issue = {"code": "missing_explicit_fx", "message": "USD rows omitted; no rate supplied",
             "source": "prices.xlsx", "source_ref": "USD!A2:F20"}
    store = WorkspaceStore(":memory:")
    output = prepare({"bundle": bundle(), "issues": [issue]}, store.catalog_snapshot())
    assert output["issues"] == [issue]
    manager, _, _ = mocked(tmp_path)
    job = manager.workflow.create_draft(bundle())
    manager.workflow.accept_proposal(job["id"], output["bundle"], output["issues"])
    assert manager.workflow.get(job["id"])["issues"] == [issue]

def test_display_context_refresh_does_not_block_raw_review_and_preserves_foreign_raw(tmp_path):
    from datetime import datetime, timezone
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        initial = bundle()
        store.apply_bundle({"models": initial["models"], "official_prices": initial["official_prices"]})
        context = store.pricing_context()
        rate = {"currency": "USD", "cny_per_unit": "7", "source": "test human rate",
                "observed_at": datetime.now(timezone.utc).isoformat()}
        context = store.set_pricing_context(fx_rates=[rate], token_equivalences=[],
                                           expected_revision=context["revision"])
        raw = deepcopy(initial["create"][0])
        raw.update(amount="0.1", discount_rate=None, currency="USD", unit="second", unit_quantity="1")
        job = manager.workflow.create_draft({"create": [raw]})
        assert job["preview"]["items"][0]["normalized"] == {}
        token = job["preview"]["base_catalog_token"]
        factor = {"model_id": "m1", "billing_item": "input", "unit": "second", "tokens_per_unit": "100",
                  "source": "test measured sample", "source_ref": "sample-run:1",
                  "observed_at": datetime.now(timezone.utc).isoformat(),
                  "assumptions": "Synthetic test estimate"}
        store.set_pricing_context(fx_rates=[rate], token_equivalences=[factor],
                                  expected_revision=context["revision"])
        assert store.catalog_token() == token
        done = await manager.workflow.confirm(job["id"], job["revision"])
        assert done["status"] == "confirmed"
        saved = store.catalog_snapshot()["quotes"][0]
        assert saved["amount"] == "0.1" and saved["currency"] == "USD"
        assert saved["unit"] == "second" and saved["unit_quantity"] == "1"
        assert saved["normalized"] == {} and saved["derived_amount"] is None
        assert saved["discount_rate"] is None
    asyncio.run(scenario())

def test_unicode_upload_names_preserve_context_with_safe_byte_limit(tmp_path):
    manager, _, _ = mocked(tmp_path)
    filename = "\u6a21\u578b \u62a5\u4ef7\uff08\u5341\u6708\uff09.xlsx"
    saved = manager.workflow.upload([AgentUpload("../" + filename, b"raw")])[0]
    assert saved["filename"] == filename
    long_name = "\u62a5" * 180 + ".xlsx"
    saved = manager.workflow.upload([AgentUpload(long_name, b"raw")])[0]
    assert len(saved["filename"].encode("utf-8")) <= 180
    item = manager.workflow.state["uploads"][saved["id"]]
    assert manager.workflow._location(item).read_bytes() == b"raw"

def test_display_rate_expiry_does_not_block_confirm_or_change_raw_values(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    import tokensql.workspace.conversion as conversions
    import tokensql.workspace.store as stores
    async def scenario():
        manager, store, _ = mocked(tmp_path)
        initial = bundle()
        store.apply_bundle({"models": initial["models"], "official_prices": initial["official_prices"]})
        present = datetime.now(timezone.utc)
        expiry = present + timedelta(hours=1)
        context = store.pricing_context()
        store.set_pricing_context(fx_rates=[{"currency": "USD", "cny_per_unit": "7",
            "source": "test rate", "observed_at": present.isoformat(), "expires_at": expiry.isoformat()}],
            token_equivalences=[], expected_revision=context["revision"])
        raw = deepcopy(initial["create"][0])
        raw.update(amount="0.1", discount_rate=None, currency="USD")
        job = manager.workflow.create_draft({"create": [raw]})
        assert job["preview"]["items"][0]["normalized"] == {}
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                result = expiry + timedelta(seconds=1)
                return result.astimezone(tz) if tz else result.replace(tzinfo=None)
        monkeypatch.setattr(conversions, "datetime", Later)
        monkeypatch.setattr(stores, "datetime", Later)
        done = await manager.workflow.confirm(job["id"], job["revision"])
        assert done["status"] == "confirmed"
        saved = store.catalog_snapshot()["quotes"][0]
        assert saved["amount"] == "0.1" and saved["currency"] == "USD"
        assert saved["discount_rate"] is None and saved["normalized"] == {}
    asyncio.run(scenario())

def test_confirmed_memory_keeps_entity_scopes_and_valid_bounded_json(tmp_path):
    manager, _, _ = mocked(tmp_path)
    before = {"create": [{"model_id": "m" + str(index), "billing_item": "input",
                         "supplier": "Supplier", "amount": "1", "note": "old"} for index in range(200)]}
    after = deepcopy(before)
    for row in after["create"]:
        row["note"] = "Confirmed replacement " * 100
    manager.memory.remember_confirmed("bounded-test", before, after, ["prices.xlsx"])
    state = manager.memory.read()
    rule = state["rules"][0]
    payload = json.loads(rule["text"])
    assert len(rule["text"]) <= 16000
    assert payload["omitted_count"] > 0 and payload["corrections"]
    assert payload["corrections"][0]["entity_scope"]["model_id"]["after"] == "m0"
    assert "never identities across runs" in payload["instruction"]
    # Human can round-trip an automatically generated rule without its valid bound being rejected.
    assert manager.memory.update(state["rules"], state["revision"])["rules"]
