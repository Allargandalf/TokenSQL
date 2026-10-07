from __future__ import annotations

from collections import Counter
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tokensql.importers import (  # noqa: E402
    FormulaKind,
    ParserProfile,
    PriceKind,
    normalize_header,
    parse_excel,
    parse_price,
)


HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None


class HeaderAndPriceTests(unittest.TestCase):
    def test_header_normalization_preserves_raw_separately(self) -> None:
        raw = "  ＡＰＩ\u3000 Price\t Header  "
        self.assertEqual(normalize_header(raw), "api price header")
        self.assertEqual(raw, "  ＡＰＩ\u3000 Price\t Header  ")

    def test_strict_price_categories_use_decimal(self) -> None:
        cases = (
            ("3.1折", PriceKind.DISCOUNT, Decimal("3.1"), "discount_multiplier"),
            ("0.4948元/秒", PriceKind.CNY_PER_SECOND, Decimal("0.4948"), "second"),
            ("0.0082/秒", PriceKind.UNKNOWN_CURRENCY_PER_SECOND, Decimal("0.0082"), "second"),
            ("31.7526元/百万tokens", PriceKind.CNY_PER_MILLION_TOKENS, Decimal("31.7526"), "million_tokens"),
            ("0.0206元/token", PriceKind.CNY_PER_TOKEN, Decimal("0.0206"), "token"),
            ("1.5464元/次", PriceKind.CNY_PER_REQUEST, Decimal("1.5464"), "request"),
            ("0.1546元/张", PriceKind.CNY_PER_IMAGE, Decimal("0.1546"), "image"),
            ("2.5773元/万字符", PriceKind.CNY_PER_TEN_THOUSAND_CHARACTERS, Decimal("2.5773"), "ten_thousand_characters"),
            ("4.1237元/个音色", PriceKind.CNY_PER_VOICE, Decimal("4.1237"), "voice"),
        )
        for raw, kind, amount, unit in cases:
            with self.subTest(raw=raw):
                parsed = parse_price(raw)
                self.assertEqual(parsed.kind, kind)
                self.assertEqual(parsed.amount, amount)
                self.assertIsInstance(parsed.amount, Decimal)
                self.assertEqual(parsed.unit, unit)

    def test_missing_currency_requires_review(self) -> None:
        parsed = parse_price("0.0082/秒")
        self.assertTrue(parsed.needs_review)
        self.assertIn("currency_missing", parsed.review_reasons)
        self.assertIsNone(parsed.currency)

    def test_conditional_price_does_not_extract_amount(self) -> None:
        parsed = parse_price("首图免费，超出部分0.0206元/张")
        self.assertEqual(parsed.kind, PriceKind.CONDITIONAL)
        self.assertIsNone(parsed.amount)
        self.assertEqual(parsed.currency, "CNY")
        self.assertEqual(parsed.unit, "image")
        self.assertTrue(parsed.needs_review)

    def test_bare_discount_requires_explicit_scale_ten(self) -> None:
        default = parse_price("6.4")
        self.assertEqual(default.kind, PriceKind.UNKNOWN)
        self.assertTrue(default.needs_review)

        explicit = parse_price("6.4", ParserProfile(discount_scale=10))
        self.assertEqual(explicit.kind, PriceKind.DISCOUNT)
        self.assertEqual(explicit.discount_multiplier, Decimal("0.64"))

        with self.assertRaises(ValueError):
            ParserProfile(discount_scale=100)


