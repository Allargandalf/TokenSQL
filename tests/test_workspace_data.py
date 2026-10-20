from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.cli import main as cli_main
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.pricing import convert_amount
from tokensql.workspace.schemas import PricingBundle, QuoteCreate
from tokensql.workspace.store import (
    IdempotencyConflictError, QuoteNotFoundError, RevisionConflictError,
    WorkspaceStore, prepare_bundle,
)


def model(**changes):
    value = {"model_id": "maker/model-a", "vendor": "Maker", "model_name": "Model A",
             "aliases": ["Model-A-2026"]}
    value.update(changes)
    return value


def official(**changes):
    value = {"model_id": "maker/model-a", "billing_item": "input", "amount": "10",
             "currency": "CNY", "unit": "token", "unit_quantity": "1000000", "explicit_official": True,
             "source": "explicit-official.xlsx", "source_ref": "Sheet1!D2"}
    value.update(changes)
    if value.get("pricing_unit_id") is not None and "currency" not in changes:
        value.pop("currency", None)
    return value


def quote(**changes):
    value = {"model_id": "maker/model-a", "billing_item": "input", "supplier": "Supplier",
             "supplier_model_id": "raw-channel-999", "tags": ["国内", "官转"],
             "amount": "0.008", "currency": "CNY", "unit": "token", "unit_quantity": "1000",
             "source": "prices.xlsx", "source_ref": "Sheet1!C2"}
    value.update(changes)
    if value.get("pricing_unit_id") is not None and "currency" not in changes:
        value.pop("currency", None)
    return value


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "workspace.sqlite3")
    value.apply_bundle({"models": [model()], "official_prices": [official()]}, actor="catalog")
    try:
        yield value
    finally:
        value.close()


def test_preview_never_changes_database_or_audits(store):
    before = store.catalog_snapshot()
    raw = PathLikeHash(store.path)
    audits = store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0]
    result = store.preview_bundle({"create": [quote()]})
    assert result["items"][0]["discount_rate"] is None
    assert store.catalog_snapshot() == before
    assert PathLikeHash(store.path) == raw
    assert store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] == audits


def PathLikeHash(path):
    from pathlib import Path
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_model_id_identity_and_only_explicit_aliases_prevent_splits(store):
    with pytest.raises(ValueError, match="split"):
        store.apply_bundle({"models": [model(model_id="maker/split-output", aliases=["Model-A-2026"])]})
    # Display names may be identical; they are not a second identity field.
    store.apply_bundle({"models": [model(model_id="maker/another", aliases=[])]})
    assert len(store.catalog_snapshot()["models"]) == 2
    assert all("physical_key" not in item for item in store.catalog_snapshot()["models"])


def test_billing_items_and_tags_stay_under_one_physical_model(store):
    result = store.apply_bundle({
        "official_prices": [official(billing_item="output:context<=200k:cn", amount="30")],
        "create": [
            quote(),
            quote(billing_item="output:context<=200k:cn", amount="0.024", tags=["国外"]),
            quote(supplier="Other supplier", supplier_model_id="raw-channel-999", tags=["便宜"]),
        ],
    })
    assert len(result["catalog"]["models"]) == 1
    assert len(result["catalog"]["official_prices"]) == 2
    assert len(result["catalog"]["suppliers"]) == 2
    assert len(result["catalog"]["channels"]) == 2
    assert result["items"][0]["supplier_id"] != result["items"][2]["supplier_id"]
    assert result["items"][0]["model_id"] != result["items"][0]["supplier_model_id"]
    selected, total = store.list_quotes(tag="国外")
    assert total == 1 and selected[0].billing_item == "output:context<=200k:cn"


def test_source_amount_and_discount_are_independent_and_never_derived(store):
    rows = store.apply_bundle({"create": [
        quote(), quote(amount=None, discount_rate="1.2"),
        quote(amount=None, discount_rate="0.77777", unit_quantity="1000000"),
        quote(discount_rate="0.7"),
    ]})["items"]
    assert rows[0]["discount_rate"] is None
    assert rows[1]["amount"] is None and rows[1]["discount_rate"] == "1.20"
    assert rows[2]["amount"] is None and rows[2]["discount_rate"] == "0.78"
    assert rows[3]["amount"] == "0.008" and rows[3]["discount_rate"] == "0.70"
    assert all(row["derived_amount"] is None and row["normalized"] == {} for row in rows)
    unconverted = store.apply_bundle({"create": [quote(unit="second", unit_quantity="1")]})["items"][0]
    assert unconverted["conversion_status"] == "unavailable"
    assert unconverted["normalized"] == {}


