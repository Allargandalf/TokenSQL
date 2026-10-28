"""Tax status is a pricing condition, not a supplier label or inferred currency."""
import pytest
from pydantic import ValidationError
from tokensql.workspace.pricing_conditions import (
    PricingConditions, conditions_match, match_official_prices, scenario_key,
)


def test_tax_status_is_typed_and_identity_separates_tax_basis():
    for status in ("included", "excluded", "unknown"):
        assert PricingConditions(tax_status=status).tax_status == status
    with pytest.raises(ValidationError):
        PricingConditions(tax_status="probably included")
    values = [scenario_key({"billing_metric": "input", "pricing_conditions": {"tax_status": x}})
              for x in ("included", "excluded", "unknown")]
    assert len(set(values)) == 3


@pytest.mark.parametrize("wanted,actual,expected", [
    ("included", "included", (True, False)),
    ("included", "excluded", (False, False)),
    ("included", None, (False, True)),
    ("included", "unknown", (False, True)),
    ("unknown", "included", (False, True)),
    ("unknown", "unknown", (False, True)),
])
def test_unknown_tax_is_not_confirmed_equivalence(wanted, actual, expected):
    other = {"tax_status": actual} if actual else {}
    assert conditions_match({"tax_status": wanted}, other) == expected


def test_exact_tax_scope_matches_only_corresponding_official():
    prices = [{"model_id": "m", "official_price_id": status, "billing_metric": "input",
               "pricing_conditions": {"tax_status": status}} for status in ("included", "excluded")]
    matched, result = match_official_prices(
        {"model_id": "m", "discount_scope": "model", "pricing_conditions": {"tax_status": "included"}},
        prices,
    )
    assert result["status"] == "matched"
    assert [p["official_price_id"] for p in matched] == ["included"]