@unittest.skipUnless(HAS_OPENPYXL, "openpyxl is not installed on this server")
class RealWorkbookTests(unittest.TestCase):
    fixture_dir = PROJECT_ROOT / "test"

    def test_real_sheet_row_counts_and_sha256(self) -> None:
        expected = {
            "token折扣(2).xlsx": 26,
            "token折扣.xlsx": 26,
            "token报价 (1).xlsx": 121,
        }
        for filename, row_count in expected.items():
            with self.subTest(filename=filename):
                result = parse_excel(self.fixture_dir / filename)
                self.assertEqual(len(result.workbook.sha256), 64)
                self.assertEqual(len(result.workbook.sheets), 1)
                self.assertEqual(result.workbook.sheets[0].data_row_count, row_count)

    def test_header_raw_and_normalized_values_coexist(self) -> None:
        result = parse_excel(self.fixture_dir / "token折扣.xlsx")
        header = result.workbook.sheets[0].header_at_column(2)
        self.assertEqual(header.raw_value, " 模型名称  ")
        self.assertEqual(header.normalized_value, "模型名称")

    def test_merges_are_filled_only_with_explicit_provenance(self) -> None:
        result = parse_excel(self.fixture_dir / "token报价 (1).xlsx")
        sheet = result.workbook.sheets[0]
        self.assertEqual(len(sheet.merge_ranges), 20)

        merged = sheet.cell("B3")
        self.assertEqual(merged.raw_value, sheet.cell("B2").raw_value)
        self.assertEqual(merged.merge_range, "B2:B10")
        self.assertEqual(merged.source_coordinate, "B2")

        unmerged = sheet.cell("C3")
        self.assertIsNone(unmerged.merge_range)
        self.assertEqual(unmerged.source_coordinate, "C3")

    def test_empty_e_header_and_array_formulas_are_reviewed(self) -> None:
        result = parse_excel(self.fixture_dir / "token报价 (1).xlsx")
        sheet = result.workbook.sheets[0]
        header = sheet.header_at_column(5)
        self.assertIsNone(header.raw_value)
        self.assertEqual(header.normalized_value, "")
        self.assertTrue(header.needs_review)
        self.assertIn("empty_header:E", sheet.review_reasons)

        formulas = [sheet.cell("E" + str(row)) for row in range(2, 122)]
        self.assertEqual(len(formulas), 120)
        self.assertTrue(all(cell.formula_kind == FormulaKind.ARRAY for cell in formulas))
        self.assertTrue(all(cell.formula_text for cell in formulas))
        self.assertTrue(all(cell.cached_value is not None for cell in formulas))
        self.assertTrue(all(cell.needs_review for cell in formulas))
        self.assertTrue(all("missing_header" in cell.review_reasons for cell in formulas))

    def test_normal_shared_and_array_formula_metadata(self) -> None:
        discount = parse_excel(
            self.fixture_dir / "token折扣.xlsx",
            ParserProfile(discount_scale=10),
        ).workbook.sheets[0]
        self.assertEqual(discount.cell("C2").formula_kind, FormulaKind.NORMAL)
        self.assertEqual(discount.cell("C3").formula_kind, FormulaKind.SHARED)
        self.assertEqual(discount.cell("C3").formula_range, "C3:C12")
        self.assertEqual(discount.cell("C3").shared_index, "0")
        self.assertEqual(discount.cell("C4").formula_kind, FormulaKind.SHARED)
        self.assertTrue(discount.cell("C4").formula_text)
        self.assertIsNotNone(discount.cell("C4").cached_value)

        quote = parse_excel(self.fixture_dir / "token报价 (1).xlsx").workbook.sheets[0]
        self.assertEqual(quote.cell("E2").formula_kind, FormulaKind.ARRAY)

    def test_real_quote_classification_counts(self) -> None:
        result = parse_excel(self.fixture_dir / "token报价 (1).xlsx")
        counts = Counter(item.parsed_price.kind for item in result.staged_quotes)
        self.assertEqual(counts[PriceKind.DISCOUNT], 31)
        self.assertEqual(counts[PriceKind.CNY_PER_SECOND], 66)
        self.assertEqual(counts[PriceKind.UNKNOWN_CURRENCY_PER_SECOND], 9)
        self.assertEqual(counts[PriceKind.CNY_PER_MILLION_TOKENS], 4)
        self.assertEqual(counts[PriceKind.CNY_PER_TOKEN], 2)
        self.assertEqual(counts[PriceKind.CNY_PER_REQUEST], 3)
        self.assertEqual(counts[PriceKind.CNY_PER_IMAGE], 3)
        self.assertEqual(counts[PriceKind.CNY_PER_TEN_THOUSAND_CHARACTERS], 1)
        self.assertEqual(counts[PriceKind.CNY_PER_VOICE], 1)
        self.assertEqual(counts[PriceKind.CONDITIONAL], 1)
        self.assertEqual(len(result.staged_quotes), 121)

        image_dimension = sum(
            item.parsed_price.unit == "image" for item in result.staged_quotes
        )
        self.assertEqual(image_dimension, 4)

    def test_discount_workbooks_need_explicit_profile(self) -> None:
        profile = ParserProfile(discount_scale=10)
        for filename in ("token折扣.xlsx", "token折扣(2).xlsx"):
            with self.subTest(filename=filename):
                result = parse_excel(self.fixture_dir / filename, profile)
                self.assertEqual(len(result.staged_quotes), 52)
                self.assertTrue(
                    all(
                        item.parsed_price.kind == PriceKind.DISCOUNT
                        for item in result.staged_quotes
                    )
                )


if __name__ == "__main__":
    unittest.main()
