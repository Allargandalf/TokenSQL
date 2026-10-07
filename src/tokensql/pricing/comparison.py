"""Strict comparison for already-normalized prices."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .normalization import NormalizedPrice, PriceKind, canonical_conditions


@dataclass(frozen=True)
class ComparisonDifference:
    field: str
    left: object
    right: object


@dataclass(frozen=True)
class ComparisonResult:
    comparable: bool
    left_amount: Decimal | None
    right_amount: Decimal | None
    delta: Decimal | None = None
    ratio: Decimal | None = None
    cheaper: str | None = None
    reasons: tuple[str, ...] = ()
    differences: tuple[ComparisonDifference, ...] = ()


def compare_prices(
    left: NormalizedPrice,
    right: NormalizedPrice,
) -> ComparisonResult:
    """Compare only exact, verified comparison identities.

    Delta is right minus left. Ratio is right divided by left. A discount-derived
    price can only be compared with another discount-derived price.
    """

    reasons: list[str] = []
    differences: list[ComparisonDifference] = []

    if not left.is_comparable:
        reasons.append("left normalization is not verified or approved")
    if not right.is_comparable:
        reasons.append("right normalization is not verified or approved")
    if left.kind is not right.kind:
        reasons.append("discount-derived and absolute prices cannot be compared directly")
        differences.append(
            ComparisonDifference("kind", left.kind.value, right.kind.value)
        )

    if left.comparison_key != right.comparison_key:
        reasons.append("comparison_key differs")
        differences.extend(_identity_differences(left, right))

    if reasons:
        return ComparisonResult(
            comparable=False,
            left_amount=left.normalized_amount,
            right_amount=right.normalized_amount,
            reasons=tuple(dict.fromkeys(reasons)),
            differences=tuple(differences),
        )

    assert left.normalized_amount is not None
    assert right.normalized_amount is not None
    delta = right.normalized_amount - left.normalized_amount
    ratio = None if left.normalized_amount == 0 else right.normalized_amount / left.normalized_amount
    cheaper = "equal"
    if left.normalized_amount < right.normalized_amount:
        cheaper = "left"
    elif right.normalized_amount < left.normalized_amount:
        cheaper = "right"

    return ComparisonResult(
        comparable=True,
        left_amount=left.normalized_amount,
        right_amount=right.normalized_amount,
        delta=delta,
        ratio=ratio,
        cheaper=cheaper,
    )


def _identity_differences(
    left: NormalizedPrice,
    right: NormalizedPrice,
) -> list[ComparisonDifference]:
    fields = (
        ("metric", left.metric, right.metric),
        ("direction", left.direction, right.direction),
        (
            "normalized_currency",
            left.normalized_currency,
            right.normalized_currency,
        ),
        (
            "normalized_unit_name",
            left.normalized_unit_name,
            right.normalized_unit_name,
        ),
        (
            "normalized_unit_quantity",
            left.normalized_unit_quantity,
            right.normalized_unit_quantity,
        ),
        (
            "conditions",
            canonical_conditions(left.conditions),
            canonical_conditions(right.conditions),
        ),
    )
    return [
        ComparisonDifference(field, left_value, right_value)
        for field, left_value, right_value in fields
        if left_value != right_value
    ]