def test_zero_official_keeps_raw_facts_without_backend_division(store):
    store.apply_bundle({"official_prices": [official(billing_item="cache", amount="0")]})
    values = store.apply_bundle({"create": [
        quote(billing_item="cache", amount="0"),
        quote(billing_item="cache", amount="4"),
        quote(billing_item="cache", amount=None, discount_rate="0.8"),
    ]})["items"]
    assert [row["amount"] for row in values] == ["0", "4", None]
    assert [row["discount_rate"] for row in values] == [None, None, "0.80"]
    assert all(row["derived_amount"] is None for row in values)
    assert all(row["official_price"]["amount"] == "0" for row in values)


def test_missing_official_amount_allowed_but_rate_not_guessed(store):
    result = store.apply_bundle({"create": [quote(billing_item="other") ]})
    assert result["items"][0]["discount_rate"] is None
    assert result["items"][0]["official_price"] is None and result["issues"] == []
    unknown = store.apply_bundle({"create": [quote(billing_item="other", amount=None, discount_rate="0.8")]})["items"][0]
    assert unknown["amount"] is None and unknown["derived_amount"] is None
    assert unknown["raw_discount_rate"] == "0.80" and unknown["discount_status"] == "unavailable"


def test_explicit_fx_decimal_provenance_and_no_float_guessing(store):
    conversion = {"original_currency": "USD", "original_amount": "0.001",
                  "exchange_rate": "7.1234", "rate_source": "user supplied 2026-10-16"}
    amount = convert_amount(conversion["original_amount"], conversion["exchange_rate"])
    assert amount == Decimal("0.0071234")
    item = store.apply_bundle({"create": [quote(amount=str(amount), conversion=conversion)]})["items"][0]
    assert item["conversion"] == conversion
    independent = store.apply_bundle({"create": [quote(amount="1", conversion=conversion)]})["items"][0]
    assert independent["amount"] == "1" and independent["conversion"] == conversion
    with pytest.raises(ValueError):
        QuoteCreate.model_validate(quote(amount=0.8))
    with pytest.raises(ValueError):
        QuoteCreate.model_validate(quote(currency="EUR"))
    with pytest.raises(ValueError):
        PricingBundle.model_validate({"official_prices": [official(explicit_official=False)]})
    assert PricingBundle.model_validate({"official_prices": [official(source_ref=" ")]}).official_prices[0].source_ref is None


def test_atomic_bundle_idempotency_snapshot_and_optimistic_revisions(store):
    bundle = {"create": [quote()]}
    first = store.apply_bundle(bundle, actor="import:a", import_id="import-a")
    again = store.apply_bundle(bundle, actor="import:a", import_id="import-a")
    assert again["reused"] and again["items"] == first["items"]
    with pytest.raises(IdempotencyConflictError):
        store.apply_bundle({"create": [quote(amount="0.006")]}, import_id="import-a")
    q = first["items"][0]
    updated = store.update_quote(q["id"], {"discount_rate": "1.2"}, expected_revision=1, actor="ui")
    assert updated.amount == Decimal("0.008") and updated.discount_rate == Decimal("1.2") and updated.derived_amount is None
    assert updated.revision == 2
    with pytest.raises(RevisionConflictError):
        store.update_quote(q["id"], {}, expected_revision=1)
    bad = {"models": [model(model_id="maker/new", model_name="New", aliases=[])],
           "create": [quote()], "update": [{"id": 99999, "expected_revision": 1, "changes": {"amount": "2"}}]}
    before = store.catalog_snapshot()
    with pytest.raises(QuoteNotFoundError):
        store.apply_bundle(bad, actor="failed")
    assert store.catalog_snapshot() == before
    assert store.change_summary("failed")["total"] == 0
    prepared = prepare_bundle({"update": [{"id": q["id"], "expected_revision": 2,
                                          "changes": {"amount": "0.009"}}]}, before)
    assert prepared["items"][0]["revision"] == 3
    assert store.get_quote(q["id"]).revision == 2
    deleted = store.delete_quote(q["id"], expected_revision=2, actor="ui")
    assert deleted.id == q["id"] and deleted.revision == 2
    assert store.list_quotes()[1] == 0
    with pytest.raises(QuoteNotFoundError):
        store.get_quote(q["id"])


