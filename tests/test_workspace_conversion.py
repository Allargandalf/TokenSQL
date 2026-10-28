"""Raw-currency preservation, evidence-backed unit estimates and live context."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.conversion import normalize_price, quote_projection
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.schemas import PricingContextUpdate
from tokensql.workspace.store import WorkspaceStore, RevisionConflictError, prepare_bundle
from tests.test_workspace_data import model, official, quote


def rates(usd="7", hkd="0.9", **extra):
    return [{"currency": currency, "cny_per_unit": amount, "source": "human provided benchmark",
             "source_url": "https://example.test/rates", "observed_at": "2026-10-16T00:00:00+00:00",
             **extra} for currency, amount in (("USD", usd), ("HKD", hkd))]


def factor(**extra):
    return {"model_id": "maker/model-a", "billing_item": "input", "unit": "second",
            "tokens_per_unit": "100", "source": "model benchmark run",
            "source_ref": "report#measurement-17", "observed_at": "2026-10-16T00:00:00+00:00",
            "assumptions": "Specific resolution/audio/context documented by billing item", **extra}


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "conversion.sqlite")
    value.apply_bundle({"models": [model()], "official_prices": [official()]})
    try:
        yield value
    finally:
        value.close()


def test_raw_currency_and_discount_unchanged_by_display_context(store):
    store.set_pricing_context(fx_rates=rates(), token_equivalences=[], expected_revision=1)
    identifier = store.apply_bundle({"create": [quote(amount="0.001", currency="USD", discount_rate="0.7")]})["items"][0]["id"]
    before = store.get_quote(identifier).model_dump(mode="json")
    payload = store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (identifier,)).fetchone()[0]
    token = store.catalog_token()
    context = store.set_pricing_context(fx_rates=rates(usd="8"), token_equivalences=[], expected_revision=2)
    assert context["revision"] == 3 and store.catalog_token() == token
    assert store.get_quote(identifier).model_dump(mode="json") == before
    assert before["amount"] == "0.001" and before["discount_rate"] == "0.70"
    assert before["raw_discount_rate"] == "0.70" and before["normalized"] == {}
    assert store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (identifier,)).fetchone()[0] == payload
    assert len(store.pricing_context_history()) == 3


@pytest.mark.parametrize("source_currency", ["USD", "HKD", "CNY"])
def test_official_and_supplier_raw_currency_and_unit_retained_without_projection(store, source_currency):
    store.apply_bundle({"official_prices": [official(amount="1", currency="USD", expected_revision=1)]})
    item = store.apply_bundle({"create": [quote(amount="0.0054", currency=source_currency)]})["items"][0]
    assert item["official_price"]["amount"] == "1"
    assert item["official_price"]["currency"] == "USD"
    assert "normalized" not in item["official_price"]
    assert item["amount"] == "0.0054" and item["currency"] == source_currency
    assert item["discount_rate"] is None and item["converted_supplier_amount"] is None


@pytest.mark.parametrize("unit,quantity", [("token", "1000"), ("second", "1"), ("image", "1")])
@pytest.mark.parametrize("with_context", [False, True])
def test_raw_only_even_when_display_conversion_has_complete_evidence(store, unit, quantity, with_context):
    if with_context:
        store.set_pricing_context(fx_rates=rates(), token_equivalences=[factor()], expected_revision=1)
    item = store.apply_bundle({"create": [quote(amount="0.001", discount_rate="0.8", pricing_basis="amount", unit=unit, unit_quantity=quantity)]})["items"][0]
    assert item["amount"] == "0.001" and item["discount_rate"] == "0.80"
    assert item["unit"] == unit and item["unit_quantity"] == quantity
    assert item["derived_amount"] is None and item["converted_supplier_amount"] is None
    assert item["normalized"] == {} and item["discount_evidence"] == {} and item["conversion_evidence"] == {}
    raw = json.loads(store._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (item["id"],)).fetchone()[0])
    assert raw["amount"] == "0.001" and raw["discount_rate"] == "0.80"
    assert "derived_amount" not in raw and "normalized" not in raw


@pytest.mark.parametrize("currency", [None, "CNY", "USD", "HKD"])
def test_discount_only_never_needs_official_price_or_fx(store, currency):
    item = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "new-unknown", "discount_rate": "1.2", "currency": currency}]})["items"][0]
    assert item["amount"] is None and item["discount_rate"] == "1.20"
    assert item["raw_discount_rate"] == "1.20" and item["official_price"] is None
    assert item["derived_amount"] is None and item["normalized"] == {}
    assert item["vendor"] is None and item["pricing_unit_id"] is None


def test_context_validation_revisions_source_evidence_and_cny_identity(store):
    for changed in (
        {"currency": "CNY", "cny_per_unit": "2", "source": "wrong", "observed_at": "2026-01-01T00:00:00Z"},
        {"currency": "USD", "cny_per_unit": "7", "source": "", "observed_at": "2026-01-01T00:00:00Z"},
        {"currency": "USD", "cny_per_unit": "7", "source": "x", "observed_at": "2026-01-01"},
    ):
        with pytest.raises(ValueError):
            store.set_pricing_context(fx_rates=[changed], token_equivalences=[], expected_revision=1)
    with pytest.raises(ValueError):
        store.set_pricing_context(fx_rates=rates(), token_equivalences=[factor(source_ref=" ")], expected_revision=1)
    with pytest.raises(ValueError):
        store.set_pricing_context(fx_rates=rates() + rates(), token_equivalences=[], expected_revision=1)
    with pytest.raises(ValueError):
        store.set_pricing_context(fx_rates=[], token_equivalences=[factor(unit="token")], expected_revision=1)
    with pytest.raises(ValueError, match="second or image"):
        store.set_pricing_context(fx_rates=[], token_equivalences=[factor(unit="request")], expected_revision=1)
    store.set_pricing_context(fx_rates=rates(), token_equivalences=[], expected_revision=1)
    with pytest.raises(RevisionConflictError):
        store.set_pricing_context(fx_rates=[], token_equivalences=[], expected_revision=1)


def test_display_context_does_not_invalidate_raw_review_and_snapshot_preserves_claims(store):
    item = store.apply_bundle({"create": [quote(amount="0.001", currency="USD", discount_rate="0.7")]})["items"][0]
    proposal = {"create": [quote()]}
    reviewed = store.preview_bundle(proposal)
    store.set_pricing_context(fx_rates=rates(usd="8"), token_equivalences=[factor()], expected_revision=1)
    applied = store.apply_bundle(proposal, expected_catalog_token=reviewed["base_catalog_token"])
    assert applied["changes"]["added"] == 1
    read = store.get_quote(item["id"])
    assert read.raw_discount_rate == Decimal("0.7") and read.discount_rate == Decimal("0.7")
    prepared = prepare_bundle(proposal, store.catalog_snapshot())
    assert prepared["changes"]["added"] == 1
    edited = store.update_quote(item["id"], {"note": "Human annotation"}, expected_revision=1)
    assert edited.amount == Decimal("0.001") and edited.discount_rate == Decimal("0.7")


def test_expiry_never_changes_raw_review_token_or_computes_backend_values(store, monkeypatch):
    import tokensql.workspace.store as store_module
    store.set_pricing_context(fx_rates=rates(expires_at="2026-10-17T00:00:00+00:00"),
                              token_equivalences=[], expected_revision=1)
    class Frozen(datetime):
        value = datetime(2026, 10, 16, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.value
    monkeypatch.setattr(store_module, "datetime", Frozen)
    before = store.catalog_token()
    Frozen.value = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert store.catalog_token() == before
    assert normalize_price(quote(currency="USD"), store.pricing_context(), at=Frozen.value) == {}
    raw = quote_projection(quote(currency="USD", discount_rate="0.7"), official(), store.pricing_context())
    assert raw["discount_rate"] == "0.7" and raw["derived_amount"] is None


def test_context_api_get_put_history_and_atomic_conflict(store):
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        assert client.get("/api/workspace/pricing-context").json()["revision"] == 1
        body = {"fx_rates": rates(), "token_equivalences": [factor()], "expected_revision": 1}
        result = client.put("/api/workspace/pricing-context", json=body)
        assert result.status_code == 200 and result.json()["revision"] == 2
        assert client.put("/api/workspace/pricing-context", json=body).status_code == 409
        assert len(client.get("/api/workspace/pricing-context/history").json()["items"]) == 2
        catalog = client.get("/api/workspace/catalog").json()
        assert catalog["pricing_context"]["revision"] == 2
