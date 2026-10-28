"""Deterministic fixed-two-place discount policy in review, memory and comparison."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.compare import compare_snapshots
from tokensql.workspace.discount import DISCOUNT_POLICY_VERSION
from tokensql.workspace.draft_cli import prepare
from tokensql.workspace.memory import AgentMemory, atomic_json
from tokensql.workspace.store import WorkspaceStore
from tokensql.workspace.workflow import normalize_bundle_discounts


def setup_manager(tmp_path):
    config = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                         python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "agent",
                         input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                         data_dir=tmp_path / "data")
    store = WorkspaceStore(config.database_path)
    return OpenCodeAgentManager(config=config, store=store), store


def test_bundle_normalization_is_scoped_and_preserves_amount_and_literals():
    original = {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "amount": "1.2345678912345", "discount_rate": "0.155",
                            "conditions": {"discount_rate": "0.155"}}],
                "update": [{"id": 1, "expected_revision": 2, "changes": {"amount": "8.123456789", "discount_rate": "0.145"}}],
                "official_prices": [{"model_id": "m", "amount": "12.3456789"}],
                "note": "raw source 0.155"}
    result = normalize_bundle_discounts(original)
    assert result["create"][0]["discount_rate"] == "0.16"
    assert result["update"][0]["changes"]["discount_rate"] == "0.15"
    assert result["create"][0]["amount"] == original["create"][0]["amount"]
    assert result["create"][0]["conditions"] == original["create"][0]["conditions"]
    assert result["official_prices"] == original["official_prices"]
    assert original["create"][0]["discount_rate"] == "0.155"


def test_new_draft_prepare_and_restart_share_one_policy(tmp_path):
    manager, store = setup_manager(tmp_path)
    bundle = {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.155", "amount": "1.2345678912345"}]}
    prepared = prepare(bundle, store.catalog_snapshot())
    assert prepared["bundle"]["create"][0]["discount_rate"] == "0.16"
    job = manager.workflow.create_draft(bundle)
    assert job["discount_policy_version"] == DISCOUNT_POLICY_VERSION
    assert job["bundle"]["create"][0]["discount_rate"] == "0.16"
    assert manager.workflow._job(job["id"])["original_bundle"]["create"][0]["discount_rate"] == "0.16"
    assert job["preview"]["items"][0]["discount_rate"] == "0.16"
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    assert restored.workflow.get(job["id"])["revision"] == job["revision"]
    assert store.catalog_snapshot()["quotes"] == []
    asyncio.run(restored.workflow.confirm(job["id"], job["revision"]))
    row = store.catalog_snapshot()["quotes"][0]
    assert row["discount_rate"] == "0.16" and row["amount"] == "1.2345678912345"
    assert restored.memory.read()["rules"] == []


def test_accept_and_manual_revise_normalize_before_preview(tmp_path):
    manager, store = setup_manager(tmp_path)
    job = manager.workflow._new_job("Synthetic", None, [])
    manager.workflow.accept_proposal(job["id"], {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.145"}]})
    accepted = manager.workflow.get(job["id"])
    assert accepted["bundle"]["create"][0]["discount_rate"] == "0.15"
    assert accepted["preview"]["items"][0]["discount_rate"] == "0.15"
    revised = asyncio.run(manager.workflow.revise(job["id"], accepted["revision"],
                        {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.155"}]}))
    assert revised["bundle"]["create"][0]["discount_rate"] == "0.16"
    assert revised["preview"]["items"][0]["discount_rate"] == "0.16"
    assert revised["discount_policy_version"] == DISCOUNT_POLICY_VERSION
    assert store.catalog_snapshot()["quotes"] == []


def test_legacy_ready_normalizes_once_preserving_files_messages_and_history(tmp_path):
    manager, store = setup_manager(tmp_path)
    upload = manager.workflow.upload([AgentUpload("synthetic.xlsx", b"immutable synthetic workbook")])[0]
    current = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m"}]})
    state = deepcopy(manager.workflow.state)
    job = state["jobs"][current["id"]]
    job.pop("discount_policy_version")
    old_bundle = {"create": [
        {"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.155", "amount": "1.23456789123"},
        {"supplier": "Synthetic Supplier", "model_id": "zero", "discount_rate": "0"},
        {"supplier": "Synthetic Supplier", "model_id": "unknown", "discount_rate": None},
        {"supplier": "Synthetic Supplier", "model_id": "bare"}]}
    job.update(bundle=deepcopy(old_bundle), original_bundle=deepcopy(old_bundle),
               conversation_id="ses_original", assistant_message="Source says 0.155",
               messages=[{"role": "assistant", "text": "Source says 0.155"}])
    for status in ("confirmed", "failed"):
        historical = deepcopy(job)
        historical.update(id=status, status=status)
        state["jobs"][status] = historical
    historic = {key: deepcopy(state["jobs"][key]) for key in ("confirmed", "failed")}
    atomic_json(manager.workflow.path, state)
    memory_before = manager.memory.read()
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    result = restored.workflow.get(job["id"])
    assert result["status"] == "ready" and result["revision"] == current["revision"] + 1
    assert result["conversation_id"] == job["conversation_id"]
    assert result["assistant_message"] == job["assistant_message"] and result["messages"] == job["messages"]
    rows = result["bundle"]["create"]
    assert rows[0]["discount_rate"] == "0.16" and rows[0]["amount"] == "1.23456789123"
    assert rows[1]["discount_rate"] == "0.00"
    assert rows[2]["discount_rate"] is None and "discount_rate" not in rows[3]
    assert restored.workflow._job(job["id"])["original_bundle"] == result["bundle"]
    assert restored.memory.read() == memory_before
    for key, expected in historic.items():
        assert restored.workflow._job(key) == expected
    uploaded = restored.workflow.state["uploads"][upload["id"]]
    assert restored.workflow._location(uploaded).read_bytes() == b"immutable synthetic workbook"
    assert uploaded["status"] == "input"
    assert store.catalog_snapshot()["quotes"] == []
    again = OpenCodeAgentManager(config=manager.config, store=store)
    assert again.workflow.get(job["id"]) == result
    async def confirm():
        with pytest.raises(ValueError, match="revision conflict"):
            await again.workflow.confirm(job["id"], current["revision"])
        assert (await again.workflow.confirm(job["id"], result["revision"]))["status"] == "confirmed"
    asyncio.run(confirm())
    assert len(store.catalog_snapshot()["quotes"]) == 4
    assert again.memory.read()["rules"] == []


@pytest.mark.parametrize("bad", ["-0.0001", "NaN", 0.8, True])
def test_invalid_legacy_draft_requires_manual_revision_without_crashing_or_repeating(tmp_path, bad):
    manager, store = setup_manager(tmp_path)
    current = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m"}]})
    state = deepcopy(manager.workflow.state)
    job = state["jobs"][current["id"]]
    job.pop("discount_policy_version")
    job["bundle"]["create"][0]["discount_rate"] = bad
    job["original_bundle"] = deepcopy(job["bundle"])
    atomic_json(manager.workflow.path, state)
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    result = restored.workflow.get(job["id"])
    assert result["status"] == "ready" and result["discount_policy_warning"]
    assert result["preview"] is None
    assert result["revision"] == current["revision"] + 1
    assert result["bundle"]["create"][0]["discount_rate"] == bad
    again = OpenCodeAgentManager(config=manager.config, store=store)
    assert again.workflow.get(job["id"]) == result
    async def revise():
        with pytest.raises(ValueError, match="manual revision"):
            await again.workflow.confirm(job["id"], result["revision"])
        revised = await again.workflow.revise(job["id"], result["revision"],
                      {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.155"}]})
        assert "discount_policy_warning" not in revised and revised["bundle"]["create"][0]["discount_rate"] == "0.16"
    asyncio.run(revise())
    assert store.catalog_snapshot()["quotes"] == []


def test_precision_policy_only_does_not_become_human_memory(tmp_path):
    memory = AgentMemory(tmp_path / "agent")
    memory.remember_confirmed("policy", {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.1544"}]},
                              {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.15"}]}, ["synthetic.xlsx"])
    assert memory.read()["rules"] == []
    memory.remember_confirmed("human", {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.15"}]},
                              {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.16"}]}, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 1
    memory.remember_confirmed("literal", {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "conditions": {"discount_rate": "0.1544"}}]},
                              {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "conditions": {"discount_rate": "0.15"}}]}, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 2
    memory.remember_confirmed("update-policy", {"update": [{"id": 1, "changes": {"discount_rate": "0.1544"}}]},
                              {"update": [{"id": 1, "changes": {"discount_rate": "0.15"}}]}, ["synthetic.xlsx"])
    assert len(memory.read()["rules"]) == 2


def test_comparison_uses_same_discount_policy_but_keeps_amount_precision():
    def snapshot(rate, amount="1"):
        return {"models": [{"model_id": "m"}],
                "quotes": [{"model_id": "m", "amount": amount, "discount_rate": rate,
                            "currency": "CNY", "unit": "token", "unit_quantity": "1000000"}]}
    assert compare_snapshots(snapshot("0.155"), snapshot("0.16"))["pass"]
    assert not compare_snapshots(snapshot("0.154"), snapshot("0.16"))["pass"]
    assert not compare_snapshots(snapshot("-0.0001"), snapshot("0.00"))["pass"]
    assert not compare_snapshots(snapshot("0.16", "1.0000001"), snapshot("0.16"))["pass"]



def test_legacy_update_ratio_normalizes_and_keeps_original_revision_guard(tmp_path):
    manager, store = setup_manager(tmp_path)
    inserted = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.80"}]})["items"][0]
    current = manager.workflow.create_draft({"update": [{"id": inserted["id"],
                           "expected_revision": inserted["revision"], "changes": {"discount_rate": "0.16"}}]})
    state = deepcopy(manager.workflow.state)
    job = state["jobs"][current["id"]]
    job.pop("discount_policy_version")
    job["bundle"]["update"][0]["changes"]["discount_rate"] = "0.155"
    job["original_bundle"] = deepcopy(job["bundle"])
    atomic_json(manager.workflow.path, state)
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    result = restored.workflow.get(job["id"])
    update = result["bundle"]["update"][0]
    assert update["changes"]["discount_rate"] == "0.16"
    assert update["expected_revision"] == inserted["revision"]
    assert result["preview"]["items"][0]["discount_rate"] == "0.16"
    assert store.get_quote(inserted["id"]).model_dump(mode="json")["discount_rate"] == "0.80"
    asyncio.run(restored.workflow.confirm(job["id"], result["revision"]))
    assert store.get_quote(inserted["id"]).model_dump(mode="json")["discount_rate"] == "0.16"
    assert restored.memory.read()["rules"] == []


def test_old_review_without_original_baseline_does_not_invent_corrections(tmp_path):
    manager, store = setup_manager(tmp_path)
    current = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m"}]})
    state = deepcopy(manager.workflow.state)
    job = state["jobs"][current["id"]]
    job.pop("discount_policy_version")
    job["bundle"]["create"][0]["discount_rate"] = "0.155"
    job["original_bundle"] = None
    atomic_json(manager.workflow.path, state)
    restored = OpenCodeAgentManager(config=manager.config, store=store)
    result = restored.workflow.get(job["id"])
    assert result["bundle"]["create"][0]["discount_rate"] == "0.16"
    asyncio.run(restored.workflow.confirm(job["id"], result["revision"]))
    assert restored.memory.read()["rules"] == []


def test_catalog_drift_still_requires_new_review_under_current_precision(tmp_path):
    manager, store = setup_manager(tmp_path)
    job = manager.workflow.create_draft({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.155"}]})
    store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "independent", "amount": "9.87654321"}]})
    async def confirm():
        from tokensql.workspace.store import RevisionConflictError
        with pytest.raises(RevisionConflictError, match="Catalog changed"):
            await manager.workflow.confirm(job["id"], job["revision"])
    asyncio.run(confirm())
    refreshed = manager.workflow.get(job["id"])
    assert refreshed["status"] == "ready" and refreshed["revision"] == job["revision"] + 1
    assert refreshed["bundle"]["create"][0]["discount_rate"] == "0.16"
    assert [row["model_id"] for row in store.catalog_snapshot()["quotes"]] == ["independent"]


def test_current_system_policy_explicitly_requires_code_normalization(tmp_path):
    manager, _ = setup_manager(tmp_path)
    prompt = manager._system_prompt(Path("/run"), Path("/catalog.json"))
    for token in ("two-decimal payment-ratio storage policy", "ROUND_HALF_UP", "0.80", "0.15",
                  "reject original negatives before rounding", "do not round original amount or FX values"):
        assert token in prompt