def test_official_edit_recalculates_supplied_basis_and_audits(store):
    created = store.apply_bundle({"create": [quote(), quote(amount=None, discount_rate="0.8")]})["items"]
    changed = store.apply_bundle({"official_prices": [official(amount="20", expected_revision=1)]},
                                 actor="official:edit")
    first, second = [store.get_quote(q["id"]) for q in created]
    assert first.amount == Decimal("0.008") and first.discount_rate is None
    assert second.discount_rate == Decimal("0.8") and second.amount is None and second.derived_amount is None
    assert changed["changes"]["updated"] == 0
    assert first.revision == 1
    with pytest.raises(RevisionConflictError):
        store.apply_bundle({"official_prices": [official(amount="30", expected_revision=1)]})
    # Explicit quote edits in the same bundle use the visible pre-bundle revision.
    both = store.apply_bundle({
        "official_prices": [official(amount="30", expected_revision=2)],
        "update": [{"id": first.id, "expected_revision": 1, "changes": {"discount_rate": "0.6"}}],
    })
    assert store.get_quote(first.id).amount == Decimal("0.008")
    assert store.get_quote(first.id).discount_rate == Decimal("0.6")
    assert store.get_quote(first.id).derived_amount is None


def test_catalog_alias_history_and_vendor_can_be_corrected(store):
    store.apply_bundle({"models": [model(model_name="Renamed", aliases=["New"], expected_revision=1)]})
    with pytest.raises(ValueError, match="split"):
        store.apply_bundle({"models": [model(model_id="new-id", model_name="Other", aliases=["Model-A-2026"])]})
    store.apply_bundle({"models": [model(vendor="Corrected Maker", expected_revision=2)]})
    changed = store.catalog_snapshot()["models"][0]
    assert changed["vendor"] == "Corrected Maker" and changed["revision"] == 3
    assert changed["aliases"] == ["Model-A-2026", "New"]
    assert store._conn.execute("SELECT count(*) FROM pricing_alias WHERE vendor_id='maker'").fetchone()[0] == 0


