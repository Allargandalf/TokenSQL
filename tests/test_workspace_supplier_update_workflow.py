"""Supplier-update lifecycle on isolated synthetic data; never invokes an inference service."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
import httpx
from openpyxl import Workbook
import pytest

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.agent_routes import create_agent_router
from tokensql.workspace.store import RevisionConflictError, WorkspaceStore


def setup(tmp_path):
    cfg = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "synthetic.sqlite3",
                      python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "worker",
                      input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                      data_dir=tmp_path / "data")
    store = WorkspaceStore(cfg.database_path)
    manager = OpenCodeAgentManager(config=cfg, store=store)
    manager.schedule = lambda identifier: None
    supplier = store.confirm_supplier({"name": "Synthetic Supplier"})
    return manager, store, supplier["supplier_id"]


def workbook(*sheets):
    book = Workbook()
    book.remove(book.active)
    for name in sheets or ("Rates",):
        sheet = book.create_sheet(name)
        sheet.append(["model_id", "metric", "price"])
        sheet.append(["a", "input", "2"])
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def row(model, amount, supplier_id=None, **extra):
    return {"model_id": model, "supplier_id": supplier_id, "billing_metric": "input",
            "billing_item": "input", "amount": amount, "pricing_unit_id": "cny_mtoken",
            "pricing_conditions": {}, "source_sheet": "Rates", **extra}


async def update(manager, supplier_id, rows, *, content=None, name="rates.xlsx", context=None, metadata=None):
    raw = content or workbook("Rates")
    files = manager.workflow.upload([AgentUpload(name, raw)])
    request = {"supplier_id": supplier_id, "source_scope": "series-a", "latest_confirmed": True,
               **(context or {})}
    job = await manager.workflow.start(upload_ids=[files[0]["id"]], prompt="Update this supplier",
                                       update_request=request)
    manager.workflow.accept_proposal(job["id"], {"create": rows}, reconciliation_request=metadata)
    return manager.workflow.get(job["id"])


def seed(store, supplier_id):
    return store.apply_bundle({"create": [
        row("a", "1", supplier_id, source_scope="series-a"),
        row("c", "3", supplier_id, source_scope="series-a"),
    ]})["items"]


def test_missing_supplier_retains_parsed_draft_and_resumes_without_model_or_upload(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            memory = manager.memory.read()
            draft = manager.workflow.create_draft({"create": [row("new", "1.234567891")]})
            assert draft["status"] == "waiting_for_input" and draft["waiting_reason"] == "supplier"
            assert not store.catalog_snapshot()["quotes"] and manager.memory.read() == memory
            with pytest.raises(ValueError):
                await manager.workflow.confirm(draft["id"], draft["revision"])
            ready = await manager.workflow.respond(draft["id"], draft["revision"], "Use selected supplier", supplier_id)
            assert ready["status"] == "ready" and ready["id"] == draft["id"]
            assert ready["bundle"]["create"][0]["supplier_id"] == supplier_id
            confirmed = await manager.workflow.confirm(ready["id"], ready["revision"])
            assert confirmed["status"] == "confirmed"
            assert store.get_quote(confirmed["preview"]["items"][0]["id"]).amount == __import__("decimal").Decimal("1.234567891")
            assert not manager.memory.read()["rules"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_full_replace_one_modify_one_create_one_delete_preserves_identity(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            a, c = seed(store, supplier_id)
            job = await update(manager, supplier_id, [row("a", "2"), row("b", "4")],
                context={"mode": "full", "scope_confirmed": True, "expected_sheets": ["Rates"]},
                metadata={"complete_read": True, "covered_sheets": ["Rates"], "unresolved_items": 0})
            assert job["preview"]["ready"]
            groups = job["preview"]["groups"]
            assert {key: len(groups[key]) for key in ("create", "update", "delete", "unchanged")} == {
                "create": 1, "update": 1, "delete": 1, "unchanged": 0}
            assert groups["update"][0]["before"]["id"] == a["id"]
            assert groups["delete"][0]["before"]["id"] == c["id"]
            result = await manager.workflow.confirm(job["id"], job["revision"])
            assert result["changes"]["added"] == result["changes"]["updated"] == result["changes"]["deleted"] == 1
            assert store.get_quote(a["id"]).revision == a["revision"] + 1
            assert {r["model_id"] for r in store.catalog_snapshot()["quotes"]} == {"a", "b"}
            assert not manager.memory.read()["rules"]
            repeated = await manager.workflow.confirm(job["id"], job["revision"])
            assert repeated["changes"] == result["changes"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_incremental_absence_preserves_other_quotes_and_scopes(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            a, c = seed(store, supplier_id)
            manual = store.apply_bundle({"create": [row("manual", "6", supplier_id)]})["items"][0]
            job = await update(manager, supplier_id, [row("a", "2")])
            assert not job["preview"]["groups"]["delete"]
            await manager.workflow.confirm(job["id"], job["revision"])
            assert store.get_quote(c["id"]).amount == __import__("decimal").Decimal("3")
            assert store.get_quote(manual["id"]).amount == __import__("decimal").Decimal("6")
        finally:
            store.close()
    asyncio.run(scenario())


def test_full_missing_sheet_and_forged_model_authorization_cannot_delete(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            seed(store, supplier_id)
            job = await update(manager, supplier_id, [row("a", "2")],
                content=workbook("Rates", "Second"),
                context={"mode": "full", "scope_confirmed": True, "expected_sheets": ["Rates", "Second"]},
                metadata={"complete_read": True, "covered_sheets": ["Rates"], "scope_confirmed": True,
                          "allow_rollback": True, "file_sha256": "0" * 64})
            assert not job["preview"]["ready"] and not job["preview"]["groups"]["delete"]
            assert not job["reconciliation_request"]["complete_read"]
            assert not job["reconciliation_request"]["allow_rollback"]
            assert job["reconciliation_request"]["file_sha256"] != "0" * 64
            with pytest.raises(ValueError, match="pending"):
                await manager.workflow.confirm(job["id"], job["revision"])
            assert len(store.catalog_snapshot()["quotes"]) == 2
            await manager.workflow.cancel(job["id"])
            assert manager.workflow.get(job["id"])["files"][0]["status"] == "input"
            assert not manager.memory.read()["rules"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_supplier_update_missing_supplier_reuses_original_and_source_observations(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            job = await update(manager, None, [row("a", "2")],
                context={"mode": "full", "scope_confirmed": True, "expected_sheets": ["Rates"]},
                metadata={"complete_read": True, "covered_sheets": ["Rates"], "source_version": "v2"})
            assert job["status"] == "waiting_for_input"
            path = manager.workflow._location(manager.workflow.state["uploads"][job["upload_ids"][0]])
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            restored = OpenCodeAgentManager(config=manager.config, store=store)
            restored.schedule = lambda identifier: None
            await restored.recover()
            resumed = await restored.workflow.respond(job["id"], job["revision"], "Supplier confirmed", supplier_id)
            assert resumed["status"] == "ready" and not resumed["preview"]["ready"]
            assert not resumed["reconciliation_request"]["scope_confirmed"]
            assert not resumed["preview"]["groups"]["delete"]
            assert resumed["reconciliation_request"]["source_version"] == "v2"
            assert resumed["files"][0]["sha256"] == sha
            assert resumed["conversation_id"] == job["conversation_id"]
            assert not store.catalog_snapshot()["quotes"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_concurrent_update_rebuilds_operations_and_requires_another_confirmation(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            a, _ = seed(store, supplier_id)
            first = await update(manager, supplier_id, [row("a", "2")])
            second = await update(manager, supplier_id, [row("a", "3")])
            await manager.workflow.confirm(first["id"], first["revision"])
            with pytest.raises(RevisionConflictError, match="rebuilt"):
                await manager.workflow.confirm(second["id"], second["revision"])
            refreshed = manager.workflow.get(second["id"])
            change = refreshed["preview"]["groups"]["update"][0]
            assert change["before"]["amount"] == "2"
            assert change["before"]["revision"] == a["revision"] + 1
            assert store.get_quote(a["id"]).amount == __import__("decimal").Decimal("2")
            assert refreshed["revision"] > second["revision"]
            await manager.workflow.confirm(refreshed["id"], refreshed["revision"])
            assert store.get_quote(a["id"]).amount == __import__("decimal").Decimal("3")
        finally:
            store.close()
    asyncio.run(scenario())


def test_same_file_new_name_is_noop_and_human_correction_can_repreview(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            raw = workbook("Rates")
            first = await update(manager, supplier_id, [row("a", "2")], content=raw)
            await manager.workflow.confirm(first["id"], first["revision"])
            repeated = await update(manager, supplier_id, [row("a", "2")], content=raw, name="renamed.xlsx")
            assert repeated["preview"]["already_applied_file"]
            assert len(repeated["preview"]["groups"]["unchanged"]) == 1
            assert not repeated["preview"]["groups"]["create"]
            revised = await manager.workflow.revise(repeated["id"], repeated["revision"],
                                                    {"create": [row("a", "2.5")]})
            assert len(revised["preview"]["groups"]["update"]) == 1
            await manager.workflow.confirm(revised["id"], revised["revision"])
            assert len(store.catalog_snapshot()["quotes"]) == 1
            assert store.catalog_snapshot()["quotes"][0]["amount"] == "2.5"
        finally:
            store.close()
    asyncio.run(scenario())


def test_unknown_age_requires_human_latest_confirmation_and_cannot_use_model_permission(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            job = await update(manager, supplier_id, [row("a", "2")],
                context={"latest_confirmed": False}, metadata={"latest_confirmed": True})
            assert not job["preview"]["ready"]
            assert any("source_order_unknown" in v["reason"] for v in job["preview"]["groups"]["needs_confirmation"])
            reviewed = await manager.workflow.revise(job["id"], job["revision"],
                                reconciliation_request={"latest_confirmed": True})
            assert reviewed["preview"]["ready"]
            await manager.workflow.confirm(reviewed["id"], reviewed["revision"])
        finally:
            store.close()
    asyncio.run(scenario())


def test_api_supplier_resume_and_update_context_fields(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        api = FastAPI()
        api.include_router(create_agent_router(manager))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
                draft = manager.workflow.create_draft({"create": [row("a", "2")]})
                response = await client.post("/api/workspace/imports/" + draft["id"] + "/respond",
                    json={"expected_revision": draft["revision"], "prompt": "Supplier selected", "supplier_id": supplier_id})
                assert response.status_code == 202, response.text
                assert response.json()["status"] == "ready"
                response = await client.post("/api/workspace/agent/messages",
                    data={"prompt": "Supplier update", "supplier_id": supplier_id,
                          "update_request": json.dumps({"supplier_id": supplier_id, "source_scope": "series-a", "latest_confirmed": True})},
                    files={"files": ("synthetic.xlsx", workbook("Rates"))})
                assert response.status_code == 202, response.text
                job = manager.workflow.get(response.json()["run_id"])
                assert job["update_request"]["supplier_id"] == supplier_id
                assert len(job["update_request"]["file_sha256"]) == 64
        finally:
            store.close()
    asyncio.run(scenario())


def test_invalid_edit_does_not_mutate_the_existing_review(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            job = await update(manager, supplier_id, [row("a", "2")])
            before = deepcopy(manager.workflow.get(job["id"]))
            with pytest.raises(ValueError):
                await manager.workflow.revise(job["id"], job["revision"],
                    reconciliation_request={"source_scope": "", "allow_rollback": True})
            assert manager.workflow.get(job["id"]) == before
            result = await manager.workflow.confirm(job["id"], job["revision"])
            assert result["status"] == "confirmed"
        finally:
            store.close()
    asyncio.run(scenario())


def test_switching_incremental_to_full_requires_fresh_scope_confirmation(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            seed(store, supplier_id)
            job = await update(manager, supplier_id, [row("a", "2")],
                context={"scope_confirmed": True, "expected_sheets": ["Rates"]},
                metadata={"complete_read": True, "covered_sheets": ["Rates"]})
            changed = await manager.workflow.revise(job["id"], job["revision"],
                reconciliation_request={"mode": "full"})
            assert not changed["preview"]["ready"]
            assert not changed["reconciliation_request"]["scope_confirmed"]
            assert not changed["preview"]["groups"]["delete"]
            ready = await manager.workflow.revise(job["id"], changed["revision"],
                reconciliation_request={"scope_confirmed": True, "complete_read": True, "covered_sheets": ["Rates"]})
            assert ready["preview"]["ready"] and len(ready["preview"]["groups"]["delete"]) == 1
        finally:
            store.close()
    asyncio.run(scenario())


def test_human_selected_version_cannot_be_replaced_by_model_metadata(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            job = await update(manager, supplier_id, [row("a", "2")],
                context={"source_version": "v5"}, metadata={"source_version": "v1"})
            assert job["reconciliation_request"]["source_version"] == "v5"
            assert job["source_observation_conflicts"] == [{"field": "source_version", "human": "v5", "source": "v1"}]
            assert not job["preview"]["ready"] and not store.catalog_snapshot()["quotes"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_ambiguous_identity_waits_for_one_explicit_mapping_then_updates_only_target(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            originals = store.apply_bundle({"create": [row("a", "1", supplier_id, source_scope="series-a"),
                                                       row("a", "1.5", supplier_id, source_scope="series-a")]})["items"]
            job = await update(manager, supplier_id, [row("a", "2")])
            pending = job["preview"]["groups"]["needs_confirmation"]
            assert len(pending) == 1 and len(pending[0]["candidate_ids"]) == 2
            selected = originals[1]
            reviewed = await manager.workflow.revise(job["id"], job["revision"], decisions=[{
                "entry_key": pending[0]["entry_key"], "action": "update",
                "quote_id": selected["id"], "expected_revision": selected["revision"]}])
            assert reviewed["preview"]["ready"]
            await manager.workflow.confirm(reviewed["id"], reviewed["revision"])
            assert store.get_quote(originals[0]["id"]).amount == __import__("decimal").Decimal("1")
            assert store.get_quote(selected["id"]).amount == __import__("decimal").Decimal("2")
        finally:
            store.close()
    asyncio.run(scenario())


def test_old_source_version_requires_explicit_restore_review(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            first = await update(manager, supplier_id, [row("a", "2")], context={"source_version": "v2"})
            await manager.workflow.confirm(first["id"], first["revision"])
            old = await update(manager, supplier_id, [row("a", "1")], context={"source_version": "v1"})
            assert not old["preview"]["ready"]
            assert any("older_version" in item["reason"] for item in old["preview"]["groups"]["needs_confirmation"])
            assert store.catalog_snapshot()["quotes"][0]["amount"] == "2"
            restored = await manager.workflow.revise(old["id"], old["revision"],
                reconciliation_request={"allow_rollback": True})
            assert restored["preview"]["ready"]
            await manager.workflow.confirm(restored["id"], restored["revision"])
            assert store.catalog_snapshot()["quotes"][0]["amount"] == "1"
        finally:
            store.close()
    asyncio.run(scenario())


def test_mock_sdk_draft_cli_update_uses_host_context_and_returns_raw_entries(tmp_path):
    from tokensql.workspace.draft_cli import prepare
    from tokensql.workspace.memory import atomic_json
    import re

    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        requests = []
        def handler(request):
            if request.url.path == "/session" and request.method == "POST":
                return httpx.Response(200, json={"id": "ses_supplier_update"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)
            requests.append(body)
            directory = Path(re.search(r"^Use only this run directory for writable output: (.+)$",
                                      body["system"], re.MULTILINE)[1])
            snapshot_path = Path(re.search(r"^Read-only catalog snapshot: (.+)$",
                                          body["system"], re.MULTILINE)[1])
            snapshot = json.loads(snapshot_path.read_text())
            assert snapshot["supplier_update_request"]["supplier_id"] == supplier_id
            payload = prepare({"bundle": {"create": [row("a", "2")]},
                               "reconciliation_request": {"covered_sheets": ["Rates"], "complete_read": True}},
                              snapshot)
            assert payload["bundle"]["create"] and not payload["bundle"]["update"]
            atomic_json(directory / "proposal.json", payload)
            return httpx.Response(200, json={"info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "Prepared supplier update for review."}]})
        manager = OpenCodeAgentManager(config=manager.config, store=store, transport=httpx.MockTransport(handler))
        try:
            a, _ = seed(store, supplier_id)
            run = await manager.submit(prompt="Update the file", files=[AgentUpload("rates.xlsx", workbook("Rates"))],
                update_request={"supplier_id": supplier_id, "source_scope": "series-a", "latest_confirmed": True})
            result = await manager.wait(run.id)
            assert result.status == "ready", result.error
            job = manager.workflow.get(run.id)
            assert len(job["preview"]["groups"]["update"]) == 1
            assert job["bundle"]["update"][0]["id"] == a["id"]
            assert "LATEST supplier-update policy" in requests[0]["system"]
            assert store.get_quote(a["id"]).amount == __import__("decimal").Decimal("1")
            await manager.workflow.confirm(job["id"], job["revision"])
            assert store.get_quote(a["id"]).amount == __import__("decimal").Decimal("2")
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(scenario())


def test_waiting_response_expands_full_scope_without_reusing_old_delete_authorization(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            seed(store, supplier_id)
            job = await update(manager, None, [row("a", "2")], content=workbook("Rates", "Second"),
                context={"mode": "full", "scope_confirmed": True, "expected_sheets": ["Rates"]},
                metadata={"complete_read": True, "covered_sheets": ["Rates"]})
            assert job["status"] == "waiting_for_input"
            resumed = await manager.workflow.respond(job["id"], job["revision"], "Use selected supplier and wider scope",
                supplier_id, update_request={"expected_sheets": ["Rates", "Second"]})
            assert resumed["status"] == "ready" and resumed["id"] == job["id"]
            request = resumed["reconciliation_request"]
            assert not request["scope_confirmed"] and not request["complete_read"]
            assert not resumed["preview"]["ready"] and not resumed["preview"]["groups"]["delete"]
            assert len(store.catalog_snapshot()["quotes"]) == 2
        finally:
            store.close()
    asyncio.run(scenario())


def test_conflicting_amount_discount_stays_reviewable_until_pricing_basis_selected(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            store.apply_bundle({"official_prices": [{"model_id": "a", "billing_metric": "input",
                "amount": "10", "pricing_unit_id": "cny_mtoken", "pricing_conditions": {}}]})
            bundle = {"create": [row("a", "9.00", supplier_id, discount_rate="0.80")]}
            job = manager.workflow.create_draft(bundle)
            assert job["status"] == "ready"
            assert any(item["code"] == "pricing_basis_required" for item in job["preview"]["issues"])
            with pytest.raises(ValueError, match="valid pricing basis"):
                await manager.workflow.confirm(job["id"], job["revision"])
            assert manager.workflow.get(job["id"])["status"] == "ready"
            assert not store.catalog_snapshot()["quotes"]
            chosen = deepcopy(job["bundle"])
            chosen["create"][0]["pricing_basis"] = "amount"
            reviewed = await manager.workflow.revise(job["id"], job["revision"], chosen)
            await manager.workflow.confirm(reviewed["id"], reviewed["revision"])
            quote = store.catalog_snapshot()["quotes"][0]
            assert quote["amount"] == "9.00" and quote["discount_rate"] == "0.80"
            assert quote["pricing_basis"] == "amount"
        finally:
            store.close()
    asyncio.run(scenario())


def test_supplier_update_conflicting_basis_requires_edit_before_confirm(tmp_path):
    async def scenario():
        manager, store, supplier_id = setup(tmp_path)
        try:
            store.apply_bundle({"official_prices": [{"model_id": "a", "billing_metric": "input",
                "amount": "10", "pricing_unit_id": "cny_mtoken", "pricing_conditions": {}}]})
            job = await update(manager, supplier_id, [row("a", "9.00", discount_rate="0.80")])
            assert not job["preview"]["ready"]
            assert any(item["reason"] == "pricing_basis_required" for item in job["preview"]["groups"]["needs_confirmation"])
            candidate = deepcopy(job["proposal_bundle"])
            candidate["create"][0]["pricing_basis"] = "discount_rate"
            reviewed = await manager.workflow.revise(job["id"], job["revision"], candidate)
            assert reviewed["preview"]["ready"]
            await manager.workflow.confirm(reviewed["id"], reviewed["revision"])
            quote = store.catalog_snapshot()["quotes"][0]
            assert quote["amount"] == "9.00" and quote["discount_rate"] == "0.80"
            assert quote["pricing_basis"] == "discount_rate"
        finally:
            store.close()
    asyncio.run(scenario())
