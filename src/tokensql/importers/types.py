"""Dependency-free domain types for Excel import inspection.

The importer deliberately returns immutable dataclasses rather than ORM or
Pydantic objects. A service layer can validate these records and map them to a
staged_quote table without allowing the parser to choose database keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple


class PriceKind(str, Enum):
    DISCOUNT = "discount"
    CNY_PER_SECOND = "CNY/second"
    UNKNOWN_CURRENCY_PER_SECOND = "unknown_currency/second"
    CNY_PER_MILLION_TOKENS = "CNY/million_tokens"
    CNY_PER_TOKEN = "CNY/token"
    CNY_PER_REQUEST = "CNY/request"
    CNY_PER_IMAGE = "CNY/image"
    CNY_PER_TEN_THOUSAND_CHARACTERS = "CNY/ten_thousand_characters"
    CNY_PER_VOICE = "CNY/voice"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class FormulaKind(str, Enum):
    NORMAL = "normal"
    SHARED = "shared"
    ARRAY = "array"


@dataclass(frozen=True)
class ParserProfile:
    """Explicit parsing choices; nothing here is inferred from workbook prose."""

    header_row: int = 1
    discount_scale: Optional[Decimal] = None
    price_headers: Tuple[str, ...] = (
        "报价或折扣（最低）",
        "折扣",
        "蒸馏折扣",
        "修正折扣",
        "修正蒸馏折扣",
    )

    def __post_init__(self) -> None:
        if self.header_row < 1:
            raise ValueError("header_row must be >= 1")
        if self.discount_scale is not None:
            scale = Decimal(str(self.discount_scale))
            if scale != Decimal("10"):
                raise ValueError("only the explicitly supported discount_scale=10 is valid")
            object.__setattr__(self, "discount_scale", scale)


@dataclass(frozen=True)
class ParsedPrice:
    raw_value: Any
    raw_text: str
    kind: PriceKind
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    unit: Optional[str] = None
    discount_multiplier: Optional[Decimal] = None
    condition_text: Optional[str] = None
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnHeader:
    column_index: int
    column_letter: str
    coordinate: str
    raw_value: Any
    normalized_value: str
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CellRecord:
    coordinate: str
    column_index: int
    row_index: int
    raw_value: Any
    cached_value: Any = None
    formula_text: Optional[str] = None
    formula_kind: Optional[FormulaKind] = None
    formula_range: Optional[str] = None
    shared_index: Optional[str] = None
    merge_range: Optional[str] = None
    source_coordinate: Optional[str] = None
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RowRecord:
    row_index: int
    cells: Tuple[CellRecord, ...]
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()

    def cell_at_column(self, column_index: int) -> CellRecord:
        for cell in self.cells:
            if cell.column_index == column_index:
                return cell
        raise KeyError(column_index)


@dataclass(frozen=True)
class SheetProfile:
    title: str
    sheet_index: int
    max_row: int
    max_column: int
    header_row: int
    data_row_count: int
    merge_ranges: Tuple[str, ...]
    headers: Tuple[ColumnHeader, ...]
    rows: Tuple[RowRecord, ...]
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()

    def cell(self, coordinate: str) -> CellRecord:
        for row in self.rows:
            for cell in row.cells:
                if cell.coordinate == coordinate:
                    return cell
        raise KeyError(coordinate)

    def header_at_column(self, column_index: int) -> ColumnHeader:
        for header in self.headers:
            if header.column_index == column_index:
                return header
        raise KeyError(column_index)


@dataclass(frozen=True)
class WorkbookProfile:
    source_path: Path
    source_name: str
    sha256: str
    byte_size: int
    sheets: Tuple[SheetProfile, ...]
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StagedQuoteRecord:
    """A traceable candidate record, intentionally without any database IDs."""

    workbook_sha256: str
    sheet_name: str
    source_row: int
    source_coordinate: str
    header: ColumnHeader
    cell: CellRecord
    parsed_price: ParsedPrice
    needs_review: bool = False
    review_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExcelParseResult:
    workbook: WorkbookProfile
    staged_quotes: Tuple[StagedQuoteRecord, ...]