def test_preserves_legacy_table_instead_of_guessing_entities(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE workspace_quote(id INTEGER PRIMARY KEY, model_name TEXT, currency TEXT)")
    conn.execute("INSERT INTO workspace_quote VALUES (3,'Legacy ambiguous','USD')")
    conn.commit()
    conn.close()
    store = WorkspaceStore(path)
    try:
        assert store.catalog_snapshot()["legacy_quote_count"] == 1
        assert store.list_quotes()[1] == 0
        assert tuple(store._conn.execute("SELECT * FROM workspace_quote").fetchone()) == (
            3, "Legacy ambiguous", "USD")
    finally:
        store.close()


def test_data_router_manual_catalog_crud_preview_filters_and_exports(tmp_path):
    store = WorkspaceStore(tmp_path / "api.sqlite")
    api = FastAPI()
    api.include_router(create_data_router(store))
    try:
        with TestClient(api) as client:
            catalog = {"models": [model()], "official_prices": [official()]}
            response = client.post("/api/workspace/catalog/preview", json=catalog)
            assert response.status_code == 200
            assert client.get("/api/workspace/catalog").json()["models"] == []
            assert client.post("/api/workspace/catalog", json={"bundle": catalog}).status_code == 200
            preview = client.post("/api/workspace/quotes/preview", json={"items": [quote()]})
            assert preview.status_code == 200
            assert preview.json()["items"][0]["discount_rate"] is None
            assert client.get("/api/workspace/quotes").json()["total"] == 0
            created = client.post("/api/workspace/quotes", json={"items": [quote()]})
            assert created.status_code == 201, created.text
            item = created.json()["items"][0]
            assert client.get("/api/workspace/quotes", params={"tag": "官转"}).json()["total"] == 1
            patched = client.patch(f"/api/workspace/quotes/{item['id']}",
                json={"expected_revision": 1, "amount": "0.009"})
            assert patched.status_code == 200 and patched.json()["discount_rate"] is None
            assert client.patch(f"/api/workspace/quotes/{item['id']}",
                json={"expected_revision": 1, "amount": "1"}).status_code == 409
            assert client.post("/api/workspace/quotes", json={"items": [quote(model_id="unknown")]}).status_code == 201
            for fmt in ("csv", "xlsx"):
                export = client.get("/api/workspace/exports", params={"format": fmt, "model_id": "maker/model-a"})
                assert export.status_code == 200
                assert b"model_id" in export.content if fmt == "csv" else export.content.startswith(b"PK")
            assert client.delete(f"/api/workspace/quotes/{item['id']}",
                                 params={"expected_revision": 2}).status_code == 200
    finally:
        store.close()


def test_cli_catalog_loading_preview_and_apply(tmp_path, capsys):
    db = tmp_path / "cli.sqlite"
    payload = tmp_path / "bundle.json"
    payload.write_text(json.dumps({"models": [model()], "official_prices": [official()]}), encoding="utf-8")
    assert cli_main(["--db", str(db), "schema"]) == 0
    assert "bundle" in json.loads(capsys.readouterr().out)
    assert cli_main(["--db", str(db), "catalog-load", str(payload), "--import-id", "seed"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"]["models"] == 1
    payload.write_text(json.dumps({"create": [quote()]}), encoding="utf-8")
    assert cli_main(["--db", str(db), "preview", str(payload)]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1
    assert cli_main(["--db", str(db), "list"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0
    assert cli_main(["--db", str(db), "apply", str(payload), "--import-id", "one"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"]["added"] == 1


def test_review_token_rejects_stale_price_and_keeps_idempotent_receipt(store):
    proposal = {"create": [quote(amount=None, discount_rate="0.8")]}
    reviewed = store.preview_bundle(proposal)
    token = reviewed["base_catalog_token"]
    assert reviewed["items"][0]["derived_amount"] is None
    assert reviewed["items"][0]["official_price"]["amount"] == "10"
    # A separate connection represents concurrent UI/CLI writes, not the review worker.
    other = WorkspaceStore(store.path)
    try:
        other.apply_bundle({"official_prices": [official(amount="20", expected_revision=1)]})
    finally:
        other.close()
    with pytest.raises(RevisionConflictError, match="catalog changed after review"):
        store.apply_bundle(proposal, import_id="review-token", expected_catalog_token=token)
    assert store.list_quotes()[1] == 0
    fresh = store.preview_bundle(proposal)
    assert fresh["base_catalog_token"] != token
    assert fresh["items"][0]["derived_amount"] is None
    assert fresh["items"][0]["official_price"]["amount"] == "20"
    applied = store.apply_bundle(proposal, import_id="review-token",
                                 expected_catalog_token=fresh["base_catalog_token"])
    assert applied["changes"]["added"] == 1
    # The saved receipt wins over a now-stale precondition on a retry.
    replay = store.apply_bundle(proposal, import_id="review-token",
                                expected_catalog_token=fresh["base_catalog_token"])
    assert replay["reused"] is True
    assert replay["items"] == applied["items"]


def test_concurrent_writers_cannot_both_apply_one_reviewed_catalog(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    token = store.preview_bundle({"create": [quote()]})["base_catalog_token"]
    barrier = Barrier(2)

    def write(index):
        connection = WorkspaceStore(store.path)
        try:
            barrier.wait(timeout=5)
            try:
                value = connection.apply_bundle({"create": [quote(supplier=f"Concurrent {index}")]},
                    import_id=f"concurrent-{index}", expected_catalog_token=token)
                return "applied", value
            except RevisionConflictError:
                return "conflict", None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(write, (1, 2)))
    assert sorted(value[0] for value in result) == ["applied", "conflict"]
    assert store.list_quotes()[1] == 1
