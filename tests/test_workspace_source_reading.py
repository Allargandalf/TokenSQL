"""End-to-end content reuse through the real store/workflow and a scripted SDK."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from io import BytesIO
import json
import httpx
import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from tokensql.workspace.agent import AgentUpload, OpenCodeAgentManager
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore
from tests.test_workspace_agent import config


def workbook(rows=None, *, presentation=False, header="pricing_unit"):
    rows = rows or [("entry-" + str(i), "m" + str(i), i) for i in range(1, 11)]
    book = Workbook()
    sheet = book.active
    sheet.title = "Rates"
    sheet.append(["source_entry_id", "model_id", "billing_metric", "amount", header])
    for entry, model, amount in rows:
        sheet.append([entry, model, "input", amount, "usd_mtoken"])
    if presentation:
        for row in sheet:
            for cell in row:
                cell.font = Font(bold=True)
        sheet.row_dimensions[2].height = 30
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def setup(tmp_path):
    cfg = config(tmp_path)
    store = WorkspaceStore(cfg.database_path)
    sid = store.confirm_supplier({"name": "Source Supplier"})["supplier_id"]
    calls, packets = [], []
    counter = 0
    def handler(request):
        nonlocal counter
        if request.url.path == "/session" and request.method == "POST":
            counter += 1
            return httpx.Response(200, json={"id": "ses_source_" + str(counter)})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        if request.url.path.endswith("/message"):
            body = json.loads(request.content)
            calls.append(body)
            job = next(job for job in manager.workflow.state["jobs"].values()
                       if job["status"] == "running" and job["conversation_id"] in request.url.path)
            context = job.get("_source_changed_context")
            packets.append(deepcopy(context))
            if context:
                rows = [(region["new_filename"], region["new_sheet"], row)
                        for region in context["regions"] for row in region["new_rows"] if row.get("parse")]
            else:
                rows = [(file["filename"], sheet["name"], row)
                        for file in job["_source_snapshot"]["files"] for sheet in file["sheets"]
                        for row in sheet["rows"] if row["row"] > 1]
            candidates, seen = [], set()
            for filename, sheet, row in rows:
                if row["row"] == 1 or (filename, sheet, row["row"]) in seen:
                    continue
                seen.add((filename, sheet, row["row"]))
                cells = {cell["column"]: cell["value"] for cell in row["cells"]}
                candidates.append({"supplier_id": sid, "source_entry_id": cells[1], "model_id": cells[2],
                    "billing_metric": cells[3], "amount": cells[4], "pricing_unit_id": cells[5],
                    "source": filename, "source_sheet": sheet, "source_ref": f"{sheet}!A{row['row']}:E{row['row']}"})
            root = cfg.agent_dir / "runs" / job["id"]
            directory = root if job["attempt"] == 1 else root / "attempts" / str(job["attempt"])
            candidates = [row for row in candidates if row["model_id"] not in getattr(manager, "test_skip_models", [])]
            official = ([{"model_id": "m1", "billing_item": "input", "billing_metric": "input", "amount": "1",
                          "pricing_unit_id": "usd_mtoken", "explicit_official": True,
                          "source": "rates.xlsx", "source_ref": "Rates!D2"}]
                        if getattr(manager, "test_official", False) else [])
            coverage = {"complete_read": True, "covered_sheets": ["Rates"], "unresolved_items": 0}
            coverage.update(getattr(manager, "test_coverage", {}))
            atomic_json(directory / "proposal.json", {"bundle": {"create": candidates, "official_prices": official},
                "reconciliation_request": coverage})
            return httpx.Response(200, json={"info": {"role": "assistant", "tokens": {"input": 100, "output": 30}},
                                            "parts": [{"type": "text", "text": "Source proposal prepared"}]})
        raise AssertionError(str(request.url))
    manager = OpenCodeAgentManager(config=cfg, store=store, transport=httpx.MockTransport(handler))
    return manager, store, sid, calls, packets


async def submit(manager, sid, content, *, context=None, full=False, prompt="Read raw rows", name="rates.xlsx"):
    run = await manager.submit(prompt=prompt, supplier_id=sid, source_scope="monthly", full_reparse=full,
                               files=[AgentUpload(name, content)], update_request=context)
    await manager.wait(run.id)
    job = manager.workflow.get(run.id)
    assert job["status"] == "ready", job.get("error")
    return job


def prices(job):
    return {row["model_id"]: (row["amount"], row.get("pricing_unit_id"))
            for row in job["proposal_bundle"]["create"]}


def test_ordinary_initial_repeat_and_presentation_zero_requests_keep_quote_ids(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        raw = workbook()
        first = await submit(manager, sid, raw)
        assert first["auto_reconciliation"] and first["preview"]["ready"]
        await manager.workflow.confirm(first["id"], first["revision"])
        original = {row["model_id"]: row["id"] for row in store.catalog_snapshot()["quotes"]}
        assert len(original) == 10 and store.get_supplier_baseline(sid, "monthly")["import_id"] == first["id"]
        for content, name in [(raw, "renamed.xlsx"), (workbook(presentation=True), "styled.xlsx")]:
            job = await submit(manager, sid, content, name=name)
            assert job["reading_plan"]["reuse_mode"] == "all"
            assert job["metrics"]["semantic_requests"] == 0 and job["metrics"]["reused_entries"] == 10
            assert job["context_usage"]["task_token_measurement"] == "no_model_request"
            assert not job["preview"]["groups"]["create"] and len(job["preview"]["groups"]["unchanged"]) == 10
            await manager.workflow.confirm(job["id"], job["revision"])
            assert {row["model_id"]: row["id"] for row in store.catalog_snapshot()["quotes"]} == original
        assert len(calls) == 1
        listing = manager.workflow.list_jobs()
        assert listing[0]["source_comparison_available"] and "source_comparison" not in listing[0]
        assert '"style":' not in json.dumps(manager.workflow.get(job["id"])["source_comparison"]["new_snapshot"])
    asyncio.run(scenario())


def test_delta_reorder_add_delete_merges_equal_to_forced_full_and_full_replacement(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        await manager.workflow.confirm(first["id"], first["revision"])
        rows = [("entry-" + str(i), "m" + str(i), 33 if i == 3 else i) for i in range(1, 10)]
        rows.append(("entry-11", "m11", 11))
        rows.reverse()
        raw = workbook(rows)
        context = {"supplier_id": sid, "source_scope": "monthly", "mode": "full", "scope_confirmed": True,
                   "expected_sheets": ["Rates"], "latest_confirmed": True}
        delta = await submit(manager, sid, raw, context=context)
        assert delta["reading_plan"]["reuse_mode"] == "partial"
        assert delta["metrics"]["reused_entries"] == 8 and delta["metrics"]["reinterpreted_entries"] == 2
        assert len(delta["proposal_bundle"]["create"]) == 10
        assert len(delta["preview"]["groups"]["delete"]) == 1
        assert packets[-1] is not None and len([row for region in packets[-1]["regions"] for row in region["new_rows"] if row.get("parse") and row["row"] > 1]) == 2
        assert "Changed-region package" in calls[-1]["parts"][0]["text"]
        assert "Business files" not in calls[-1]["parts"][0]["text"]
        forced = await submit(manager, sid, raw, context=context, full=True)
        assert forced["reading_plan"]["reuse_mode"] == "none" and packets[-1] is None
        assert prices(delta) == prices(forced)
        await manager.workflow.confirm(delta["id"], delta["revision"])
        assert {row["model_id"] for row in store.catalog_snapshot()["quotes"]} == {"m" + str(i) for i in range(1, 10)} | {"m11"}
        assert store.get_supplier_baseline(sid, "monthly")["bundle"]["update"] == []
    asyncio.run(scenario())


def test_reused_source_reconciles_manual_database_changes_and_cancel_keeps_baseline(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        await manager.workflow.confirm(first["id"], first["revision"])
        baseline = store.get_supplier_baseline(sid, "monthly")
        quote = store.catalog_snapshot()["quotes"][0]
        store.apply_bundle({"update": [{"id": quote["id"], "expected_revision": quote["revision"], "changes": {"amount": "999"}}]})
        reused = await submit(manager, sid, workbook())
        assert reused["metrics"]["semantic_requests"] == 0
        changes = reused["preview"]["groups"]["update"]
        assert len(changes) == 1 and changes[0]["before"]["amount"] == "999"
        assert store.get_quote(quote["id"]).amount == 999
        await manager.workflow.cancel(reused["id"])
        assert store.get_supplier_baseline(sid, "monthly")["revision"] == baseline["revision"]
        assert store.get_quote(quote["id"]).amount == 999
    asyncio.run(scenario())


def test_profile_or_prompt_change_forces_reinterpretation_and_baseline_recovery_once(tmp_path, monkeypatch):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        original = store.record_supplier_baseline
        def unavailable(*args, **kwargs):
            raise OSError("baseline disk unavailable")
        monkeypatch.setattr(store, "record_supplier_baseline", unavailable)
        with pytest.raises(OSError):
            await manager.workflow.confirm(first["id"], first["revision"])
        assert manager.workflow.get(first["id"])["status"] == "committed"
        assert len(store.catalog_snapshot()["quotes"]) == 10
        frozen = deepcopy(manager.workflow._job(first["id"])["baseline_update"])
        monkeypatch.setattr(store, "record_supplier_baseline", original)
        restored = OpenCodeAgentManager(config=manager.config, store=store, transport=manager._transport)
        await restored.recover()
        assert restored.workflow.get(first["id"])["status"] == "confirmed"
        assert len(store.catalog_snapshot()["quotes"]) == 10
        assert "baseline_update" not in restored.workflow._job(first["id"])
        profile = store.get_supplier_profile(sid)
        store.update_supplier_profile(sid, {"expected_revision": profile["revision"], "rules": [{"text": "Input means current API input"}]})
        changed = await submit(manager, sid, workbook())
        assert changed["metrics"]["semantic_requests"] == 1 and changed["reading_plan"]["reuse_mode"] == "none"
        assert packets[-1] is None
        prompt = await submit(manager, sid, workbook(), prompt="Read a new interpretation")
        assert prompt["metrics"]["semantic_requests"] == 1 and prompt["reading_plan"]["reuse_mode"] == "none"
    asyncio.run(scenario())


def test_incomplete_source_and_missing_changed_sheet_cannot_become_complete_baseline(tmp_path):
    from tokensql.workspace.source_reading import freeze_source_baseline, source_coverage
    manager, store, sid, calls, packets = setup(tmp_path)
    job = {"reading_plan": {"reuse_mode": "partial", "needs_model": True,
                            "coverage": {"complete_read": True, "covered_sheets": ["A", "B", "Unchanged"]}},
           "_source_changed_context": {"regions": [
               {"new_sheet": "A", "new_rows": [{"parse": True}]},
               {"new_sheet": "B", "new_rows": [{"parse": True}]}]}}
    missing = source_coverage(job, {"complete_read": True, "covered_sheets": ["A"]})
    assert missing["complete_read"] is False and missing["covered_sheets"] == ["A"]
    complete = source_coverage(job, {"complete_read": True, "covered_sheets": ["A", "B"]})
    assert complete["complete_read"] and complete["covered_sheets"] == ["A", "B", "Unchanged"]
    incomplete = {"baseline_update": {"stale": True}, "_source_snapshot": {"supported": True, "files": [{"sheets": [{"name": "Rates"}]}]},
                  "source_read_coverage": {"complete_read": False, "covered_sheets": ["Rates"]}}
    freeze_source_baseline(manager.workflow, incomplete, {})
    assert "baseline_update" not in incomplete


def test_terminal_source_state_keeps_only_latest_confirmed_comparison(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        await manager.workflow.confirm(first["id"], first["revision"])
        second = await submit(manager, sid, workbook())
        await manager.workflow.confirm(second["id"], second["revision"])
        old = manager.workflow._job(first["id"])
        current = manager.workflow._job(second["id"])
        assert "source_comparison" not in old and old["source_comparison_archived"]
        assert "source_comparison" in current
        assert not any(key.startswith("_source_") for key in old)
        assert "baseline_update" not in current and "profile_updates" not in current
        assert current["metrics"]["semantic_requests"] == 0
        cancelled = await submit(manager, sid, workbook())
        await manager.workflow.cancel(cancelled["id"])
        cancelled = manager.workflow._job(cancelled["id"])
        assert "source_comparison" not in cancelled and not any(key.startswith("_source_") for key in cancelled)
    asyncio.run(scenario())


def test_incomplete_first_source_is_accepted_incrementally_but_identical_file_reinterprets(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        manager.test_skip_models = ["m10"]
        manager.test_coverage = {"complete_read": False}
        first = await submit(manager, sid, workbook())
        await manager.workflow.confirm(first["id"], first["revision"])
        assert len(store.catalog_snapshot()["quotes"]) == 9
        assert store.get_supplier_baseline(sid, "monthly") is None
        manager.test_skip_models = []
        manager.test_coverage = {}
        repeated = await submit(manager, sid, workbook(), context={"supplier_id": sid, "source_scope": "monthly",
            "mode": "full", "scope_confirmed": True, "expected_sheets": ["Rates"], "latest_confirmed": True})
        assert repeated["metrics"]["semantic_requests"] == 1 and repeated["reading_plan"]["reuse_mode"] == "none"
        assert len(repeated["proposal_bundle"]["create"]) == 10 and not repeated["preview"]["groups"]["delete"]
    asyncio.run(scenario())


def test_final_batch_only_choice_keeps_answer_out_of_profile_and_invalidates_next_cache(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        internal = manager.workflow._job(first["id"])
        internal["clarifications"] = [{"prompt": "Only this batch uses the answered meaning", "questions": ["Meaning?"], "persist_profile": True}]
        ready = await manager.workflow.revise(first["id"], first["revision"], persist_profile=False)
        await manager.workflow.confirm(first["id"], ready["revision"])
        assert store.get_supplier_profile(sid, "monthly")["effective"]["rules"] == []
        repeated = await submit(manager, sid, workbook())
        assert repeated["metrics"]["semantic_requests"] == 1 and repeated["reading_plan"]["reuse_mode"] == "none"
    asyncio.run(scenario())


def test_reused_official_exact_scenario_rebinds_revision_and_reviews_manual_price_change(tmp_path):
    from tokensql.workspace.schemas import OfficialPriceCreate
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        manager.test_official = True
        first = await submit(manager, sid, workbook())
        await manager.workflow.confirm(first["id"], first["revision"])
        current = store.catalog_snapshot()["official_prices"][0]
        repeated = await submit(manager, sid, workbook(), name="renamed.xlsx")
        assert repeated["metrics"]["semantic_requests"] == 0 and repeated["preview"]["ready"]
        assert repeated["bundle"]["official_prices"] == [] and repeated["official_catalog_changes"] == []
        assert not any(repeated["preview"]["groups"][key] for key in ("create", "update", "delete"))
        await manager.workflow.confirm(repeated["id"], repeated["revision"])
        assert store.catalog_snapshot()["official_prices"][0]["revision"] == current["revision"]
        def manual_price(amount):
            row = store.catalog_snapshot()["official_prices"][0]
            write = {key: value for key, value in row.items() if key in OfficialPriceCreate.model_fields}
            write.update(amount=amount, expected_revision=row["revision"])
            store.apply_bundle({"official_prices": [write]})
        manual_price("9")
        context = {"supplier_id": sid, "source_scope": "monthly", "latest_confirmed": True}
        changed = await submit(manager, sid, workbook(), context=context)
        assert changed["metrics"]["semantic_requests"] == 0 and changed["preview"]["ready"]
        review = changed["official_catalog_changes"][0]
        assert review["before"]["amount"] == "9" and review["after"]["amount"] == "1"
        manual_price("8")
        from tokensql.workspace.store import RevisionConflictError
        with pytest.raises(RevisionConflictError, match="Catalog changed"):
            await manager.workflow.confirm(changed["id"], changed["revision"])
        refreshed = manager.workflow.get(changed["id"])
        assert refreshed["official_catalog_changes"][0]["before"]["amount"] == "8"
        assert refreshed["revision"] > changed["revision"]
        await manager.workflow.confirm(changed["id"], refreshed["revision"])
        assert store.catalog_snapshot()["official_prices"][0]["amount"] == "1"
    asyncio.run(scenario())


def test_superseded_delayed_baseline_never_overwrites_newer_confirmed_cache(tmp_path):
    from tokensql.workspace.source_reading import record_source_baseline
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        # Freeze an approved payload, simulating a crash after formal commit but
        # before the first baseline receipt is written.
        internal = manager.workflow._job(first["id"])
        original = store.record_supplier_baseline
        def unavailable(*args, **kwargs):
            raise OSError("baseline outage")
        store.record_supplier_baseline = unavailable
        with pytest.raises(OSError):
            await manager.workflow.confirm(first["id"], first["revision"])
        pending = deepcopy(manager.workflow._job(first["id"]))
        store.record_supplier_baseline = original
        newer = deepcopy(pending["baseline_update"])
        newer["import_id"] = "newer-human-confirmation"
        store.record_supplier_baseline(**newer)
        record_source_baseline(manager.workflow, pending)
        assert pending["baseline_status"] == "superseded"
        assert store.get_supplier_baseline(sid, "monthly")["import_id"] == "newer-human-confirmation"
    asyncio.run(scenario())


def test_next_supplier_task_retries_committed_receipt_and_blocks_persistent_storage_failure(tmp_path, monkeypatch):
    from tokensql.workspace.store import RevisionConflictError
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        original = store.record_supplier_baseline
        def unavailable(*args, **kwargs):
            raise OSError("baseline outage")
        monkeypatch.setattr(store, "record_supplier_baseline", unavailable)
        with pytest.raises(OSError):
            await manager.workflow.confirm(first["id"], first["revision"])
        before = len(manager.workflow.state["jobs"])
        with pytest.raises(RevisionConflictError, match="already committed"):
            await manager.submit(prompt="Next", supplier_id=sid)
        assert len(manager.workflow.state["jobs"]) == before
        monkeypatch.setattr(store, "record_supplier_baseline", original)
        repeated = await submit(manager, sid, workbook())
        assert manager.workflow.get(first["id"])["status"] == "confirmed"
        assert repeated["metrics"]["semantic_requests"] == 0
    asyncio.run(scenario())


def test_global_rule_changed_after_parsing_cannot_stamp_old_output_with_new_policy(tmp_path):
    async def scenario():
        manager, store, sid, calls, packets = setup(tmp_path)
        first = await submit(manager, sid, workbook())
        memory = manager.memory.read()
        manager.memory.update([{"scope": "workspace", "text": "A new interpretation policy added after this draft was parsed"}], memory["revision"])
        await manager.workflow.confirm(first["id"], first["revision"])
        repeated = await submit(manager, sid, workbook())
        assert repeated["metrics"]["semantic_requests"] == 1
        assert repeated["reading_plan"]["reuse_mode"] == "none"
    asyncio.run(scenario())
