"""Read-only comparison of condition scenarios, explicit scope and local official IDs."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from tokensql.workspace.compare import compare_snapshots, main


def catalog(prefix="a"):
    return {
        "models": [{"model_id": "m", "aliases": []}],
        "official_prices": [
            {"official_price_id": prefix + "-busy", "model_id": "m", "billing_item": "input",
             "billing_metric": "input", "pricing_conditions": {"market": "domestic", "time_band": "busy"},
             "conditions": {"table": "domestic busy"}, "amount": "2", "pricing_unit_id": "cny_mtoken"},
            {"official_price_id": prefix + "-idle", "model_id": "m", "billing_item": "input",
             "billing_metric": "input", "pricing_conditions": {"market": "domestic", "time_band": "off_peak"},
             "conditions": {"table": "domestic idle"}, "amount": "1", "pricing_unit_id": "cny_mtoken"},
        ],
        "quotes": [{"model_id": "m", "discount_rate": "0.70", "discount_scope": "items",
                    "discount_items": [{"official_price_id": prefix + "-busy"}],
                    "conditions": {"literal": "original source text"}}],
    }


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_distinct_conditions_are_valid_and_opaque_official_ids_are_local():
    baseline, actual = catalog("gold"), catalog("actual")
    before = deepcopy((baseline, actual))
    report = compare_snapshots(baseline, actual)
    assert report["pass"], report
    assert report["matched"] == {"models": 1, "official_prices": 2, "quotes": 1}
    assert (baseline, actual) == before


def test_direct_official_ids_do_not_define_price_business_identity():
    baseline, actual = catalog(), catalog("other")
    baseline["quotes"][0].update(discount_scope="model", discount_items=[])
    actual["quotes"][0].update(discount_scope="model", discount_items=[])
    assert compare_snapshots(baseline, actual)["pass"]


def test_item_reference_to_different_scenario_cannot_pass_with_equal_discount():
    actual = catalog("other")
    actual["quotes"][0]["discount_items"][0]["official_price_id"] = "other-idle"
    report = compare_snapshots(catalog(), actual)
    assert not report["pass"] and report["missing"] and report["unexpected"]


def test_dangling_and_duplicate_local_official_references_fail_even_when_both_sides_identical():
    value = catalog()
    value["quotes"][0]["discount_items"][0]["official_price_id"] = "absent"
    assert "invalid_discount_item_reference" in codes(compare_snapshots(value, deepcopy(value)))
    value = catalog()
    value["official_prices"][1]["official_price_id"] = value["official_prices"][0]["official_price_id"]
    result = compare_snapshots(value, deepcopy(value))
    assert not result["pass"] and "duplicate_official_price_id" in codes(result)


def test_cross_model_official_reference_is_invalid():
    value = catalog()
    value["models"].append({"model_id": "other"})
    value["official_prices"].append({"official_price_id": "foreign", "model_id": "other",
                                     "billing_item": "input", "amount": "1"})
    value["quotes"][0]["discount_items"][0]["official_price_id"] = "foreign"
    assert "invalid_discount_item_reference" in codes(compare_snapshots(value, deepcopy(value)))


@pytest.mark.parametrize("field,value", [
    ("billing_metric", "output"),
    ("pricing_conditions", {"market": "international", "time_band": "busy"}),
    ("pricing_conditions", {"market": "domestic", "time_band": "off_peak", "mode": "batch"}),
    ("conditions", {"table": "rewritten original source"}),
])
def test_official_conditions_metric_and_original_conditions_cannot_change_silently(field, value):
    actual = catalog()
    actual["official_prices"][0][field] = value
    assert not compare_snapshots(catalog(), actual)["pass"]


@pytest.mark.parametrize("field,value", [
    ("billing_metric", "input"),
    ("pricing_conditions", {"input_tokens": {"min": 0, "max": 100, "max_inclusive": False}}),
    ("discount_scope", "model"),
    ("discount_items", [{"billing_metric": "output"}]),
    ("conditions", {"literal": "altered"}),
])
def test_quote_structure_scope_selectors_and_original_conditions_are_compared(field, value):
    actual = catalog()
    actual["quotes"][0][field] = value
    assert not compare_snapshots(catalog(), actual)["pass"]


def test_range_defaults_and_selector_order_are_semantically_normalized():
    baseline, actual = catalog(), catalog("other")
    conditions = {"input_tokens": {"min": 0, "max": 100}, "output_modality": "text"}
    baseline["official_prices"][0]["pricing_conditions"] = deepcopy(conditions)
    actual["official_prices"][0]["pricing_conditions"] = {
        **conditions, "input_tokens": {"min": 0, "max": 100, "min_inclusive": True, "max_inclusive": True},
        "raw": {}, "mode": None}
    for snapshot in (baseline, actual):
        quote = snapshot["quotes"][0]
        quote["discount_items"] = [{"billing_metric": "input", "pricing_conditions": conditions},
                                   {"billing_item": "output"}]
    actual["quotes"][0]["discount_items"].reverse()
    assert compare_snapshots(baseline, actual)["pass"]
    actual["official_prices"][0]["pricing_conditions"]["input_tokens"]["max_inclusive"] = False
    assert not compare_snapshots(baseline, actual)["pass"]


def test_legacy_literals_use_exact_same_semantics_as_the_backend_migration():
    baseline = {"models": [{"model_id": "m"}], "official_prices": [
        {"model_id": "m", "billing_item": "input [国内版] [忙时]", "amount": "2",
         "pricing_unit_id": "cny_mtoken"}],
        "quotes": [{"model_id": "m", "discount_rate": "0.70",
                    "source": "【港研院】Lab折扣以及模型计费细则.xlsx",
                    "conditions": {"折扣口径": "合作折扣基于原厂定价-不含税"}}]}
    actual = deepcopy(baseline)
    actual["official_prices"][0].update(official_price_id="local", billing_metric="input",
        pricing_conditions={"market": "domestic", "time_band": "busy"}, conditions={})
    actual["quotes"][0].update(billing_metric=None, pricing_conditions={}, discount_scope="model", discount_items=[])
    assert compare_snapshots(baseline, actual)["pass"]
    baseline["quotes"][0]["source"] = "unrelated.xlsx"
    assert not compare_snapshots(baseline, actual)["pass"]


def test_long_unrecognized_labels_do_not_gain_guessed_metrics_or_conditions():
    baseline = {"models": [{"model_id": "m"}], "official_prices": [
        {"model_id": "m", "billing_item": "mysterious input-like condition", "amount": "1"}],
        "quotes": []}
    actual = deepcopy(baseline)
    actual["official_prices"][0]["billing_metric"] = "input"
    assert not compare_snapshots(baseline, actual)["pass"]


def test_same_official_scenario_with_different_effective_dates_still_violates_uniqueness():
    baseline = catalog()
    duplicate = deepcopy(baseline["official_prices"][0])
    duplicate.update(official_price_id="third", effective_date="2025-01-01")
    baseline["official_prices"].append(duplicate)
    result = compare_snapshots(baseline, deepcopy(baseline))
    assert not result["pass"] and "duplicate_price_key" in codes(result)


@pytest.mark.parametrize("unit_id,dimension", [
    ("cny_request", "request"), ("cny_account_month", "account_month"), ("cny_mtoken_hour", "token_hour"),
])
def test_composite_request_and_subscription_units_never_gain_token_estimates(unit_id, dimension):
    value = {"models": [{"model_id": "m"}], "official_prices": [],
             "quotes": [{"model_id": "m", "billing_item": "input", "amount": "5",
                         "pricing_unit_id": unit_id}]}
    context = {"revision": 1, "token_equivalences": [
        {"model_id": "m", "billing_item": "input", "unit": dimension,
         "tokens_per_unit": "1000", "source": "explicit but inapplicable fixture factor"}]}
    result = compare_snapshots(value, deepcopy(value), pricing_context=context)
    assert result["pass"], result
    assert result["estimates"] == []


def test_model_identity_split_still_fails_with_same_condition_prices():
    baseline, actual = catalog(), catalog()
    actual["models"].append({"model_id": "m-output", "aliases": ["m"]})
    assert "model_identity_split" in codes(compare_snapshots(baseline, actual))


@pytest.mark.parametrize("conditions", [
    {"input_tokens": {"min": 10, "max": 1}}, {"unknown_typed_key": "must stay raw"},
    {"with_audio": "yes"},
])
def test_invalid_typed_conditions_cannot_pass_self_comparison(conditions):
    value = catalog()
    value["official_prices"][0]["pricing_conditions"] = conditions
    result = compare_snapshots(value, deepcopy(value))
    assert not result["pass"] and "invalid_pricing_structure" in codes(result)


def test_real_sqlite_readonly_comparison_preserves_both_files_and_resolves_local_ids(tmp_path):
    paths = [tmp_path / "baseline.sqlite3", tmp_path / "actual.sqlite3"]
    for path, prefix in zip(paths, ("baseline", "actual")):
        snapshot = catalog(prefix)
        with sqlite3.connect(path) as connection:
            for key, table in (("models", "pricing_model"), ("official_prices", "pricing_official"),
                               ("quotes", "pricing_quote")):
                connection.execute("CREATE TABLE " + table + "(payload TEXT NOT NULL,status TEXT DEFAULT 'active')")
                connection.executemany("INSERT INTO " + table + "(payload) VALUES (?)",
                                       [(json.dumps(row),) for row in snapshot[key]])
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    output = tmp_path / "report.json"
    assert main(["--baseline", str(paths[0]), "--actual", str(paths[1]), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["pass"]
    for side in ("baseline", "actual"):
        evidence = report["read_evidence"][side]
        assert evidence["sqlite_mode"] == "ro" and evidence["sha256_before"] == evidence["sha256_after"]
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]

def test_opaque_billing_labels_keep_punctuation_instead_of_fuzzy_aliasing():
    baseline = {"models": [{"model_id": "m"}], "official_prices": [
        {"model_id": "m", "billing_item": "custom input 1-2 band", "amount": "1"}],
        "quotes": [{"model_id": "m", "billing_item": "custom input 1-2 band", "amount": "1"}]}
    actual = deepcopy(baseline)
    for row in actual["official_prices"] + actual["quotes"]:
        row["billing_item"] = "custom input 12 band"
    assert not compare_snapshots(baseline, actual)["pass"]


def test_old_official_raw_condition_mirror_is_compatible_only_when_typed_was_absent():
    baseline = {"models": [{"model_id": "m"}], "official_prices": [
        {"model_id": "m", "billing_item": "input", "amount": "1", "conditions": {"tier": "old raw"}}],
        "quotes": []}
    actual = deepcopy(baseline)
    actual["official_prices"][0].update(billing_metric="input",
        pricing_conditions={"raw": {"tier": "old raw"}})
    assert compare_snapshots(baseline, actual)["pass"]
    baseline["official_prices"][0]["pricing_conditions"] = {}
    assert not compare_snapshots(baseline, actual)["pass"]

def test_exact_official_literal_conditions_merge_with_independent_typed_conditions():
    baseline = {"models": [{"model_id": "m"}], "official_prices": [
        {"model_id": "m", "billing_item": "cache write (5m)", "amount": "1",
         "pricing_conditions": {"market": "domestic"}}], "quotes": []}
    actual = deepcopy(baseline)
    actual["official_prices"][0].update(billing_metric="cache_write",
        pricing_conditions={"market": "domestic", "cache_ttl_seconds": 300})
    assert compare_snapshots(baseline, actual)["pass"]
    conflicting = deepcopy(baseline)
    conflicting["official_prices"][0]["pricing_conditions"]["cache_ttl_seconds"] = 3600
    result = compare_snapshots(conflicting, deepcopy(conflicting))
    assert not result["pass"] and "invalid_pricing_structure" in codes(result)
