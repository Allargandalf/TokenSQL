"""Synthetic supplier requirements and atomic scoped update acceptance."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.cli import main as cli_main
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.draft_cli import prepare
from tokensql.workspace.reconciliation import build_reconciliation, entry_key
from tokensql.workspace.schemas import QuoteCreate
from tokensql.workspace.store import IdempotencyConflictError, RevisionConflictError, WorkspaceStore


@pytest.fixture
def store():
    value = WorkspaceStore(":memory:")
    try:
        yield value
    finally:
        value.close()


def supplier(store, name="Synthetic Supplier"):
    return store.confirm_supplier({"name": name})["supplier_id"]


def quote(model="A", amount="1", **changes):
    return {"model_id": model, "amount": amount, "billing_metric": "input",
            "pricing_unit_id": "cny_mtoken", "source_sheet": "Prices", **changes}


def request(supplier_id, entries=None, **changes):
    return {"supplier_id": supplier_id, "source_scope": "monthly-prices",
            "file_sha256": "a" * 64, "latest_confirmed": True,
            "entries": entries or [], **changes}


def apply(store, payload, import_id=None):
    review = store.preview_reconciliation(payload)
    assert review["ready"], review["groups"]["needs_confirmation"]
    return store.apply_reconciliation(payload, import_id=import_id,
        expected_catalog_token=review["base_catalog_token"], expected_review_digest=review["review_digest"])


def test_missing_supplier_is_a_draft_and_all_formal_writes_are_atomic(store, tmp_path, capsys):
    assert QuoteCreate.model_validate({"model_id": "draft"}).supplier is None
    before = store.catalog_snapshot()
    for payload in ({"create": [{"model_id": "missing"}]},
                    {"models": [{"model_id": "would-create"}], "create": [{"model_id": "missing"}]},
                    {"create": [{"model_id": "good", "supplier": "Confirmed"}, {"model_id": "missing"}]}):
        with pytest.raises(ValueError, match="supplier"):
            store.apply_bundle(payload)
        assert store.catalog_snapshot() == before
    created = store.create_many([{"model_id": "good", "supplier": "Confirmed"}])[0][0]
    unchanged = store.catalog_snapshot()
    for change in ({"supplier": None}, {"supplier": " "}, {"supplier": "unknown"},
                   {"supplier_id": "does-not-exist"}):
        with pytest.raises(ValueError, match="supplier"):
            store.update_quote(created.id, change, expected_revision=1)
        assert store.catalog_snapshot() == unchanged
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        for url, payload in (("/quotes", {"items": [{"model_id": "missing"}]}),
                             ("/quotes/preview", {"items": [{"model_id": "missing"}]}),
                             ("/catalog", {"bundle": {"create": [{"model_id": "missing"}]}})):
            assert client.post("/api/workspace" + url, json=payload).status_code == 422
        assert client.post("/api/workspace/catalog", json={"bundle": {
            "models": [{"model_id": "official"}],
            "official_prices": [{"model_id": "official", "amount": "10"}]}}).status_code == 200
    source = tmp_path / "missing.json"
    source.write_text(json.dumps({"create": [{"model_id": "missing"}]}))
    assert cli_main(["--db", ":memory:", "apply", str(source)]) == 2
    assert "supplier" in capsys.readouterr().out


def test_supplier_alias_and_rename_preserve_identity_without_fuzzy_merging(store):
    first = store.confirm_supplier({"name": "Supplier-A"})
    other = store.confirm_supplier({"name": "SupplierA"})
    assert first["supplier_id"] != other["supplier_id"]
    row = store.create_many([{"model_id": "A", "supplier_id": first["supplier_id"]}])[0][0]
    renamed = store.confirm_supplier({"supplier_id": first["supplier_id"], "name": "Canonical Supplier",
                                     "aliases": ["Confirmed Alias"], "expected_revision": 1})
    assert renamed["supplier_id"] == first["supplier_id"] and renamed["revision"] == 2
    assert store.get_quote(row.id).supplier == "Canonical Supplier"
    alias = store.create_many([{"model_id": "B", "supplier": "Confirmed Alias"}])[0][0]
    assert alias.supplier_id == row.supplier_id and alias.supplier == "Canonical Supplier"
    with pytest.raises(ValueError, match="another supplier"):
        store.confirm_supplier({"supplier_id": first["supplier_id"], "name": "SupplierA", "expected_revision": 2})
    with pytest.raises(RevisionConflictError):
        store.confirm_supplier({"supplier_id": first["supplier_id"], "name": "New", "expected_revision": 1})


def test_price_file_and_row_order_are_not_identity_and_token_scale_is_normalized(store):
    sid = supplier(store)
    first = apply(store, request(sid, [quote("A", "1"), quote("B", "2")]), "first")
    ids = {value["model_id"]: value["id"] for value in first["items"]}
    reordered = request(sid, [quote("B", "2", source="renamed.xlsx", source_ref="Sheet!C20"),
                              quote("A", "1", source="renamed.xlsx", source_ref="Sheet!C30")],
                        file_sha256="b" * 64)
    review = store.preview_reconciliation(reordered)
    assert not review["groups"]["create"] and not review["groups"]["update"]
    assert len(review["groups"]["unchanged"]) == 2
    changed = request(sid, [quote("A", "0.002", pricing_unit_id="cny_ktoken")], file_sha256="c" * 64)
    result = apply(store, changed)
    assert result["items"][0]["id"] == ids["A"] and result["items"][0]["revision"] == 2
    assert len(store.catalog_snapshot()["quotes"]) == 2


def test_full_replace_scopes_models_sheets_channels_and_manual_sources(store):
    sid = supplier(store)
    other = supplier(store, "Other Supplier")
    initial = request(sid, [quote("A"), quote("C")])
    first = apply(store, initial)
    ids = {value["model_id"]: value["id"] for value in first["items"]}
    store.create_many([{"model_id": "A", "supplier_id": sid, "amount": "9"},
                       quote("outside-model", supplier_id=sid, source_scope="monthly-prices"),
                       quote("outside-sheet", supplier_id=sid, source_scope="monthly-prices", source_sheet="Other"),
                       quote("outside-source", supplier_id=sid, source_scope="different-series"),
                       quote("outside-supplier", supplier_id=other, source_scope="monthly-prices"),
                       quote("outside-channel", supplier_id=sid, source_scope="monthly-prices", supplier_model_id="premium")])
    payload = request(sid, [quote("A", "3"), quote("B", "2")], mode="full", scope_confirmed=True,
                      expected_sheets=["Prices"], covered_sheets=["Prices"], complete_read=True,
                      model_ids=["A", "B", "C"])
    review = store.preview_reconciliation(payload)
    assert [len(review["groups"][key]) for key in ("create", "update", "delete")] == [1, 1, 1]
    assert review["groups"]["delete"][0]["before"]["id"] == ids["C"]
    result = apply(store, payload)
    assert result["changes"]["added"] == result["changes"]["updated"] == result["changes"]["deleted"] == 1
    assert store.get_quote(ids["A"]).revision == 2
    remaining = store.catalog_snapshot()["quotes"]
    assert len(remaining) == 8 and all(value["model_id"] != "C" for value in remaining)


@pytest.mark.parametrize("changes", [
    {"scope_confirmed": False}, {"complete_read": False}, {"covered_sheets": []},
    {"expected_sheets": []}, {"unresolved_items": 1}])
def test_incomplete_full_never_generates_missing_deletes(store, changes):
    sid = supplier(store)
    apply(store, request(sid, [quote("A"), quote("C")]))
    payload = request(sid, [quote("A", "2")], mode="full", scope_confirmed=True, complete_read=True,
                      expected_sheets=["Prices"], covered_sheets=["Prices"])
    payload.update(changes)
    before = store.catalog_snapshot()
    review = store.preview_reconciliation(payload)
    assert not review["ready"] and review["groups"]["delete"] == []
    assert store.catalog_snapshot() == before


def test_incremental_deletes_only_explicit_in_scope_rows(store):
    sid = supplier(store)
    first = apply(store, request(sid, [quote("A"), quote("C")]))
    a, c = first["items"]
    result = apply(store, request(sid, [quote("A", "2")]))
    assert result["changes"]["deleted"] == 0 and store.get_quote(c["id"]).revision == 1
    result = apply(store, request(sid, [], explicit_delete_ids=[{"id": c["id"], "expected_revision": 1}]))
    assert result["deleted_ids"] == [c["id"]]
    manual = store.create_many([quote("M", supplier_id=sid)])[0][0]
    with pytest.raises(ValueError, match="outside"):
        store.preview_reconciliation(request(sid, [], explicit_delete_ids=[{"id": manual.id, "expected_revision": 1}]))


def test_semantic_change_and_duplicate_old_candidates_need_explicit_mapping(store):
    sid = supplier(store)
    first = apply(store, request(sid, [quote("A", source_entry_id="product-a",
        pricing_conditions={"market": "domestic"})]))["items"][0]
    newer = request(sid, [quote("A", "2", source_entry_id="product-a",
        pricing_conditions={"market": "international"})])
    review = store.preview_reconciliation(newer)
    assert not review["ready"] and review["groups"]["needs_confirmation"][0]["candidate_ids"] == [first["id"]]
    newer["decisions"] = [{"entry_key": "entry:product-a", "action": "update",
                           "quote_id": first["id"], "expected_revision": 1}]
    assert apply(store, newer)["items"][0]["id"] == first["id"]
    # Verified source mapping is reusable after the human resolves the identity change.
    newer["decisions"] = []
    newer["entries"][0]["amount"] = "3"
    assert apply(store, newer)["items"][0]["revision"] == 3
    store.create_many([quote("A", "4", supplier_id=sid, source_scope="monthly-prices",
        source_entry_id="product-a", pricing_conditions={"market": "international"})])
    assert not store.preview_reconciliation(newer)["ready"]


def test_ignore_changed_conditions_in_full_preserves_the_old_candidate(store):
    sid = supplier(store)
    old = apply(store, request(sid, [quote("A", pricing_conditions={"market": "domestic"})]))["items"][0]
    entry = quote("A", "2", pricing_conditions={"market": "international"})
    payload = request(sid, [entry], mode="full", scope_confirmed=True, complete_read=True,
                      expected_sheets=["Prices"], covered_sheets=["Prices"],
                      decisions=[{"entry_key": entry_key(entry), "action": "ignore"}])
    review = store.preview_reconciliation(payload)
    assert review["ready"] and not review["groups"]["delete"] and not review["groups"]["update"]
    apply(store, payload)
    assert store.get_quote(old["id"]).amount == Decimal("1")


def test_same_content_replay_is_idempotent_but_semantic_correction_can_be_reviewed(store):
    sid = supplier(store)
    payload = request(sid, [quote()])
    review = store.preview_reconciliation(payload)
    first = store.apply_reconciliation(payload, import_id="one",
        expected_catalog_token=review["base_catalog_token"], expected_review_digest=review["review_digest"])
    replay = store.apply_reconciliation(payload, import_id="one", expected_catalog_token="obsolete",
                                         expected_review_digest="obsolete")
    assert replay["reused"] and replay["items"] == first["items"]
    assert len(store.catalog_snapshot()["supplier_update_receipts"]) == 1
    correction = deepcopy(payload)
    correction["entries"][0]["amount"] = "2"
    review = store.preview_reconciliation(correction)
    assert review["already_applied_file"] and len(review["groups"]["update"]) == 1
    assert apply(store, correction)["items"][0]["revision"] == 2
    with pytest.raises(IdempotencyConflictError):
        store.apply_reconciliation(correction, import_id="one", expected_catalog_token="x", expected_review_digest="y")


def test_old_files_same_version_changed_content_and_unknown_order_require_confirmation(store):
    sid = supplier(store)
    apply(store, request(sid, [quote()], source_version="v2", source_effective_date="2026-10-20"))
    before = store.catalog_snapshot()
    old = request(sid, [quote(amount="3")], file_sha256="b"*64, source_version="v1",
                  source_effective_date="2026-09-20")
    assert not store.preview_reconciliation(old)["ready"]
    changed = request(sid, [quote(amount="3")], file_sha256="c"*64, source_version="v2")
    assert not store.preview_reconciliation(changed)["ready"]
    unknown = request(sid, [quote(amount="3")], latest_confirmed=False)
    assert not store.preview_reconciliation(unknown)["ready"]
    assert store.catalog_snapshot() == before
    old["allow_rollback"] = True
    assert apply(store, old)["items"][0]["amount"] == "3"


def test_catalog_drift_and_tampered_review_digest_cannot_refresh_revision_and_apply(store):
    sid = supplier(store)
    original = apply(store, request(sid, [quote()]))["items"][0]
    payload = request(sid, [quote(amount="2")])
    review = store.preview_reconciliation(payload)
    store.update_quote(original["id"], {"amount": "5"}, expected_revision=1)
    current = store.catalog_snapshot()
    with pytest.raises(RevisionConflictError, match="catalog changed"):
        store.apply_reconciliation(payload, expected_catalog_token=review["base_catalog_token"],
                                    expected_review_digest=review["review_digest"])
    # Merely refreshing the token and revision cannot re-use an earlier review.
    with pytest.raises(RevisionConflictError, match="changed after review"):
        store.apply_reconciliation(payload, expected_catalog_token=store.catalog_token(),
                                    expected_review_digest=review["review_digest"])
    assert store.catalog_snapshot() == current


def test_stale_explicit_mapping_and_delete_become_pending_without_revision_upgrade(store):
    sid = supplier(store)
    original = apply(store, request(sid, [quote(source_entry_id="p")]))["items"][0]
    store.update_quote(original["id"], {"amount": "5"}, expected_revision=1)
    payload = request(sid, [quote(amount="2", source_entry_id="p")], decisions=[
        {"entry_key": "entry:p", "action": "update", "quote_id": original["id"], "expected_revision": 1}])
    review = store.preview_reconciliation(payload)
    assert not review["ready"] and "revision" in review["groups"]["needs_confirmation"][0]["reason"]
    payload = request(sid, [], explicit_delete_ids=[{"id": original["id"], "expected_revision": 1}])
    review = store.preview_reconciliation(payload)
    assert not review["ready"] and review["groups"]["delete"] == []


def test_injected_deletion_failure_rolls_back_quotes_models_receipts_and_mappings(store, monkeypatch):
    sid = supplier(store)
    apply(store, request(sid, [quote("A"), quote("C")]))
    payload = request(sid, [quote("A", "2"), quote("B")], mode="full", scope_confirmed=True,
                      complete_read=True, expected_sheets=["Prices"], covered_sheets=["Prices"])
    review = store.preview_reconciliation(payload)
    before = store.catalog_snapshot()
    original_audit = store._audit
    def fail(kind, key, action, actor, old, new):
        if kind == "quote" and action == "deleted":
            raise sqlite3.OperationalError("injected reconciliation deletion failure")
        return original_audit(kind, key, action, actor, old, new)
    monkeypatch.setattr(store, "_audit", fail)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        store.apply_reconciliation(payload, import_id="failed",
            expected_catalog_token=review["base_catalog_token"], expected_review_digest=review["review_digest"])
    assert store.catalog_snapshot() == before
    assert store._conn.execute("SELECT count(*) FROM pricing_import WHERE import_id='failed'").fetchone()[0] == 0


def test_two_connections_have_one_winner_and_second_requires_new_review(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    first, second = WorkspaceStore(path), WorkspaceStore(path)
    try:
        sid = supplier(first)
        apply(first, request(sid, [quote()]))
        payload = request(sid, [quote(amount="2")])
        review_a, review_b = first.preview_reconciliation(payload), second.preview_reconciliation(payload)
        first.apply_reconciliation(payload, expected_catalog_token=review_a["base_catalog_token"],
                                   expected_review_digest=review_a["review_digest"])
        before = first.catalog_snapshot()
        with pytest.raises(RevisionConflictError):
            second.apply_reconciliation(payload, expected_catalog_token=review_b["base_catalog_token"],
                                        expected_review_digest=review_b["review_digest"])
        assert first.catalog_snapshot() == before
    finally:
        first.close()
        second.close()


def test_amount_discount_conflict_preview_requires_basis_but_rounding_is_allowed(store):
    store.apply_bundle({"official_prices": [{"model_id": "A", "billing_metric": "input",
        "amount": "10", "pricing_unit_id": "cny_mtoken"}]})
    sid = supplier(store)
    rounded = quote(amount="8.04", discount_rate="0.8", supplier_id=sid)
    store.create_many([rounded])
    contradictory = quote(amount="9.00", discount_rate="0.8", supplier_id=sid)
    preview = store.preview_bundle({"create": [contradictory]})
    assert any(value["code"] == "pricing_basis_required" for value in preview["issues"])
    before = store.catalog_snapshot()
    with pytest.raises(ValueError, match="pricing basis"):
        store.create_many([contradictory])
    assert store.catalog_snapshot() == before
    contradictory["pricing_basis"] = "amount"
    row = store.create_many([contradictory])[0][0]
    assert row.amount == Decimal("9") and row.discount_rate == Decimal("0.8")


def test_amount_item_exception_overrides_model_discount_without_repeated_raw_rows(store):
    store.apply_bundle({"official_prices": [
        {"model_id": "A", "billing_metric": "input", "amount": "10", "pricing_unit_id": "cny_mtoken"},
        {"model_id": "A", "billing_metric": "output", "amount": "20", "pricing_unit_id": "cny_mtoken"}]})
    sid = supplier(store)
    rows = store.create_many([
        {"model_id": "A", "supplier_id": sid, "discount_rate": "0.7", "discount_scope": "model"},
        quote(amount="3", supplier_id=sid, discount_scope="items",
              discount_items=[{"billing_metric": "input"}])])[0]
    rules = store.get_quote(rows[0].id).effective_discount_rules
    input_id = next(value["official_price_id"] for value in store.catalog_snapshot()["official_prices"]
                    if value["billing_metric"] == "input")
    rule = next(value for value in rules if value["official_price_id"] == input_id)
    assert rule["quote_id"] == rows[1].id and rule["discount_rate"] is None
    assert rule["reason"] == "item_amount_override"
    assert store.list_quotes()[1] == 2


def test_draft_prepare_retains_raw_entries_and_uses_host_scope_for_review(store):
    sid = supplier(store)
    snapshot = store.catalog_snapshot()
    snapshot["supplier_update_request"] = request(sid, [], source_scope="host-source")
    raw = {"bundle": {"create": [quote()]}, "reconciliation_request": {
        "source_scope": "malicious-expanded-scope", "mode": "full", "complete_read": True}}
    result = prepare(raw, snapshot)
    assert result["bundle"]["create"][0]["supplier_id"] is None
    assert result["bundle"]["update"] == result["bundle"]["delete"] == []
    assert result["reconciliation_request"]["source_scope"] == "host-source"
    assert result["reconciliation_request"]["mode"] == "incremental"
    assert store.list_quotes()[1] == 0
    incomplete = prepare({"create": [{"model_id": "draft"}]}, store.catalog_snapshot())
    assert incomplete["preview"]["requires_supplier"]


def test_supplier_api_and_reconciliation_cli_round_trip(store, tmp_path, capsys):
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        response = client.post("/api/workspace/suppliers", json={"name": "API Supplier"})
        assert response.status_code == 200
        sid = response.json()["supplier_id"]
        assert client.get("/api/workspace/suppliers").json()["items"][0]["supplier_id"] == sid
        payload = request(sid, [quote()])
        review = client.post("/api/workspace/supplier-updates/preview", json=payload).json()
        result = client.post("/api/workspace/supplier-updates/apply", json={
            "request": payload, "expected_catalog_token": review["base_catalog_token"],
            "expected_review_digest": review["review_digest"], "import_id": "api-update"})
        assert result.status_code == 200 and result.json()["changes"]["added"] == 1
    path = tmp_path / "cli.sqlite"
    isolated = WorkspaceStore(path)
    sid = supplier(isolated)
    isolated.close()
    source = tmp_path / "update.json"
    source.write_text(json.dumps(request(sid, [quote()])))
    assert cli_main(["--db", str(path), "reconcile-preview", str(source)]) == 0
    review = json.loads(capsys.readouterr().out)
    assert cli_main(["--db", str(path), "reconcile-apply", str(source), "--catalog-token",
                     review["base_catalog_token"], "--review-digest", review["review_digest"],
                     "--import-id", "cli-update"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["changes"]["added"] == 1


def test_missing_channel_does_not_create_a_new_generic_channel_implicitly(store):
    sid = supplier(store)
    old = apply(store, request(sid, [quote(supplier_model_id="premium")]))["items"][0]
    payload = request(sid, [quote(amount="2")])
    review = store.preview_reconciliation(payload)
    assert not review["ready"]
    assert review["groups"]["needs_confirmation"][0]["candidate_ids"] == [old["id"]]
    assert review["groups"]["create"] == []


def test_explicit_channel_boundary_never_deletes_the_other_channel(store):
    sid = supplier(store)
    first = apply(store, request(sid, [
        quote(supplier_model_id="standard", source_entry_id="standard"),
        quote(supplier_model_id="premium", source_entry_id="premium")]))
    premium = next(value for value in first["items"] if value["supplier_model_id"] == "premium")
    payload = request(sid, [quote(amount="2", supplier_model_id="standard", source_entry_id="standard")],
        supplier_model_ids=["standard"], mode="full", scope_confirmed=True, complete_read=True,
        expected_sheets=["Prices"], covered_sheets=["Prices"])
    result = apply(store, payload)
    assert result["changes"]["updated"] == 1 and result["deleted_ids"] == []
    assert store.get_quote(premium["id"]).revision == 1


def test_selecting_a_confirmed_alias_does_not_rename_the_supplier(store):
    original = store.confirm_supplier({"name": "Canonical", "aliases": ["Known Alias"]})
    selected = store.confirm_supplier({"name": "Known Alias"})
    assert selected == original
    assert store.list_suppliers()[0]["name"] == "Canonical"


def test_unknown_official_context_preserves_dual_source_values_without_guessing_conflict(store):
    sid = supplier(store)
    rows = store.create_many([
        quote(amount="9", discount_rate="0.2", supplier_id=sid),
        {"model_id": "unknown-unit", "amount": "9", "discount_rate": "0.2", "supplier_id": sid}])[0]
    assert all(value.amount == 9 and value.discount_rate == Decimal("0.2") for value in rows)
    assert all(not value.issues for value in rows)


def test_reconciliation_cannot_repurpose_an_existing_generic_import_receipt(store):
    sid = supplier(store)
    apply(store, request(sid, [quote()]))
    store.apply_bundle({}, import_id="generic-operation")
    payload = request(sid, [quote()])
    review = store.preview_reconciliation(payload)
    before = store.catalog_snapshot()
    historical = store._conn.execute("SELECT result FROM pricing_import WHERE import_id='generic-operation'").fetchone()[0]
    with pytest.raises(IdempotencyConflictError, match="another operation"):
        store.apply_reconciliation(payload, import_id="generic-operation",
            expected_catalog_token=review["base_catalog_token"], expected_review_digest=review["review_digest"])
    assert store.catalog_snapshot() == before
    assert store._conn.execute("SELECT result FROM pricing_import WHERE import_id='generic-operation'").fetchone()[0] == historical


@pytest.mark.parametrize("change", [
    {"model_name": "Rewritten Name"}, {"vendor": "Other Vendor"}, {"aliases": ["new-alias"]}])
def test_supplier_update_cannot_modify_existing_model_metadata(store, change):
    store.apply_bundle({"models": [{"model_id": "A", "model_name": "Canonical Name",
        "vendor": "Canonical Vendor", "aliases": ["known-alias"]}]})
    sid = supplier(store)
    apply(store, request(sid, [quote()]))
    payload = request(sid, [quote(amount="2"), quote("B")],
        models=[{"model_id": "B"}, {"model_id": "A", "expected_revision": 1, **change}])
    before = store.catalog_snapshot()
    audit_count = store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0]
    with pytest.raises(ValueError, match="cannot modify existing model metadata"):
        store.preview_reconciliation(payload)
    with pytest.raises(ValueError, match="cannot modify existing model metadata"):
        store.apply_reconciliation(payload, import_id="forbidden-model-edit",
            expected_catalog_token=before["catalog_token"], expected_review_digest="untrusted")
    assert store.catalog_snapshot() == before
    assert store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] == audit_count
    assert store._conn.execute(
        "SELECT count(*) FROM pricing_import WHERE import_id='forbidden-model-edit'").fetchone()[0] == 0


@pytest.mark.parametrize("existing_declaration", [
    {"model_id": "A"},
    {"model_id": "A", "model_name": "Canonical Name", "vendor": "Canonical Vendor",
     "aliases": ["known-alias"], "expected_revision": 1}])
def test_supplier_update_allows_existing_model_stubs_and_creates_missing_models(store, existing_declaration):
    store.apply_bundle({"models": [{"model_id": "A", "model_name": "Canonical Name",
        "vendor": "Canonical Vendor", "aliases": ["known-alias"]}]})
    sid = supplier(store)
    existing = store.catalog_snapshot()["models"][0]
    new_model = {"model_id": "B", "model_name": "New Name", "vendor": "New Vendor",
                 "aliases": ["new-model-alias"]}
    payload = request(sid, [quote(), quote("B"), quote("C")],
                      models=[existing_declaration, new_model])
    review = store.preview_reconciliation(payload)
    assert review["ready"] and [value["model_id"] for value in review["bundle"]["models"]] == ["B"]
    assert store.catalog_snapshot()["models"] == [existing]
    result = apply(store, payload)
    models = {value["model_id"]: value for value in result["catalog"]["models"]}
    assert models["A"] == existing
    assert models["B"]["model_name"] == "New Name" and models["B"]["aliases"] == ["new-model-alias"]
    assert models["C"]["revision"] == 1
    assert result["changes"]["models"] == 2 and result["changes"]["added"] == 3


@pytest.mark.parametrize("explicit_delete", [False, True])
def test_full_price_basis_conflict_suppresses_missing_deletes_and_blocks_atomic_apply(store, explicit_delete):
    store.apply_bundle({"official_prices": [{"model_id": "A", "billing_metric": "input",
        "amount": "10", "pricing_unit_id": "cny_mtoken"}]})
    sid = supplier(store)
    first = apply(store, request(sid, [quote(), quote("C"), quote("D")]))
    ids = {value["model_id"]: value["id"] for value in first["items"]}
    explicit = [{"id": ids["D"], "expected_revision": 1}] if explicit_delete else []
    payload = request(sid, [quote(amount="9.00", discount_rate="0.8"), quote("B")],
        mode="full", scope_confirmed=True, complete_read=True,
        expected_sheets=["Prices"], covered_sheets=["Prices"], explicit_delete_ids=explicit)
    before = store.catalog_snapshot()
    audit_count = store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0]
    review = store.preview_reconciliation(payload)
    assert not review["ready"]
    assert any(value["reason"] == "pricing_basis_required" for value in review["groups"]["needs_confirmation"])
    assert all(value["reason"] == "explicit_deletion" for value in review["groups"]["delete"])
    expected_delete_ids = [ids["D"]] if explicit_delete else []
    assert [value["id"] for value in review["bundle"]["delete"]] == expected_delete_ids
    assert review["preview"]["deleted_ids"] == expected_delete_ids
    assert ids["C"] in [value["id"] for value in review["preview"]["catalog"]["quotes"]]
    assert store.catalog_snapshot() == before
    with pytest.raises(RevisionConflictError, match="changed after review"):
        store.apply_reconciliation(payload, expected_catalog_token=review["base_catalog_token"],
            expected_review_digest=review["review_digest"])
    # The formal write also rejects conflicting source facts if a caller bypasses
    # preview and supplies the deterministic reconciliation digest directly.
    raw_review = build_reconciliation(payload, before)
    with pytest.raises(ValueError, match="pricing basis"):
        store.apply_reconciliation(payload, import_id="conflicting-price-basis",
            expected_catalog_token=before["catalog_token"], expected_review_digest=raw_review["review_digest"])
    assert store.catalog_snapshot() == before
    assert store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] == audit_count
    assert store._conn.execute(
        "SELECT count(*) FROM pricing_import WHERE import_id='conflicting-price-basis'").fetchone()[0] == 0
