"""Synthetic read-only identity and pricing comparison regression."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from tokensql.workspace.compare import compare_snapshots, main, read_snapshot


def catalog(currency="CNY"):
    return {
        "models": [{"model_id": "model-a", "vendor": "Example Labs", "model_name": "Model A",
                    "aliases": ["Known A"]}],
        "official_prices": [{"model_id": "model-a", "billing_item": "input", "amount": "20",
                             "currency": currency, "unit": "token", "unit_quantity": "1000000"}],
        "quotes": [{"model_id": "model-a", "billing_item": "input", "supplier": "Supplier One",
                    "tags": ["premium", "cn"], "supplier_model_id": "raw-a", "amount": "10",
                    "currency": currency, "unit": "token", "unit_quantity": "1000000",
                    "discount_rate": "0.5", "conditions": {}}],
    }


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_trusted_alias_and_unit_quantity_equivalence():
    baseline, actual = catalog(), catalog()
    actual["models"][0].update(model_id="Known_A", model_name="Known A", aliases=[])
    for row in actual["official_prices"] + actual["quotes"]:
        row.update(model_id="Known_A", unit="tokens", unit_quantity="1000")
        row["amount"] = "0.02" if row in actual["official_prices"] else "0.01"
    actual["quotes"][0]["tags"].reverse()
    result = compare_snapshots(baseline, actual)
    assert result["pass"], result
    assert result["identity"] == {"Known_A": "model-a"}
    assert result["matched"] == {"models": 1, "official_prices": 1, "quotes": 1}


def test_split_model_alias_identity_is_strict_failure():
    actual = catalog()
    duplicate = deepcopy(actual["models"][0])
    duplicate["model_id"] = "model-a-output"
    actual["models"].append(duplicate)
    result = compare_snapshots(catalog(), actual)
    assert not result["pass"]
    assert "model_identity_split" in codes(result)


def test_unknown_does_not_fuzzy_merge_and_manual_map_is_explicit():
    actual = catalog()
    actual["models"][0].update(model_id="completely-new", model_name="similar but untrusted", aliases=[])
    for row in actual["quotes"] + actual["official_prices"]:
        row["model_id"] = "completely-new"
    result = compare_snapshots(catalog(), actual)
    assert "unknown_model" in codes(result) and not result["pass"]
    assert compare_snapshots(catalog(), actual, identity_map={"completely-new": "model-a"})["pass"]


def test_conflicting_trusted_anchors_fail_ambiguously():
    baseline = catalog()
    baseline["models"].append({"model_id": "model-b", "vendor": "Example Labs", "model_name": "Model B",
                               "aliases": []})
    actual = catalog()
    actual["models"][0]["aliases"] = ["model-b"]
    result = compare_snapshots(baseline, actual)
    assert "ambiguous_model" in codes(result)
    assert not result["pass"]


@pytest.mark.parametrize("field,value", [("supplier", "Other Supplier"), ("tags", ["different"]), ("supplier_model_id", "other-channel"), ("billing_item", "output")])
def test_supplier_tags_channel_and_billing_item_are_independent(field, value):
    actual = catalog()
    actual["quotes"][0][field] = value
    result = compare_snapshots(catalog(), actual)
    assert not result["pass"] and result["missing"] and result["unexpected"]


def test_duplicate_equal_amount_is_not_overwritten():
    actual = catalog()
    actual["quotes"].append(deepcopy(actual["quotes"][0]))
    result = compare_snapshots(catalog(), actual)
    assert not result["pass"]
    assert len(result["unexpected"]) == 1
    assert result["matched"]["quotes"] == 1


def test_empty_catalogs_never_pass():
    result = compare_snapshots({"models": []}, {"models": []})
    assert not result["pass"] and "empty_catalog" in codes(result)


def test_price_and_rate_decimal_tolerance():
    actual = catalog()
    actual["quotes"][0]["amount"] = "10.000000000001"
    actual["quotes"][0]["discount_rate"] = "0.5000000000001"
    assert compare_snapshots(catalog(), actual)["pass"]
    actual["quotes"][0]["discount_rate"] = "0.5001"
    assert compare_snapshots(catalog(), actual)["pass"]  # Same fixed-two-place policy value.
    actual["quotes"][0]["discount_rate"] = "0.505"
    result = compare_snapshots(catalog(), actual)
    assert not result["pass"]
    assert result["issues"][-1]["evidence"][0]["code"] == "discount_rate_mismatch"


def test_same_original_foreign_currency_needs_no_fx():
    assert compare_snapshots(catalog("USD"), catalog("USD"))["pass"]


def test_cross_currency_requires_same_explicit_fx_snapshot():
    baseline, actual = catalog("USD"), catalog("CNY")
    for row in actual["official_prices"] + actual["quotes"]:
        row["amount"] = str(int(row["amount"]) * 7)
    assert not compare_snapshots(baseline, actual)["pass"]
    context = {"revision": 9, "fx_rates": [{"currency": "USD", "cny_per_unit": "7", "source": "Synthetic published rate",
                "observed_at": "2026-10-16T00:00:00Z"}]}
    result = compare_snapshots(baseline, actual, pricing_context=context)
    assert not result["pass"]  # FX equivalence must not hide original currency rewriting.
    assert result["issues"][-1]["evidence"][0]["shared_fx_equal"] is True
    assert result["comparison_context"]["revision"] == 9


def test_cross_dimension_estimate_is_evidence_never_strict_equivalence():
    actual = catalog()
    actual["quotes"][0].update(unit="second", unit_quantity="1", amount="1")
    context = {"revision": 2, "token_equivalences": [{
        "model_id": "model-a", "billing_item": "input", "unit": "second", "tokens_per_unit": "100000",
        "source": "Synthetic model-specific measurement", "source_ref": "fixture-only"}]}
    result = compare_snapshots(catalog(), actual, pricing_context=context)
    assert not result["pass"]
    assert result["estimates"][0]["estimated"] is True
    assert result["estimates"][0]["cny_per_million_tokens"] == "10.00000"


def test_shared_fx_requires_explicit_version_and_source():
    with pytest.raises(ValueError):
        compare_snapshots(catalog(), catalog(), pricing_context={"fx_rates": [{"currency": "USD", "cny_per_unit": "7", "source": "fixture"}]})
    with pytest.raises(ValueError):
        compare_snapshots(catalog(), catalog(), pricing_context={"revision": 1, "fx_rates": [{"currency": "USD", "cny_per_unit": "7"}]})


def test_different_snapshot_context_versions_are_not_silently_compared():
    baseline, actual = catalog(), catalog()
    baseline["fx_version"], actual["fx_version"] = "v1", "v2"
    assert "context_version_mismatch" in codes(compare_snapshots(baseline, actual))


def create_database(path, snapshot):
    with sqlite3.connect(path) as conn:
        for key, table in (("models", "pricing_model"), ("official_prices", "pricing_official"), ("quotes", "pricing_quote")):
            conn.execute("CREATE TABLE " + table + "(payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active')")
            conn.executemany("INSERT INTO " + table + "(payload) VALUES (?)", [(json.dumps(row),) for row in snapshot[key]])
        archived = deepcopy(snapshot["quotes"][0])
        archived["amount"] = "999"
        conn.execute("INSERT INTO pricing_quote(payload,status) VALUES (?,'archived')", (json.dumps(archived),))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_readonly_database_cli_preserves_both_hashes(tmp_path):
    baseline, actual, output = tmp_path / "gold.sqlite3", tmp_path / "actual.sqlite3", tmp_path / "private" / "comparison.json"
    create_database(baseline, catalog())
    create_database(actual, catalog())
    before = (sha(baseline), sha(actual))
    assert main(["--baseline", str(baseline), "--actual", str(actual), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["pass"]
    assert report["read_evidence"]["actual"]["sqlite_mode"] == "ro"
    assert report["read_evidence"]["baseline"]["sha256_before"] == report["read_evidence"]["baseline"]["sha256_after"]
    assert len(read_snapshot(actual)["quotes"]) == 1
    assert before == (sha(baseline), sha(actual))


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        read_snapshot(missing)
    assert not missing.exists()


def test_cli_rejects_overwriting_database_with_report(tmp_path):
    baseline, actual = tmp_path / "gold.sqlite3", tmp_path / "actual.sqlite3"
    create_database(baseline, catalog()); create_database(actual, catalog())
    before = sha(actual)
    with pytest.raises(SystemExit):
        main(["--baseline", str(baseline), "--actual", str(actual), "--output", str(actual)])
    assert sha(actual) == before


@pytest.mark.parametrize("field,left,right", [
    ("supplier_model_id", "raw.a", "rawa"),
    ("tags", ["cn+premium"], ["cnpremium"]),
    ("effective_date", "2026-10-01", "2025-01-01"),
    ("dimension", "token", "time"),
    ("direction", "input", "output"),
])
def test_price_identity_fields_do_not_silently_collapse(field, left, right):
    baseline, actual = catalog(), catalog()
    baseline["quotes"][0][field] = left
    actual["quotes"][0][field] = right
    assert not compare_snapshots(baseline, actual)["pass"]


def test_retired_physical_metadata_is_ignored_in_read_only_legacy_snapshots():
    baseline, actual = catalog(), catalog()
    second = {"model_id": "model-b", "vendor": "Example Labs", "model_name": "Model B",
              "physical_key": "physical-b", "aliases": []}
    baseline["models"].append(deepcopy(second))
    actual["models"].append(deepcopy(second))
    for row in actual["models"]:
        row["physical_key"] = "same-new-physical"
    result = compare_snapshots(baseline, actual)
    assert result["pass"], result


def test_identical_quote_multisets_preserve_multiplicity_and_pass():
    baseline, actual = catalog(), catalog()
    baseline["quotes"].append(deepcopy(baseline["quotes"][0]))
    actual["quotes"].append(deepcopy(actual["quotes"][0]))
    result = compare_snapshots(baseline, actual)
    assert result["pass"], result
    assert result["matched"]["quotes"] == 2


def test_official_price_unique_key_duplicate_is_invalid():
    baseline, actual = catalog(), catalog()
    actual["official_prices"].append(deepcopy(actual["official_prices"][0]))
    result = compare_snapshots(baseline, actual)
    assert not result["pass"] and "duplicate_price_key" in codes(result)


def test_large_prices_do_not_round_away_material_difference():
    baseline, actual = catalog(), catalog()
    baseline["quotes"][0]["amount"] = "1000000000000000000000000000000"
    actual["quotes"][0]["amount"] = "1000000000000000000000000000100"
    result = compare_snapshots(baseline, actual)
    assert not result["pass"]
    assert result["issues"][-1]["evidence"][0]["code"] == "amount_mismatch"


def test_multiset_uses_maximum_not_greedy_tolerance_matching():
    baseline, actual = catalog(), catalog()
    for snapshot, values in [(baseline, ["1", "1.9"]), (actual, ["1.5", "0.5"])]:
        first = snapshot["quotes"][0]
        first.update(amount=values[0], unit_quantity="1", discount_rate=None)
        other = deepcopy(first); other["amount"] = values[1]; snapshot["quotes"].append(other)
    result = compare_snapshots(baseline, actual, amount_tolerance="1")
    assert result["pass"], result
    assert result["matched"]["quotes"] == 2


def test_official_uniqueness_ignores_effective_date_invariant():
    baseline, actual = catalog(), catalog()
    for snapshot in (baseline, actual):
        second = deepcopy(snapshot["official_prices"][0])
        second["effective_date"] = "2025-01-01"
        snapshot["official_prices"].append(second)
    result = compare_snapshots(baseline, actual)
    assert not result["pass"] and "duplicate_price_key" in codes(result)


@pytest.mark.parametrize("field,value", [("discount_rate", "-1")])
def test_identical_invalid_pricing_rows_do_not_pass(field, value):
    baseline, actual = catalog(), catalog()
    for snapshot in (baseline, actual):
        snapshot["quotes"][0][field] = value
    assert not compare_snapshots(baseline, actual)["pass"]


def canonical_catalog(unit_id):
    value = deepcopy(catalog())
    for row in value["official_prices"] + value["quotes"]:
        for key in ("currency", "unit", "unit_quantity"):
            row.pop(key, None)
        row["pricing_unit_id"] = unit_id
    return value


def test_direct_compare_expands_canonical_unit_ids_and_distinguishes_currency():
    usd = canonical_catalog("usd_mtoken")
    assert compare_snapshots(usd, deepcopy(usd))["pass"]
    mismatch = compare_snapshots(usd, canonical_catalog("hkd_mtoken"))
    assert not mismatch["pass"]
    assert any(e.get("code") == "original_currency_mismatch"
               for issue in mismatch["issues"] for e in issue.get("evidence", []))


def test_read_snapshot_expands_canonical_unit_id_without_writing(tmp_path):
    path = tmp_path / "canonical.sqlite3"
    create_database(path, canonical_catalog("usd_mtoken"))
    before = sha(path)
    loaded = read_snapshot(path)
    assert sha(path) == before
    for row in loaded["official_prices"] + loaded["quotes"]:
        assert (row["pricing_unit_id"], row["currency"], row["unit"], row["unit_quantity"]) == (
            "usd_mtoken", "USD", "token", "1000000")


def test_model_punctuation_requires_explicit_identity_map():
    baseline, actual = catalog(), catalog()
    baseline["models"][0].update(model_id="gpt.4", model_name="GPT.4", aliases=[])
    actual["models"][0].update(model_id="gpt4", model_name="GPT4", aliases=[])
    for row in baseline["official_prices"] + baseline["quotes"]:
        row["model_id"] = "gpt.4"
    for row in actual["official_prices"] + actual["quotes"]:
        row["model_id"] = "gpt4"
    assert "unknown_model" in codes(compare_snapshots(baseline, actual))
    assert compare_snapshots(baseline, actual, identity_map={"gpt4": "gpt.4"})["pass"]
