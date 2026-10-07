"""Public price normalization and comparison service API."""

from .comparison import (
    ComparisonDifference,
    ComparisonResult,
    compare_prices,
)
from .normalization import (
    COMPARABLE_STATUSES,
    DiscountQuote,
    FXQuote,
    NormalizationStatus,
    NormalizedPrice,
    PriceKind,
    PriceQuote,
    apply_discount,
    approve,
    build_comparison_key,
    canonical_conditions,
    normalize_price,
)

__all__ = [
    "COMPARABLE_STATUSES",
    "ComparisonDifference",
    "ComparisonResult",
    "DiscountQuote",
    "FXQuote",
    "NormalizationStatus",
    "NormalizedPrice",
    "PriceKind",
    "PriceQuote",
    "apply_discount",
    "approve",
    "build_comparison_key",
    "canonical_conditions",
    "compare_prices",
    "normalize_price",
]
