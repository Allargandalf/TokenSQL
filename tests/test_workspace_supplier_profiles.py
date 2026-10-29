"""Synthetic supplier profile persistence, scope and concurrency acceptance."""
from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.store import IdempotencyConflictError, RevisionConflictError, WorkspaceStore


@pytest.fixture
def store():
    value = WorkspaceStore(":memory:")
    value.apply_bundle({"models": [{"model_id": "canonical-A"}, {"model_id": "canonical-B"}]})
    try:
        yield value
    finally:
        value.close()


def supplier(store, name="Synthetic supplier"):
    return store.confirm_supplier({"name": name})["supplier_id"]


def mapping(model="canonical-A", **changes):
    return {"raw_name": "vendor label", "aliases": ["vendor alias"],
            "model_id": model, **changes}


def test_missing_profile_is_revision_zero_and_reads_do_not_write(store):
    sid = supplier(store)
    before = store._conn.total_changes
    empty = store.get_supplier_profile(sid, "monthly")
    assert empty["revision"] == 0 and empty["sources"] == {}
    assert empty["effective"]["model_mappings"] == []
    assert empty["effective"]["default_prompt"] is None
    assert store.supplier_profile_history(sid) == []
    assert store._conn.total_changes == before
    with pytest.raises(ValueError, match="supplier"):
        store.get_supplier_profile("missing")


