"""Independent API acceptance for the approved supplier update workflow.

All rows are synthetic and all SQLite files live under pytest tmp_path.
"""
import copy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.store import WorkspaceStore

BASE = "/api/workspace"


@pytest.fixture
def environment(tmp_path):
    store = WorkspaceStore(tmp_path / "independent.sqlite")
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        supplier = client.post(BASE + "/suppliers", json={"name": "SYNTHETIC Supplier"}).json()
        yield store, client, supplier
    store.close()


def quote(model, amount, supplier_id, scope="weekly", **extra):
    return dict(model_id=model, supplier_id=supplier_id, amount=amount,
                billing_metric="input", pricing_unit_id="cny_mtoken",
                pricing_conditions={"tax_status": "included"}, source_scope=scope,
                source_sheet="Quotes", **extra)


def update_request(supplier_id, entries, **extra):
    return dict(supplier_id=supplier_id, source_scope="weekly", mode="full",
                scope_confirmed=True, complete_read=True, expected_sheets=["Quotes"],
                covered_sheets=["Quotes"], source_version="2", file_sha256="a" * 64,
                entries=entries, **extra)


def preview(client, request):
    response = client.post(BASE + "/supplier-updates/preview", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, request, review, identifier="synthetic-api"):
    return client.post(BASE + "/supplier-updates/apply", json={
        "request": request, "expected_catalog_token": review["base_catalog_token"],
        "expected_review_digest": review["review_digest"], "import_id": identifier,
    })


def test_complete_replace_updates_adds_deletes_only_confirmed_scope(environment):
    store, client, supplier = environment
    sid = supplier["supplier_id"]
    seed = store.apply_bundle({"create": [
        quote("synthetic/A", "10", sid), quote("synthetic/C", "20", sid),
        quote("synthetic/manual", "99", sid, scope=None),
    ]})["items"]
    request = update_request(sid, [quote("synthetic/A", "7", sid), quote("synthetic/B", "4", sid)])
    before = store.catalog_snapshot()
    review = preview(client, request)
    assert store.catalog_snapshot() == before
    assert review["ready"]
    assert {key: len(review["groups"][key]) for key in ("create", "update", "delete")} == {
        "create": 1, "update": 1, "delete": 1,
    }
    result = apply(client, request, review)
    assert result.status_code == 200, result.text
    rows = store.catalog_snapshot()["quotes"]
    by_model = {row["model_id"]: row for row in rows}
    assert set(by_model) == {"synthetic/A", "synthetic/B", "synthetic/manual"}
    assert by_model["synthetic/A"]["id"] == seed[0]["id"]
    assert by_model["synthetic/A"]["revision"] == seed[0]["revision"] + 1
    assert by_model["synthetic/A"]["amount"] == "7"
    assert by_model["synthetic/manual"]["revision"] == seed[2]["revision"]
    # Approval of the new quote creates its missing model in the same transaction.
    assert any(model["model_id"] == "synthetic/B" for model in store.catalog_snapshot()["models"])
    replay = apply(client, request, review)
    assert replay.status_code == 200 and replay.json()["reused"] is True


def test_incremental_retains_absent_rows_and_incomplete_full_cannot_delete(environment):
    store, client, supplier = environment
    sid = supplier["supplier_id"]
    store.apply_bundle({"create": [quote("synthetic/A", "10", sid), quote("synthetic/C", "20", sid)]})
    request = update_request(sid, [quote("synthetic/A", "7", sid)])
    request["mode"] = "incremental"
    review = preview(client, request)
    assert review["groups"]["delete"] == []
    assert apply(client, request, review).status_code == 200
    request = copy.deepcopy(request)
    request.update(mode="full", complete_read=False, covered_sheets=[], source_version="3")
    before = store.catalog_snapshot()
    review = preview(client, request)
    assert not review["ready"] and review["groups"]["delete"] == []
    assert apply(client, request, review, "incomplete").status_code == 422
    assert store.catalog_snapshot() == before


def test_catalog_drift_requires_new_review_and_never_partially_applies(environment):
    store, client, supplier = environment
    sid = supplier["supplier_id"]
    old = store.apply_bundle({"create": [quote("synthetic/A", "10", sid)]})["items"][0]
    request = update_request(sid, [quote("synthetic/A", "7", sid), quote("synthetic/B", "4", sid)])
    review = preview(client, request)
    changed = client.patch(BASE + "/quotes/" + str(old["id"]), json={
        "expected_revision": old["revision"], "amount": "9",
    })
    assert changed.status_code == 200, changed.text
    before = store.catalog_snapshot()
    result = apply(client, request, review)
    assert result.status_code == 409, result.text
    assert store.catalog_snapshot() == before
    assert not any(row["model_id"] == "synthetic/B" for row in before["models"])


def test_supplier_required_for_quotes_but_not_model_and_official(environment):
    store, client, _ = environment
    response = client.post(BASE + "/catalog", json={"bundle": {
        "models": [{"model_id": "synthetic/model"}],
        "official_prices": [{"model_id": "synthetic/model", "billing_metric": "input",
                             "amount": "2", "pricing_unit_id": "cny_mtoken"}],
    }})
    assert response.status_code == 200, response.text
    before = store.catalog_snapshot()
    for endpoint in ("/catalog/preview", "/catalog"):
        response = client.post(BASE + endpoint, json={"bundle": {
            "create": [{"model_id": "synthetic/model", "discount_rate": "0.70"}],
        }})
        # Preview's endpoint accepts a bare bundle, while commit wraps it.
        if endpoint.endswith("preview"):
            response = client.post(BASE + endpoint, json={
                "create": [{"model_id": "synthetic/model", "discount_rate": "0.70"}],
            })
        assert response.status_code == 422, response.text
    assert store.catalog_snapshot() == before
