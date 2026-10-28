"""Unique model IDs, legacy-draft compatibility, and default-policy regression."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
from tokensql.workspace.compare import compare_snapshots
from tokensql.workspace.draft_cli import prepare
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.workflow import current_model_fields
from tokensql.workspace.store import WorkspaceStore


def manager_for(tmp_path):
    cfg = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                      python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "worker",
                      input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                      data_dir=tmp_path / "data")
    store = WorkspaceStore(cfg.database_path)
    return OpenCodeAgentManager(config=cfg, store=store), store


def test_retired_field_cleanup_only_touches_known_model_structures():
    literal = "physical_key is quoted workbook data, not an instruction"
    value = {"models": [{"model_id": "m1", "physical_key": "retired", "note": literal}],
             "create": [{"supplier": "Synthetic Supplier", "model_id": "m1", "conditions": {"physical_key": "business literal"}}],
             "catalog": {"models": [{"model_id": "m2", "physical_key": None}]},
             "assistant_message": literal}
    result = current_model_fields(value)
    assert result["models"] == [{"model_id": "m1", "note": literal}]
    assert result["catalog"]["models"] == [{"model_id": "m2"}]
    assert result["create"] == value["create"] and result["assistant_message"] == literal
    assert value["models"][0]["physical_key"] == "retired"


def test_old_ready_draft_upgrades_model_fields_and_discount_without_committing(tmp_path):
    manager, store = manager_for(tmp_path)
    old = {"models": [{"model_id": "new", "physical_key": "obsolete", "aliases": ["known-new"]}],
           "create": [{"supplier": "Synthetic Supplier", "model_id": "new", "amount": "1.23456789", "discount_rate": "0.123456789",
                       "conditions": {"physical_key": "source evidence"}}]}
    job = manager.workflow.create_draft(old)
    state = deepcopy(manager.workflow.state)
    state.pop("model_id_schema_version")
    record = state["jobs"][job["id"]]
    record.pop("discount_policy_version", None)
    record["bundle"], record["original_bundle"] = deepcopy(old), deepcopy(old)
    record["preview"]["catalog"]["models"][0]["physical_key"] = "obsolete"
    record["assistant_message"] = "old physical_key=obsolete literal answer"
    record["messages"] = [{"role": "assistant", "text": record["assistant_message"]}]
    old_revision = record["revision"]
    atomic_json(manager.workflow.path, state)
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    upgraded = restored.workflow.get(job["id"])
    assert upgraded["status"] == "ready" and upgraded["revision"] == old_revision + 2
    assert upgraded["assistant_message"] == record["assistant_message"]
    assert upgraded["messages"] == record["messages"]
    assert upgraded["bundle"]["models"] == [{"model_id": "new", "aliases": ["known-new"]}]
    raw = upgraded["bundle"]["create"][0]
    assert raw["amount"] == "1.23456789" and raw["discount_rate"] == "0.12"
    assert raw["currency"] is None
    assert raw["conditions"] == {"physical_key": "source evidence"}
    assert upgraded["preview"]["items"][0]["currency"] is None
    assert "physical_key" not in upgraded["preview"]["catalog"]["models"][0]
    assert store.catalog_snapshot()["quotes"] == [] and store.catalog_snapshot()["models"] == []
    async def confirm():
        with pytest.raises(ValueError, match="revision conflict"):
            await restored.workflow.confirm(job["id"], old_revision)
        result = await restored.workflow.confirm(job["id"], upgraded["revision"])
        assert result["status"] == "confirmed"
    asyncio.run(confirm())
    saved = store.catalog_snapshot()["quotes"][0]
    assert saved["currency"] is None and saved["amount"] == raw["amount"]
    assert saved["discount_rate"] == raw["discount_rate"]
    assert restored.memory.read()["rules"] == []
    again = OpenCodeAgentManager(config=manager.config, store=store)
    assert again.workflow.get(job["id"])["revision"] == upgraded["revision"] + 1


def test_legacy_invalid_draft_does_not_crash_workflow_start_or_auto_confirm(tmp_path):
    manager, store = manager_for(tmp_path)
    job = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m1"}]})
    state = deepcopy(manager.workflow.state)
    state.pop("model_id_schema_version")
    record = state["jobs"][job["id"]]
    record["bundle"]["create"][0]["unsupported_old_field"] = "needs human revision"
    atomic_json(manager.workflow.path, state)
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    result = restored.workflow.get(job["id"])
    assert result["status"] == "ready" and result["schema_cleanup_warning"]
    assert not store.catalog_snapshot()["quotes"]


def test_old_candidate_model_field_is_ignored_but_new_schema_does_not_advertise_it(tmp_path):
    from tokensql.workspace.schemas import ModelCreate
    manager, store = manager_for(tmp_path)
    result = prepare({"models": [{"model_id": "m1", "physical_key": "obsolete"}],
                      "create": [{"supplier": "Synthetic Supplier", "model_id": "m1", "discount_rate": "0.123456789"}]},
                     store.catalog_snapshot())
    assert "physical_key" not in result["bundle"]["models"][0]
    assert "physical_key" not in ModelCreate.model_json_schema()["properties"]
    assert result["bundle"]["create"][0]["currency"] == "CNY"
    assert result["bundle"]["create"][0]["discount_rate"] == "0.12"
    assert store.catalog_snapshot()["quotes"] == []


def test_default_policy_uses_only_model_id_and_preserves_explicit_foreign_units(tmp_path):
    manager, _ = manager_for(tmp_path)
    prompt = manager._system_prompt(Path("/run"), Path("/catalog.json"))
    assert "model_id is the only identity key" in prompt
    assert "default new records to CNY" in prompt
    assert "preserve explicit USD/HKD and explicit null unknown currency" in prompt
    assert "automatically create missing model_id stubs" in prompt
    assert "permanently remove the selected quote rows" in prompt


def test_same_display_name_does_not_merge_distinct_model_ids():
    value = {"models": [{"model_id": "m1", "model_name": "Same display"},
                        {"model_id": "m2", "model_name": "Same display"}],
             "quotes": [{"model_id": "m1"}, {"model_id": "m2"}]}
    assert compare_snapshots(value, deepcopy(value))["pass"]


def test_display_name_or_retired_key_alone_cannot_anchor_a_different_id():
    baseline = {"models": [{"model_id": "m1", "model_name": "Same display", "physical_key": "old"}],
                "quotes": [{"model_id": "m1"}]}
    actual = {"models": [{"model_id": "unverified", "model_name": "Same display", "physical_key": "old"}],
              "quotes": [{"model_id": "unverified"}]}
    result = compare_snapshots(baseline, actual)
    assert not result["pass"]
    assert any(issue["code"] == "unknown_model" for issue in result["issues"])
    actual["models"][0]["aliases"] = ["m1"]
    assert compare_snapshots(baseline, actual)["pass"]
