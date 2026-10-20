from copy import deepcopy

import pytest

from tokensql.workspace.compare import compare_snapshots


def partial_snapshot():
    return {
        "models": [
            {"model_id": "mimo-v2.5-pro", "vendor": None},
            {"model_id": "hy4-preview", "vendor": None},
        ],
        "official_prices": [],
        "quotes": [
            {"model_id": "mimo-v2.5-pro", "discount_rate": "0.8", "currency": "CNY", "unit": "token"},
            {"model_id": "hy4-preview"},
        ],
    }


def test_same_nullable_raw_records_are_comparable_without_false_identity_split():
    value = partial_snapshot()
    result = compare_snapshots(value, deepcopy(value))
    assert result["pass"], result
    assert result["matched"] == {"models": 2, "official_prices": 0, "quotes": 2}


@pytest.mark.parametrize("change", [
    {"discount_rate": "0.7"},
    {"unit_quantity": "1000"},
    {"currency": "USD"},
    {"amount": "0"},
])
def test_partial_values_cannot_be_filled_or_changed_silently(change):
    value = partial_snapshot()
    actual = deepcopy(value)
    actual["quotes"][0].update(change)
    result = compare_snapshots(value, actual)
    assert not result["pass"]
    assert result["missing"] and result["unexpected"]


def test_exact_model_id_matches_when_vendor_is_unknown_on_one_side():
    value = partial_snapshot()
    value["models"][0]["vendor"] = "Verified maker"
    actual = deepcopy(value)
    actual["models"][0]["vendor"] = None
    assert compare_snapshots(value, actual)["pass"]


def test_known_conflicting_vendor_does_not_pass():
    value = partial_snapshot()
    value["models"][0]["vendor"] = "Maker A"
    actual = deepcopy(value)
    actual["models"][0]["vendor"] = "Maker B"
    result = compare_snapshots(value, actual)
    assert not result["pass"]
    assert any(item["code"] == "model_vendor_mismatch" for item in result["issues"])


def test_null_billing_official_stub_is_comparable():
    value = partial_snapshot()
    value["official_prices"] = [{"model_id": "hy4-preview", "billing_item": None, "amount": None}]
    assert compare_snapshots(value, deepcopy(value))["pass"]


def test_unknown_quantity_does_not_generate_estimated_amount():
    value = partial_snapshot()
    value["quotes"][0] = {"model_id": "mimo-v2.5-pro", "amount": "1",
                          "currency": "CNY", "unit": "second", "unit_quantity": None}
    context = {"revision": 2, "token_equivalences": [
        {"model_id": "mimo-v2.5-pro", "billing_item": None, "unit": "second",
         "tokens_per_unit": "100", "source": "synthetic test"}]}
    result = compare_snapshots(value, deepcopy(value), pricing_context=context)
    assert result["pass"], result
    assert result["estimates"] == []
