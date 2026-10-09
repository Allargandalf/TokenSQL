from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.cli import main as cli_main
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.schemas import QuoteCreate
from tokensql.workspace.store import (
    IdempotencyConflictError,
    QuoteNotFoundError,
    RevisionConflictError,
    WorkspaceStore,
)


def quote(**overrides) -> QuoteCreate:
    payload = {
        "model_name": "Model A",
        "manufacturer": "Maker",
        "provider": "Provider",
        "provider_type": "reseller",
        "amount": "2.50",
        "currency": "cny",
        "unit": "token",
        "unit_quantity": "1000000",
        "metric": "input_token",
        "dimension": "token",
        "direction": "input",
        "source": "prices.xlsx",
        "source_ref": "Sheet1!C2",
    }
    payload.update(overrides)
    return QuoteCreate.model_validate(payload)


def test_store_idempotency_edit_archive_and_audit(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    try:
        items, reused = store.create_many(
            [quote()], actor="opencode:run-1", idempotency_key="request-1"
        )
        assert not reused
        assert items[0].currency == "CNY"
        replay, reused = store.create_many(
            [quote()], actor="opencode:run-1", idempotency_key="request-1"
        )
        assert reused
        assert replay[0].id == items[0].id
        with pytest.raises(IdempotencyConflictError):
            store.create_many(
                [quote(amount="3")],
                actor="opencode:run-1",
                idempotency_key="request-1",
            )
        updated = store.update_quote(
            items[0].id, {"amount": "2.75"},
            expected_revision=1, actor="opencode:run-1",
        )
        assert str(updated.amount) == "2.75"
        assert updated.revision == 2
        with pytest.raises(RevisionConflictError):
            store.update_quote(updated.id, {"amount": "3"}, expected_revision=1)
        archived = store.archive_quote(
            updated.id, expected_revision=2, actor="opencode:run-1"
        )
        assert archived.status == "archived"
        assert store.change_summary("opencode:run-1") == {
            "added": 1, "updated": 1, "deleted": 1, "total": 1
        }
        assert store.list_quotes()[1] == 0
    finally:
        store.close()


def test_mixed_apply_is_atomic_and_filters_are_available(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    try:
        existing, _ = store.create_many([quote()], actor="seed")
        with pytest.raises(QuoteNotFoundError):
            store.apply_changes(
                creates=[quote(model_name="Must Roll Back")],
                updates=[(999, 1, {"amount": "5"})],
                actor="opencode:failed",
            )
        rows, total = store.list_quotes()
        assert total == 1
        assert [item.model_name for item in rows] == ["Model A"]
        assert store.change_summary("opencode:failed")["total"] == 0

        changed = store.apply_changes(
            creates=[
                quote(
                    model_name="Model B", provider="Other", amount="4.25",
                    dimension="time", metric="second",
                )
            ],
            updates=[(existing[0].id, 1, {"amount": "3.00"})],
            actor="opencode:ok",
        )
        assert len(changed) == 2
        filtered, total = store.list_quotes(dimension="time")
        assert total == 1
        assert filtered[0].model_name == "Model B"
        assert store.filter_options()["dimensions"] == ["time", "token"]
    finally:
        store.close()


def test_data_router_crud_filters_and_exports(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    api = FastAPI()
    api.include_router(create_data_router(store))
    try:
        with TestClient(api) as client:
            created = client.post(
                "/api/workspace/quotes",
                json={"items": [quote().model_dump(mode="json")]},
            )
            assert created.status_code == 201
            assert store.change_summary("workspace-ui")["added"] == 1
            item = created.json()["items"][0]

            listed = client.get(
                "/api/workspace/quotes",
                params={"model_name": "Model A", "dimension": "token"},
            )
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
            assert listed.json()["filter_options"]["providers"] == ["Provider"]

            patched = client.patch(
                f"/api/workspace/quotes/{item['id']}",
                json={
                    "expected_revision": item["revision"],
                    "actor": "ui",
                    "amount": "2.75",
                },
            )
            assert patched.status_code == 200
            assert patched.json()["amount"] == "2.75"
            stale = client.patch(
                f"/api/workspace/quotes/{item['id']}",
                json={"expected_revision": 1, "amount": "3"},
            )
            assert stale.status_code == 409

            csv_export = client.get(
                "/api/workspace/exports",
                params={"format": "csv", "model_name": "Model A"},
            )
            assert csv_export.status_code == 200
            assert b"Model A" in csv_export.content
            xlsx_export = client.get(
                "/api/workspace/exports",
                params={"format": "xlsx", "ids": str(item["id"])},
            )
            assert xlsx_export.status_code == 200
            assert xlsx_export.content.startswith(b"PK")
    finally:
        store.close()


def test_cli_schema_and_atomic_apply(tmp_path, capsys):
    database = tmp_path / "workspace.sqlite3"
    input_path = tmp_path / "apply.json"
    input_path.write_text(
        json.dumps(
            {
                "create": [
                    quote(model_name="CLI Model").model_dump(mode="json")
                ],
                "update": [],
                "delete": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli_main([
        "--db", str(database), "schema"
    ]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "quote" in schema
    assert set(schema["apply"]) == {"create", "update", "delete"}

    assert cli_main([
        "--db", str(database), "apply", str(input_path),
        "--actor", "opencode:cli-run",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["changes"] == {
        "added": 1, "updated": 0, "deleted": 0, "total": 1
    }
    assert result["items"][0]["model_name"] == "CLI Model"