def test_profile_is_persistent_revisioned_and_audited(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    store = WorkspaceStore(path)
    store.apply_bundle({"models": [{"model_id": "canonical-A"}]})
    sid = supplier(store)
    state = store.update_supplier_profile(sid, {
        "expected_revision": 0, "default_prompt": "Use supplied units",
        "model_mappings": [mapping()], "rules": [{"text": "Prices exclude tax"}]})
    assert state["revision"] == 1
    assert state["model_mappings"][0]["id"].startswith("mapping-")
    assert state["rules"][0]["id"].startswith("rule-")
    with pytest.raises(RevisionConflictError):
        store.update_supplier_profile(sid, {"expected_revision": 0, "rules": []})
    store.close()
    store = WorkspaceStore(path)
    try:
        state = store.get_supplier_profile(sid)
        assert state["revision"] == 1 and state["default_prompt"] == "Use supplied units"
        edited = store.update_supplier_profile(sid, {"expected_revision": 1, "rules": []})
        assert edited["revision"] == 2 and edited["model_mappings"] == state["model_mappings"]
        assert edited["default_prompt"] == state["default_prompt"]
        history = store.supplier_profile_history(sid)
        assert [item["revision"] for item in history] == [2, 1]
        assert history[1]["profile"]["rules"][0]["text"] == "Prices exclude tax"
        assert store._conn.execute(
            "SELECT COUNT(*) FROM pricing_audit WHERE entity_type='supplier_profile'").fetchone()[0] == 2
    finally:
        store.close()


def test_supplier_scope_and_channel_aliases_are_isolated(store):
    sid = supplier(store)
    other = supplier(store, "Other supplier")
    state = store.update_supplier_profile(sid, {
        "expected_revision": 0, "default_prompt": "Supplier default",
        "model_mappings": [mapping(), mapping(supplier_model_id="premium")],
        "rules": [{"id": "currency", "text": "Currency is CNY"}],
        "sources": {"monthly": {
            "default_prompt": "Monthly default",
            "model_mappings": [mapping("canonical-B", raw_name="vendor alias", aliases=[])],
            "rules": [{"id": "currency", "text": "Currency is USD"}]}}})
    effective = store.get_supplier_profile(sid, "monthly")["effective"]
    assert effective["default_prompt"] == "Monthly default"
    assert [(item["model_id"], item["supplier_model_id"]) for item in effective["model_mappings"]] == [
        ("canonical-A", "premium"), ("canonical-B", None)]
    assert [item["text"] for item in effective["rules"]] == ["Currency is USD"]
    assert store.get_supplier_profile(sid, "other")["effective"]["default_prompt"] == "Supplier default"
    assert store.get_supplier_profile(other, "monthly")["revision"] == 0
    assert store.get_supplier_profile(sid, "monthly")["effective_fingerprint"] != (
        store.get_supplier_profile(sid, "other")["effective_fingerprint"])
    assert "source_scope" not in state


def test_confirmed_profile_merge_is_idempotent_and_quotes_unchanged(store):
    sid = supplier(store)
    before = store.catalog_snapshot()
    state = store.record_confirmed_supplier_profile(sid, "monthly", "confirmed-1",
        model_mappings=[mapping()], rules=[{"text": "Input columns use CNY/mtoken"}],
        evidence={"source_ref": "Prices header", "confirmed_by": "human"})
    assert state["revision"] == 1 and state["confirmed_imports"] == ["confirmed-1"]
    row = state["sources"]["monthly"]["model_mappings"][0]
    assert row["evidence"]["import_id"] == "confirmed-1"
    assert row["evidence"]["source"] == "confirmed_review"
    again = store.record_confirmed_supplier_profile(sid, "monthly", "confirmed-1",
        model_mappings=[mapping("canonical-B")])
    assert again == state
    with pytest.raises(IdempotencyConflictError):
        store.record_confirmed_supplier_profile(sid, "different", "confirmed-1")
    changed = store.record_confirmed_supplier_profile(sid, "monthly", "confirmed-2",
        model_mappings=[mapping("canonical-B")], rules=[{"text": "Input columns use CNY/mtoken"}])
    assert changed["revision"] == 2
    assert len(changed["sources"]["monthly"]["model_mappings"]) == 1
    assert len(changed["sources"]["monthly"]["rules"]) == 1
    assert changed["sources"]["monthly"]["model_mappings"][0]["id"] == row["id"]
    assert changed["sources"]["monthly"]["model_mappings"][0]["model_id"] == "canonical-B"
    assert store.catalog_snapshot() == before


def test_invalid_mapping_and_failed_confirm_leave_no_profile_or_import_memory(store):
    sid = supplier(store)
    for bad in ([mapping(), mapping("canonical-B")],
                [mapping(model="unknown-canonical")],
                [mapping(raw_name=" ", aliases=[])]):
        with pytest.raises(ValueError):
            store.update_supplier_profile(sid, {"expected_revision": 0, "model_mappings": bad})
        assert store.get_supplier_profile(sid)["revision"] == 0
    with pytest.raises(ValueError):
        store.record_confirmed_supplier_profile(sid, "monthly", "failed-1",
            model_mappings=[mapping(model="missing")])
    assert store.get_supplier_profile(sid)["confirmed_imports"] == []
    assert store.supplier_profile_history(sid) == []


def test_api_requires_revision_and_supports_edit_history(store):
    sid = supplier(store)
    app = FastAPI()
    app.include_router(create_data_router(store))
    url = "/api/workspace/suppliers/" + sid + "/profile"
    with TestClient(app) as client:
        assert client.get(url).json()["revision"] == 0
        assert client.put(url, json={"rules": []}).status_code == 422
        response = client.put(url, json={"expected_revision": 0,
            "sources": {"monthly": {"rules": [{"kind": "unit", "text": "CNY per million tokens"}]}}})
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert client.put(url, json={"expected_revision": 0, "rules": []}).status_code == 409
        scoped = client.get(url, params={"source_scope": "monthly"}).json()
        assert scoped["effective"]["rules"][0]["text"] == "CNY per million tokens"
        history = client.get(url + "/history").json()["items"]
        assert len(history) == 1 and history[0]["revision"] == 1
        assert client.put(url, json={"expected_revision": 1,
            "model_mappings": [mapping("missing")]}).status_code == 422

def test_confirmation_preflight_is_read_only_and_allows_pending_model_stubs(store):
    sid = supplier(store)
    before = store._conn.total_changes
    payload = {"model_mappings": [mapping("pending-model")]}
    with pytest.raises(ValueError, match="canonical model"):
        store.preview_confirmed_supplier_profile(sid, "monthly", "pending", **payload)
    preview = store.preview_confirmed_supplier_profile(sid, "monthly", "pending",
        allowed_model_ids=["pending-model"], **payload)
    assert preview["valid"] and preview["base_revision"] == 0
    assert store._conn.total_changes == before
    assert store.get_supplier_profile(sid)["revision"] == 0
    with pytest.raises(ValueError):
        store.record_confirmed_supplier_profile(sid, "monthly", "pending", **payload)
    assert store.get_supplier_profile(sid)["confirmed_imports"] == []
    store.apply_bundle({"models": [{"model_id": "pending-model"}]})
    result = store.record_confirmed_supplier_profile(sid, "monthly", "pending", **payload)
    assert result["revision"] == 1
    assert store.preview_confirmed_supplier_profile(sid, "monthly", "pending",
        **payload)["already_recorded"]


def test_confirmed_provenance_cannot_be_overridden_by_business_evidence(store):
    sid = supplier(store)
    result = store.record_confirmed_supplier_profile(sid, "monthly", "actual-receipt",
        rules=[{"text": "Use CNY", "evidence": {"import_id": "fake", "source": "fake"}}])
    evidence = result["sources"]["monthly"]["rules"][0]["evidence"]
    assert evidence["import_id"] == "actual-receipt" and evidence["source"] == "confirmed_review"


def test_profile_meaning_fingerprint_ignores_receipt_ids_and_edit_order(store):
    sid = supplier(store)
    first = store.record_confirmed_supplier_profile(sid, "monthly", "receipt-1",
        model_mappings=[mapping()], rules=[{"id": "old-rule-id", "text": "Use CNY"}])
    initial = store.get_supplier_profile(sid, "monthly")["effective_fingerprint"]
    store.record_confirmed_supplier_profile(sid, "monthly", "receipt-2",
        model_mappings=[mapping(id="new-mapping-id")],
        rules=[{"id": "new-rule-id", "text": "Use CNY"}])
    assert store.get_supplier_profile(sid, "monthly")["effective_fingerprint"] == initial
    state = store.get_supplier_profile(sid)
    store.update_supplier_profile(sid, {"expected_revision": state["revision"],
        "sources": {"monthly": {"model_mappings": [mapping()],
                                 "rules": [{"text": "Use USD"}]}}})
    assert store.get_supplier_profile(sid, "monthly")["effective_fingerprint"] != initial


def test_automatic_mapping_capture_preserves_aliases_until_explicit_human_edit(store):
    sid = supplier(store)
    store.record_confirmed_supplier_profile(sid, "monthly", "receipt-1",
        model_mappings=[mapping()])
    second = store.record_confirmed_supplier_profile(sid, "monthly", "receipt-2",
        model_mappings=[mapping(aliases=[])])
    assert second["sources"]["monthly"]["model_mappings"][0]["aliases"] == ["vendor alias"]
    third = store.update_supplier_profile(sid, {"expected_revision": second["revision"],
        "sources": {"monthly": {"model_mappings": [mapping(aliases=[])]}}})
    assert third["sources"]["monthly"]["model_mappings"][0]["aliases"] == []
