from datetime import date
from decimal import Decimal

import pytest

from tokensql.pricing import (
    DiscountQuote,
    FXQuote,
    PriceQuote,
    apply_discount,
    compare_prices,
    normalize_price,
)


def quote(**changes):
    values = {
        "amount": Decimal("0.01"),
        "currency": "USD",
        "unit_name": "token",
        "unit_quantity": Decimal("1000"),
        "metric": "token",
        "direction": "input",
        "conditions": {"cache": "miss", "tier": {"start": Decimal("0")}},
        "verification_status": "verified",
    }
    values.update(changes)
    return PriceQuote(**values)


def verified_fx(**changes):
    values = {
        "base_currency": "USD",
        "quote_currency": "CNY",
        "rate": Decimal("7.2"),
        "rate_date": date(2026, 9, 23),
        "source": "https://official.example/fx",
        "verification_status": "verified",
    }
    values.update(changes)
    return FXQuote(**values)


def test_token_and_fx_normalize_to_cny_per_million():
    result = normalize_price(quote(), verified_fx())

    assert result.is_comparable
    assert result.normalized_amount == Decimal("72")
    assert result.normalized_currency == "CNY"
    assert result.normalized_unit_name == "token"
    assert result.normalized_unit_quantity == Decimal("1000000")
    assert result.raw_amount == Decimal("0.01")
    assert result.raw_currency == "USD"
    assert result.raw_unit_quantity == Decimal("1000")
    assert result.fx == verified_fx()


@pytest.mark.parametrize(
    "fx, reason",
    [
        (None, "required"),
        (verified_fx(rate_date=None), "source date"),
        (verified_fx(source=None), "no source"),
        (verified_fx(verification_status="pending"), "not verified"),
    ],
)
def test_foreign_currency_requires_verified_dated_sourced_fx(fx, reason):
    result = normalize_price(quote(), fx)

    assert not result.is_comparable
    assert result.normalized_amount is None
    assert reason in result.pending_reasons[0]


def test_non_token_units_remain_their_own_standard_unit():
    result = normalize_price(
        quote(
            amount=Decimal("12"),
            currency="CNY",
            unit_name="秒",
            unit_quantity=Decimal("60"),
            metric="audio",
            direction=None,
            conditions={},
        )
    )

    assert result.normalized_amount == Decimal("0.2")
    assert result.normalized_unit_name == "second"
    assert result.normalized_unit_quantity == Decimal("1")


def test_condition_order_is_canonical_but_semantic_difference_changes_key():
    first = normalize_price(quote(conditions={"batch": True, "cache": "hit"}), verified_fx())
    reordered = normalize_price(
        quote(conditions={"cache": "hit", "batch": True}),
        verified_fx(),
    )
    changed = normalize_price(
        quote(conditions={"cache": "miss", "batch": True}),
        verified_fx(),
    )

    assert first.comparison_key == reordered.comparison_key
    assert first.comparison_key != changed.comparison_key
    comparison = compare_prices(first, changed)
    assert not comparison.comparable
    assert any(item.field == "conditions" for item in comparison.differences)


def test_metric_and_direction_are_comparison_identity():
    input_price = normalize_price(quote(), verified_fx())
    output_price = normalize_price(quote(direction="output"), verified_fx())

    result = compare_prices(input_price, output_price)

    assert not result.comparable
    assert {item.field for item in result.differences} >= {"direction"}


def test_discount_requires_verified_same_key_base():
    pending_base = normalize_price(
        quote(currency="CNY", verification_status="pending"),
    )
    discount = DiscountQuote(
        multiplier=Decimal("0.8"),
        metric="token",
        direction="input",
        conditions={"cache": "miss", "tier": {"start": Decimal("0")}},
        verification_status="verified",
    )

    result = apply_discount(pending_base, discount)

    assert not result.is_comparable
    assert result.normalized_amount is None
    assert "verified or approved" in " ".join(result.pending_reasons)


def test_discount_condition_must_match_base_and_not_compare_with_absolute():
    base = normalize_price(quote(), verified_fx())
    mismatch = DiscountQuote(
        multiplier=Decimal("0.8"),
        metric="token",
        direction="input",
        conditions={"cache": "hit"},
        verification_status="verified",
    )
    assert not apply_discount(base, mismatch).is_comparable

    matching = DiscountQuote(
        multiplier=Decimal("0.8"),
        metric="token",
        direction="input",
        conditions={"tier": {"start": Decimal("0")}, "cache": "miss"},
        verification_status="verified",
    )
    discounted = apply_discount(base, matching)
    assert discounted.normalized_amount == Decimal("57.6")

    comparison = compare_prices(base, discounted)
    assert not comparison.comparable
    assert "cannot be compared" in " ".join(comparison.reasons)


def test_float_inputs_are_rejected_to_keep_decimal_pipeline():
    with pytest.raises(TypeError):
        quote(amount=0.01)
    with pytest.raises(TypeError):
        quote(conditions={"threshold": 0.5})
