"""Scenario matching, rule precedence, stable-ID upgrades and raw provenance fidelity."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.pricing_conditions import PricingConditions, TokenRange, legacy_official_semantics
from tokensql.workspace.schemas import OfficialPriceCreate, PricingBundle, QuoteCreate
from tokensql.workspace.store import IdempotencyConflictError, RevisionConflictError, WorkspaceStore, _json
from tokensql.workspace.units import PRICING_UNIT_BY_ID
from tests.test_workspace_nullable_data import old_database, logical_dump


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "conditions.sqlite")
    try:
        yield value
    finally:
        value.close()


def price(metric="input", **changes):
    return {"model_id": "m", "billing_item": metric, "billing_metric": metric,
            "amount": "10.1234567890123456789", "pricing_unit_id": "usd_mtoken",
            "source": "official", "source_ref": "https://example.test/pricing", **changes}


def rule(**changes):
    return {"model_id": "m", "discount_rate": "0.7", "discount_scope": "model",
            "supplier": "S", "currency": None, **changes}


def add(store, *prices):
    store.apply_bundle({"official_prices": list(prices)})


def test_unknown_null_is_not_all_and_existing_candidates_are_not_missing(store):
    add(store, price(), price("output"))
    quote = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "discount_rate": "0.70"}]})["items"][0]
    assert quote["official_match"]["status"] == "needs_scope"
    assert len(quote["official_match"]["candidate_ids"]) == 2
    assert quote["official_price"] is None and quote["official_prices"] == []
    assert quote["discount_scope"] == "unknown" and quote["currency"] == "CNY"
    assert store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "no-prices"}]})["items"][0]["official_match"]["status"] == "missing"
    with pytest.raises(ValueError):
        QuoteCreate.model_validate({"supplier": "Synthetic Supplier", "model_id": "m", "discount_scope": "all"})


def test_model_discount_expands_five_claude_metrics_without_mutating_raw_data(store):
    labels = ["input", "output", "cache read", "cache write (5m)", "cache write (1h)"]
    add(store, *[price(label, billing_metric=None) for label in labels])
    quote = store.apply_bundle({"create": [rule(conditions={"折扣口径": "合作折扣基于原厂定价-不含税"},
        source="【港研院】Lab折扣以及模型计费细则.xlsx", source_ref="汇总表!A8:E8")]})["items"][0]
    assert quote["discount_rate"] == "0.70" and quote["currency"] is None
    assert quote["official_match"]["status"] == "multiple" and quote["official_price"] is None
    assert {item["billing_item"] for item in quote["official_prices"]} == set(labels)
    assert len(quote["effective_discount_rules"]) == 5
    assert all(item["discount_rate"] == "0.70" for item in quote["effective_discount_rules"])
    assert quote["amount"] is None and quote["derived_amount"] is None
    assert {item["pricing_conditions"].get("cache_ttl_seconds") for item in quote["official_prices"]
            if item["billing_metric"] == "cache_write"} == {300, 3600}
    before = tuple(store._conn.execute("SELECT payload,revision FROM pricing_quote").fetchone())
    store.get_quote(quote["id"])
    store.preview_bundle({})
    assert tuple(store._conn.execute("SELECT payload,revision FROM pricing_quote").fetchone()) == before


def test_conditions_preserve_market_modalities_and_boolean_false(store):
    add(store, price(pricing_conditions={"market": "domestic", "input_modality": "text", "with_audio": False}),
        price(pricing_conditions={"market": "international", "input_modality": "image", "with_audio": True}))
    result = store.apply_bundle({"create": [
        {"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input", "pricing_conditions": {
            "market": "domestic", "input_modality": "text", "with_audio": False}},
        {"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input"}]})["items"]
    assert result[0]["official_match"]["status"] == "matched"
    assert result[0]["official_price"]["pricing_conditions"]["with_audio"] is False
    assert result[1]["official_match"]["status"] == "ambiguous"
    assert result[1]["official_price"] is None and result[1]["official_prices"] == []
    assert result[0]["currency"] == "CNY"  # Currency never infers market.


@pytest.mark.parametrize(("bounds", "state", "tier"), [
    ({"min": 200000, "max": 200000}, "matched", "short"),
    ({"min": 200000, "min_inclusive": False, "max": 210000}, "matched", "long"),
    ({"min": 199999, "max": 200001}, "ambiguous", None),
    ({"min": 999999, "max": 1000000}, "matched", "long"),
])
def test_token_range_boundaries_are_exact(store, bounds, state, tier):
    add(store, price(note="short", pricing_conditions={"input_tokens": {"max": 200000}}),
        price(note="long", pricing_conditions={"input_tokens": {"min": 200000, "min_inclusive": False}}))
    row = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input",
        "pricing_conditions": {"input_tokens": bounds}}]})["items"][0]
    assert row["official_match"]["status"] == state
    if tier:
        assert row["official_price"]["note"] == tier


def test_invalid_condition_ranges_and_binary_numbers_are_rejected():
    for invalid in ({"min": 5, "max": 4}, {"min": 5, "max": 5, "max_inclusive": False},
                    {}, {"min": 1.5}, {"max": True}):
        with pytest.raises(ValueError):
            TokenRange.model_validate(invalid)
    with pytest.raises(ValueError):
        PricingConditions.model_validate({"with_audio": 0})


def test_unknown_concrete_metric_needs_conditions_even_with_one_official_candidate(store):
    add(store, price(pricing_conditions={"market": "domestic", "time_band": "busy"}))
    missing = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input",
        "discount_rate": "0.8"}]})["items"][0]
    assert missing["official_match"]["status"] == "ambiguous"
    assert missing["official_price"] is None and len(missing["official_match"]["candidate_ids"]) == 1
    assert store.apply_bundle({"create": [rule()]})["items"][0]["official_match"]["status"] == "matched"
    explicit = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input",
        "pricing_conditions": {"market": "domestic", "time_band": "busy"}}]})["items"][0]
    assert explicit["official_match"]["status"] == "matched"


def test_tagged_promotions_are_isolated_even_when_supplier_and_channel_match(store):
    add(store, price())
    regular, promo, override = store.apply_bundle({"create": [
        rule(tags=["regular"]), rule(tags=["promotion"], discount_rate="0.2"),
        rule(tags=["regular"], discount_scope="items", discount_rate="0.5",
             discount_items=[{"billing_metric": "input"}])]})["items"]
    assert store.get_quote(regular["id"]).effective_discount_rules[0]["discount_rate"] == "0.50"
    assert store.get_quote(promo["id"]).effective_discount_rules[0]["discount_rate"] == "0.20"


def test_raw_legacy_conditions_never_establish_equivalence_or_select_a_cheapest_price(store):
    add(store, price("Input text long raw label", billing_metric=None, amount="1"),
        price("Input image long raw label", billing_metric=None, amount="9"))
    unknown = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input"}]})["items"][0]
    assert unknown["official_match"]["status"] == "ambiguous"
    assert len(unknown["official_match"]["candidate_ids"]) == 2
    assert unknown["official_price"] is None
    opaque = store.apply_bundle({"create": [rule(pricing_conditions={"legacy_text": "uncertain condition"})]})["items"][0]
    assert opaque["official_match"]["status"] == "ambiguous"
    broad = store.apply_bundle({"create": [rule()]})["items"][0]
    assert len(broad["official_prices"]) == 2


def test_opaque_official_conditions_are_preserved_as_distinct_scenarios(store):
    add(store, price(conditions={"tier": "raw first"}), price(conditions={"tier": "raw second"}))
    prices = store.catalog_snapshot()["official_prices"]
    assert {p["pricing_conditions"]["raw"]["tier"] for p in prices} == {"raw first", "raw second"}
    row = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input"}]})["items"][0]
    assert row["official_match"]["status"] == "ambiguous" and row["official_price"] is None
    broad = store.apply_bundle({"create": [rule()]})["items"][0]
    assert broad["official_match"]["status"] == "multiple" and len(broad["official_prices"]) == 2


@pytest.mark.parametrize("explicit", [{}, {"raw": {}}])
def test_explicit_empty_conditions_keep_provenance_out_of_matching(store, explicit):
    add(store, price(conditions={"header_cell": "Sheet1!D1", "currency_cell": "Sheet1!B2"},
                     pricing_conditions=explicit))
    row = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "billing_metric": "input"}]})["items"][0]
    assert row["official_match"]["status"] == "matched"
    assert row["official_price"]["pricing_conditions"] == {}
    assert row["official_price"]["conditions"]["header_cell"] == "Sheet1!D1"
    reopened = WorkspaceStore(store.path)
    try:
        price_row = reopened.catalog_snapshot()["official_prices"][0]
        assert price_row["pricing_conditions"] == {}
        assert price_row["conditions"]["currency_cell"] == "Sheet1!B2"
    finally:
        reopened.close()


def test_stable_ids_disambiguate_updates_and_scene_duplicates_return_409(store):
    add(store, price(pricing_conditions={"market": "domestic"}),
        price(pricing_conditions={"market": "international"}))
    original = store.catalog_snapshot()["official_prices"]
    target = original[0]
    changed = {key: value for key, value in target.items() if key in OfficialPriceCreate.model_fields}
    changed.update(amount="17", expected_revision=target["revision"])
    store.apply_bundle({"official_prices": [changed]})
    after = {p["official_price_id"]: p for p in store.catalog_snapshot()["official_prices"]}
    assert after[target["official_price_id"]]["amount"] == "17"
    assert after[target["official_price_id"]]["revision"] == target["revision"] + 1
    assert len(after) == 2
    app = FastAPI()
    app.include_router(create_data_router(store))
    with TestClient(app) as client:
        assert client.post("/api/workspace/catalog", json={"bundle": {"official_prices": [
            price(amount="99", expected_revision=1)]}}).status_code == 409
        duplicate = price(official_price_id="another-id", pricing_conditions=target["pricing_conditions"])
        assert client.post("/api/workspace/catalog", json={"bundle": {"official_prices": [duplicate]}}).status_code == 409
    with pytest.raises(RevisionConflictError):
        store.apply_bundle({"official_prices": [changed]})
    rebuilt = WorkspaceStore.from_snapshot(store.catalog_snapshot())
    try:
        assert rebuilt.catalog_snapshot() == store.catalog_snapshot()
    finally:
        rebuilt.close()


def test_metric_case_cannot_create_duplicate_semantic_prices(store):
    add(store, price())
    with pytest.raises(RevisionConflictError):
        add(store, price(billing_item="Input Price Original", billing_metric="INPUT",
                         official_price_id="duplicate-case"))
    assert len(store.catalog_snapshot()["official_prices"]) == 1


def test_item_override_and_equal_priority_conflicts_are_resolved_across_pages(store):
    add(store, price(), price("output"))
    result = store.apply_bundle({"create": [rule(), rule(discount_rate="0.50", discount_scope="items",
        discount_items=[{"billing_metric": "input"}])]})["items"]
    baseline = store.get_quote(result[0]["id"]).model_dump(mode="json")
    effective = {p["official_price_id"]: p for p in baseline["effective_discount_rules"]}
    for official in baseline["official_prices"]:
        applied = effective[official["official_price_id"]]
        assert applied["discount_rate"] == ("0.50" if official["billing_metric"] == "input" else "0.70")
        assert applied["quote_id"] == result[1 if official["billing_metric"] == "input" else 0]["id"]
    assert baseline["discount_rate"] == "0.70" and baseline["amount"] is None
    page, total = store.list_quotes(limit=1, offset=1)
    assert total == 2 and page[0].id == baseline["id"]
    assert page[0].effective_discount_rules == baseline["effective_discount_rules"]
    store.apply_bundle({"create": [rule(discount_rate="0.60", discount_scope="items",
        discount_items=[{"billing_metric": "input"}])]})
    conflict = store.get_quote(baseline["id"]).effective_discount_rules
    entry = next(p for p in conflict if p["reason"] == "conflicting_discount_rules")
    assert entry["quote_id"] is None and entry["discount_rate"] is None
    assert len(entry["candidate_quote_ids"]) == 2
    assert store.get_quote(baseline["id"]).discount_rate == Decimal("0.70")


def test_supplier_identities_and_channels_do_not_leak_rules(store):
    add(store, price())
    a, b, c, d = store.apply_bundle({"create": [rule(supplier="Supplier A"), rule(supplier="Supplier B", discount_rate="0.3"),
        rule(supplier_model_id="one"), rule(supplier_model_id="two", discount_rate="0.2")]})["items"]
    for row, expected in ((a, "0.70"), (b, "0.30"), (c, "0.70"), (d, "0.20")):
        applied = store.get_quote(row["id"]).effective_discount_rules[0]
        assert applied["discount_rate"] == expected and applied["quote_id"] == row["id"]


def test_semantic_migration_preserves_every_source_fact_audits_and_receipts(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    conn = sqlite3.connect(path)
    original = json.loads(conn.execute("SELECT payload FROM pricing_quote WHERE id=7").fetchone()[0])
    original.update(billing_item=None, amount=None, discount_rate="0.70", currency=None, pricing_unit_id=None,
        conditions={"折扣口径": "合作折扣基于原厂定价-不含税"},
        source="【港研院】Lab折扣以及模型计费细则.xlsx", source_ref="汇总表!A8:E8")
    conn.close()
    # Use a current nullable predecessor schema while preserving the old composite official key.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_master SET sql=replace(sql,'billing_item TEXT NOT NULL','billing_item TEXT') WHERE name='pricing_quote'")
    conn.execute("PRAGMA writable_schema=OFF")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE pricing_quote SET billing_item=NULL,payload=? WHERE id=7", (_json(original),))
    for label in ("output", "cache read", "cache write (5m)", "cache write (1h)"):
        value = price(label, model_id="old-model", billing_metric=None)
        value.pop("billing_metric")
        conn.execute("INSERT INTO pricing_official VALUES (?,?,?,?,?,?)",
            ("old-model", label, _json(value), 1, "created", "updated"))
    conn.execute("CREATE INDEX custom_official_revision ON pricing_official(revision)")
    conn.execute("CREATE TRIGGER custom_official_trigger AFTER UPDATE ON pricing_official BEGIN SELECT 1; END")
    receipt = tuple(conn.execute("SELECT * FROM pricing_import").fetchone())
    history = tuple(conn.execute("SELECT * FROM pricing_audit WHERE id=11").fetchone())
    conn.commit()
    conn.close()
    migrated = WorkspaceStore(path)
    try:
        row = migrated.get_quote(7)
        assert row.discount_scope == "model" and row.revision == 4 and row.currency is None
        assert row.source_ref == original["source_ref"] and row.conditions == original["conditions"]
        assert row.discount_rate == Decimal("0.70") and row.amount is None
        assert len(row.official_prices) == 5 and row.official_match.status == "multiple"
        assert tuple(migrated._conn.execute("SELECT * FROM pricing_import").fetchone()) == receipt
        assert tuple(migrated._conn.execute("SELECT * FROM pricing_audit WHERE id=11").fetchone()) == history
        assert migrated._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated._conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'custom_official_%'").fetchone()[0] == 2
        audits = migrated._conn.execute("SELECT * FROM pricing_audit WHERE actor='migration:pricing-conditions'").fetchall()
        assert len(audits) == 6
        quote_audit = next(a for a in audits if a["entity_type"] == "quote")
        assert json.loads(quote_audit["old_value"])["currency"] is None
        assert json.loads(quote_audit["new_value"])["discount_scope"] == "model"
        snapshot = migrated.catalog_snapshot()
        audit_count = migrated._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0]
    finally:
        migrated.close()
    again = WorkspaceStore(path)
    try:
        assert again.catalog_snapshot() == snapshot
        assert again._conn.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] == audit_count
    finally:
        again.close()


def test_condition_migration_failure_rolls_back_nullable_precision_and_ids_together(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    old_database(path)
    conn = sqlite3.connect(path)
    row = json.loads(conn.execute("SELECT payload FROM pricing_quote").fetchone()[0])
    row["discount_rate"] = "0.70555"
    conn.execute("UPDATE pricing_quote SET payload=?", (_json(row),))
    conn.commit()
    conn.close()
    before = logical_dump(path)
    audit = WorkspaceStore._audit
    def fail(self, kind, key, action, actor, old, new):
        if actor == "migration:pricing-conditions" and kind == "quote":
            raise sqlite3.OperationalError("synthetic semantic migration failure")
        return audit(self, kind, key, action, actor, old, new)
    monkeypatch.setattr(WorkspaceStore, "_audit", fail)
    with pytest.raises(sqlite3.OperationalError, match="synthetic semantic"):
        WorkspaceStore(path)
    assert logical_dump(path) == before
    monkeypatch.undo()
    recovered = WorkspaceStore(path)
    assert recovered.get_quote(7).discount_rate == Decimal("0.71")
    recovered.close()


def test_only_exact_evidenced_source_migrates_model_scope_and_raw_label_adapter_is_bounded():
    from tokensql.workspace.pricing_conditions import legacy_quote_scope
    known = rule(billing_item=None, amount=None, source="【港研院】Lab折扣以及模型计费细则.xlsx",
                 conditions={"折扣口径": "合作折扣基于原厂定价-不含税"})
    assert legacy_quote_scope(known) == "model"
    assert legacy_quote_scope({**known, "source": "another.xlsx"}) == "unknown"
    assert legacy_quote_scope({**known, "conditions": {"官方单价原文": "以官方原价为准"}}) == "unknown"
    assert legacy_official_semantics("[国内版][忙时]input") == ("input", {"market": "domestic", "time_band": "busy"})
    assert legacy_official_semantics("[国际版][闲时]output") == ("output", {"market": "international", "time_band": "off_peak"})
    assert legacy_official_semantics("input unspecified tier 128k")[0] is None


def test_historical_receipt_replay_is_narrow_and_immutable(store):
    request = {"create": [{"supplier": "Synthetic Supplier", "model_id": "m", "amount": "12.12345", "discount_rate": "0.70",
                           "conditions": {"raw": "unchanged"}}]}
    initial = store.apply_bundle(request, import_id="legacy")
    legacy = PricingBundle.model_validate(request).model_dump(mode="json")
    for item in legacy["create"]:
        for key in ("billing_metric", "pricing_conditions", "discount_scope", "discount_items"):
            item.pop(key)
    digest = hashlib.sha256(_json(legacy).encode()).hexdigest()
    store._conn.execute("UPDATE pricing_import SET payload_sha256=? WHERE import_id='legacy'", (digest,))
    before = tuple(store._conn.execute("SELECT * FROM pricing_import").fetchone())
    replay = store.apply_bundle(request, import_id="legacy")
    assert replay["reused"] is True and replay["items"] == initial["items"]
    for change in ({"amount": "12.12346"}, {"discount_rate": "0.71"},
                   {"conditions": {"raw": "changed"}}, {"discount_scope": "model"},
                   {"pricing_conditions": {"market": "domestic"}}):
        amended = {"create": [{**request["create"][0], **change}]}
        with pytest.raises(IdempotencyConflictError):
            store.apply_bundle(amended, import_id="legacy")
    assert tuple(store._conn.execute("SELECT * FROM pricing_import").fetchone()) == before


@pytest.mark.parametrize("currency", ["usd", "hkd", "cny"])
@pytest.mark.parametrize(("scale", "unit", "quantity", "dimension"), [
    ("mtoken_hour", "token_hour", "1000000", "token_time"),
    ("request", "request", "1", "request"),
    ("account_month", "account_month", "1", "subscription"),
])
def test_new_units_preserve_source_values_without_token_equivalence(store, currency, scale, unit, quantity, dimension):
    identifier = f"{currency}_{scale}"
    row = store.apply_bundle({"create": [{"supplier": "Synthetic Supplier", "model_id": "subscription", "amount": "1.234567890123456789",
        "pricing_unit_id": identifier}]})["items"][0]
    assert row["amount"] == "1.234567890123456789" and row["unit"] == unit and row["unit_quantity"] == quantity
    assert PRICING_UNIT_BY_ID[identifier].dimension == dimension
    assert row["derived_amount"] is None and row["converted_supplier_amount"] is None and row["normalized"] == {}
    with pytest.raises(ValueError, match="second or image"):
        store.set_pricing_context(fx_rates=[], token_equivalences=[{"model_id": "subscription",
            "billing_item": "input", "unit": unit, "tokens_per_unit": "1", "source": "synthetic",
            "source_ref": "test", "observed_at": "2026-01-01T00:00:00Z"}], expected_revision=1)
