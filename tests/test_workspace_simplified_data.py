"""Model-ID-only, safe schema migration, create-only CNY defaults and physical deletion."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sqlite3
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests import test_workspace_nullable_data as legacy
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.schemas import ModelCreate, OfficialPriceCreate, QuoteCreate
from tokensql.workspace.store import QuoteNotFoundError, RevisionConflictError, WorkspaceStore


@pytest.mark.parametrize("schema", [QuoteCreate, OfficialPriceCreate])
@pytest.mark.parametrize("fields,expected", [
    ({}, "CNY"), ({"currency": None}, None), ({"currency": ""}, None),
    ({"currency": "HKD"}, "HKD"), ({"pricing_unit_id": "usd_mtoken"}, "USD"),
    ({"pricing_unit_id": "hkd_second"}, "HKD"),
    ({"unit": "Tokens"}, "CNY"),
])
def test_create_currency_defaults_without_overriding_explicit_unknown_or_unit_id(schema, fields, expected):
    item = schema.model_validate({"model_id": "new", **fields})
    assert item.currency == expected
    if fields.get("unit") == "Tokens":
        assert item.unit == "token" and item.unit_quantity is None and item.pricing_unit_id is None
    assert schema.model_json_schema()["required"] == ["model_id"]


def test_model_id_is_only_identity_and_display_names_are_not_aliases():
    with pytest.raises(ValueError, match="physical_key"):
        ModelCreate.model_validate({"model_id": "x", "physical_key": "removed"})
    store = WorkspaceStore(":memory:")
    try:
        store.apply_bundle({"models": [
            {"model_id": "a", "vendor": "Maker", "model_name": "Same display"},
            {"model_id": "b", "vendor": "Maker", "model_name": "Same display"},
        ]})
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(pricing_model)")}
        assert "physical_key" not in columns
        assert "physical_key" not in ModelCreate.model_json_schema()["properties"]
        assert store._conn.execute("SELECT count(*) FROM pricing_alias").fetchone()[0] == 0
        store.apply_bundle({"create": [{"model_id": "automatic-new-model"}]})
        assert {m["model_id"] for m in store.catalog_snapshot()["models"]} == {"a", "b", "automatic-new-model"}
        assert all("physical_key" not in m for m in store.catalog_snapshot()["models"])
    finally:
        store.close()


def test_nullable_predecessor_migration_preserves_history_and_unrelated_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "OLD_DDL", legacy.OLD_DDL.replace("vendor_id TEXT NOT NULL REFERENCES", "vendor_id TEXT REFERENCES")
                        .replace("physical_key TEXT NOT NULL", "physical_key TEXT")
                        .replace("billing_item TEXT NOT NULL", "billing_item TEXT")
                        .replace("supplier_id TEXT NOT NULL REFERENCES", "supplier_id TEXT REFERENCES")
                        .replace("channel_id INTEGER NOT NULL REFERENCES", "channel_id INTEGER REFERENCES"))
    path = tmp_path / "nullable-predecessor.sqlite"
    legacy.old_database(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE INDEX custom_model_revision ON pricing_model(revision)")
    connection.execute("INSERT INTO pricing_alias VALUES (\'oldmaker\',\'retiredautomaticphysical\',\'old-model\')")
    connection.execute("INSERT INTO pricing_alias VALUES (\'oldmaker\',\'oldmodeldisplayautomatic\',\'old-model\')")
    connection.execute("CREATE INDEX removed_physical_index ON pricing_model(physical_key)")
    connection.execute("CREATE TRIGGER custom_model_trigger AFTER UPDATE ON pricing_model BEGIN SELECT 1; END")
    connection.execute("CREATE TRIGGER removed_physical_trigger AFTER UPDATE ON pricing_model BEGIN SELECT new.physical_key; END")
    old_model = json.loads(connection.execute("SELECT payload FROM pricing_model").fetchone()[0])
    connection.execute("INSERT INTO pricing_audit VALUES (12,'model','old-model','added','historic',NULL,?,'created')", (json.dumps(old_model),))
    connection.execute("UPDATE pricing_quote SET payload=?", (json.dumps({"model_id": "old-model", "discount_rate": "0.123456789", "currency": None}),))
    connection.commit()
    preserved = {name: list(connection.execute(f"SELECT * FROM {name}")) for name in
                 ("pricing_official", "pricing_audit", "pricing_import", "pricing_channel")}
    connection.close()
    store = WorkspaceStore(path)
    try:
        for name, rows in preserved.items():
            query = f"SELECT * FROM {name}" + (" WHERE id<=12" if name == "pricing_audit" else "")
            assert [tuple(row) for row in store._conn.execute(query)] == rows
        assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert store.get_quote(7).currency is None
        assert store.get_quote(7).discount_rate == Decimal("0.12")
        assert store.get_quote(7).revision == 4
        migrated = store._conn.execute("SELECT old_value,new_value FROM pricing_audit WHERE actor=\'migration:discount-2dp\'").fetchone()
        assert json.loads(migrated["old_value"])["discount_rate"] == "0.123456789"
        assert json.loads(migrated["new_value"])["discount_rate"] == "0.12"
        assert "physical_key" not in store.catalog_snapshot()["models"][0]
        assert [tuple(row) for row in store._conn.execute("SELECT * FROM pricing_alias")] == [("oldmaker", "oldalias", "old-model")]
        store.apply_bundle({"models": [{"model_id": "new-with-explicit-alias", "vendor": "Old Maker",
                                       "aliases": ["retired automatic physical"]}]})
        with pytest.raises(ValueError, match="alias is already assigned"):
            store.apply_bundle({"models": [{"model_id": "must-conflict", "vendor": "Old Maker", "aliases": ["old alias"]}]})
        names = {row["name"] for row in store._conn.execute("SELECT name FROM sqlite_master")}
        assert {"custom_model_revision", "custom_model_trigger"} <= names
        assert not {"removed_physical_index", "removed_physical_trigger"} & names
        assert store._conn.execute("SELECT revision FROM pricing_model WHERE model_id=\'old-model\'").fetchone()[0] == 4
        created = store.apply_bundle({"create": [{"model_id": "id-only"}]})["items"][0]
        assert created["id"] == 1000 and created["currency"] == "CNY"
        old = store.update_quote(7, {"note": "metadata only"}, expected_revision=4)
        assert old.currency is None and old.discount_rate == Decimal("0.12")
    finally:
        store.close()


def test_physical_identity_migration_failure_preserves_entire_old_database(tmp_path, monkeypatch):
    path = tmp_path / "rollback-model.sqlite"
    legacy.old_database(path)
    before = legacy.logical_dump(path)
    real_connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.startswith("ALTER TABLE __nullable_pricing_model"):
                raise sqlite3.OperationalError("injected model migration failure")
            return super().execute(statement, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, **kw, factory=Interrupted))
    with pytest.raises(sqlite3.OperationalError, match="injected model"):
        WorkspaceStore(path)
    monkeypatch.undo()
    assert legacy.logical_dump(path) == before


def test_patch_and_reopen_do_not_default_or_rewrite_existing_currency(tmp_path):
    path = tmp_path / "currency.sqlite"
    store = WorkspaceStore(path)
    items = store.apply_bundle({"create": [
        {"model_id": "usd", "pricing_unit_id": "usd_mtoken", "discount_rate": "0.123456789"},
        {"model_id": "unknown", "currency": None}, {"model_id": "default"},
    ]})["items"]
    unknown = items[1]
    raw = json.loads(store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (unknown["id"],)).fetchone()[0])
    raw.pop("currency")
    store._conn.execute("UPDATE pricing_quote SET payload=? WHERE id=?", (json.dumps(raw), unknown["id"]))
    store.close()
    store = WorkspaceStore(path)
    try:
        before = store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (unknown["id"],)).fetchone()[0]
        assert store.get_quote(unknown["id"]).currency is None
        assert store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (unknown["id"],)).fetchone()[0] == before
        changed = store.update_quote(items[0]["id"], {"note": "new note"}, expected_revision=1)
        assert changed.currency == "USD" and changed.discount_rate == Decimal("0.12")
        changed_unknown = store.update_quote(unknown["id"], {"note": "new note"}, expected_revision=1)
        assert changed_unknown.currency is None
        cleared = store.update_quote(items[0]["id"], {"pricing_unit_id": None, "currency": None}, expected_revision=2)
        assert cleared.currency is None and cleared.pricing_unit_id is None
        assert store.get_quote(items[2]["id"]).currency == "CNY"
    finally:
        store.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_alias_cannot_conflict_with_existing_model_id(reverse):
    store = WorkspaceStore(":memory:")
    try:
        owner = {"model_id": "owner", "vendor": "Maker", "aliases": ["target"]}
        target = {"model_id": "target", "vendor": "Maker"}
        first, second = (owner, target) if not reverse else (target, owner)
        store.apply_bundle({"models": [first]})
        before = store.catalog_snapshot()
        with pytest.raises(ValueError, match="split"):
            store.apply_bundle({"models": [second]})
        assert store.catalog_snapshot() == before
    finally:
        store.close()


def test_model_ids_are_not_inferred_equivalent_without_explicit_aliases():
    store = WorkspaceStore(":memory:")
    try:
        store.apply_bundle({"models": [{"model_id": "model-a", "vendor": "Maker"},
                                      {"model_id": "model_a", "vendor": "Maker"}]})
        assert len(store.catalog_snapshot()["models"]) == 2
    finally:
        store.close()


def test_alias_rebuild_failure_rolls_back_schema_payload_and_indexes(tmp_path, monkeypatch):
    path = tmp_path / "alias-rollback.sqlite"
    legacy.old_database(path)
    before = legacy.logical_dump(path)
    real_connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "INSERT INTO pricing_alias VALUES (?,?,?)":
                raise sqlite3.OperationalError("injected alias rebuild failure")
            return super().execute(statement, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, **kw, factory=Interrupted))
    with pytest.raises(sqlite3.OperationalError, match="injected alias"):
        WorkspaceStore(path)
    monkeypatch.undo()
    assert legacy.logical_dump(path) == before


@pytest.fixture
def populated_store(tmp_path):
    store = WorkspaceStore(tmp_path / "delete.sqlite")
    store.apply_bundle({"official_prices": [{"model_id": "a", "amount": "10"}],
                        "create": [{"model_id": "a", "supplier": "Supplier", "amount": "1"},
                                   {"model_id": "b", "discount_rate": "0.15"},
                                   {"model_id": "c", "currency": None}]})
    try:
        yield store
    finally:
        store.close()


def test_bulk_delete_physically_removes_only_selected_rows_and_keeps_audit_and_catalog(populated_store):
    store = populated_store
    before = store.catalog_snapshot()
    result = store.delete_many([{"id": 1, "expected_revision": 1}, {"id": 3, "expected_revision": 1}], actor="bulk-ui")
    assert result == {"deleted": 2, "deleted_ids": [1, 3]}
    assert [row[0] for row in store._conn.execute("SELECT id FROM pricing_quote")] == [2]
    assert store.get_quote(2).discount_rate == Decimal("0.15")
    for name in ("models", "official_prices", "suppliers", "channels"):
        if name != "channels":
            assert store.catalog_snapshot()[name] == before[name]
    assert store._conn.execute("SELECT count(*) FROM pricing_channel").fetchone()[0] == 1
    deleted = store._conn.execute("SELECT old_value,new_value FROM pricing_audit WHERE actor='bulk-ui' ORDER BY id").fetchall()
    assert [json.loads(row["old_value"])["id"] for row in deleted] == [1, 3]
    assert all(row["new_value"] is None for row in deleted)
    with pytest.raises(QuoteNotFoundError):
        store.get_quote(1)
    next_quote = store.apply_bundle({"create": [{"model_id": "d"}]})["items"][0]
    assert next_quote["id"] == 4


@pytest.mark.parametrize("items,status", [
    ([], 422), ([{"id": 1, "expected_revision": 1}] * 2, 422),
    ([{"id": 1, "expected_revision": 1}, {"id": 2, "expected_revision": 999}], 409),
    ([{"id": 1, "expected_revision": 1}, {"id": 999, "expected_revision": 1}], 404),
])
def test_bulk_delete_api_rejects_invalid_whole_batch_without_side_effects(populated_store, items, status):
    store = populated_store
    app = FastAPI()
    app.include_router(create_data_router(store))
    before = store.catalog_snapshot()
    audit_count = store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0]
    with TestClient(app) as client:
        response = client.post("/api/workspace/quotes/bulk-delete", json={"items": items})
        assert response.status_code == status, response.text
    assert store.catalog_snapshot() == before
    assert store._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] == audit_count


def test_bulk_and_single_delete_api_and_bundle_share_physical_delete(populated_store):
    store = populated_store
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        response = client.post("/api/workspace/quotes/bulk-delete", json={"items": [{"id": 1, "expected_revision": 1}]})
        assert response.status_code == 200 and response.json() == {"deleted": 1, "deleted_ids": [1]}
        assert client.get("/api/workspace/quotes/1").status_code == 404
        response = client.delete("/api/workspace/quotes/2?expected_revision=1")
        assert response.status_code == 200 and response.json()["id"] == 2
        assert client.get("/api/workspace/quotes/2").status_code == 404
    bundle = {"delete": [{"id": 3, "expected_revision": 1}]}
    preview = store.preview_bundle(bundle)
    assert preview["changes"]["deleted"] == 1 and preview["items"] == [] and preview["deleted_ids"] == [3]
    assert store.get_quote(3).id == 3
    result = store.apply_bundle(bundle, import_id="hard-delete-once")
    assert result["items"] == [] and result["deleted_ids"] == [3]
    assert store.apply_bundle(bundle, import_id="hard-delete-once")["reused"] is True
    assert store._conn.execute("SELECT count(*) FROM pricing_quote").fetchone()[0] == 0


def test_audit_failure_rolls_back_entire_physical_deletion(populated_store, monkeypatch):
    store = populated_store
    before = "\n".join(store._conn.iterdump())
    audit = store._audit
    def fail_second(kind, key, action, *args):
        if kind == "quote" and action == "deleted" and key == 2:
            raise RuntimeError("injected audit failure")
        return audit(kind, key, action, *args)
    monkeypatch.setattr(store, "_audit", fail_second)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        store.delete_many([{"id": 1, "expected_revision": 1}, {"id": 2, "expected_revision": 1}])
    assert "\n".join(store._conn.iterdump()) == before


def test_overlapping_concurrent_batches_have_one_winner_and_rollback_loser(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    first, second = WorkspaceStore(path), WorkspaceStore(path)
    try:
        first.apply_bundle({"create": [{"model_id": "a"}, {"model_id": "b"}, {"model_id": "shared"}]})
        barrier = Barrier(2)
        def delete(store, own):
            barrier.wait(5)
            try:
                return store.delete_many([{"id": own, "expected_revision": 1}, {"id": 3, "expected_revision": 1}])
            except (QuoteNotFoundError, RevisionConflictError) as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(delete, first, 1), pool.submit(delete, second, 2)]
            results = [future.result(timeout=15) for future in futures]
        assert sum(isinstance(result, dict) for result in results) == 1
        remaining = [row[0] for row in first._conn.execute("SELECT id FROM pricing_quote")]
        assert remaining in ([1], [2])
        assert first._conn.execute("SELECT count(*) FROM pricing_audit WHERE action='deleted'").fetchone()[0] == 2
        assert first._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        first.close()
        second.close()
