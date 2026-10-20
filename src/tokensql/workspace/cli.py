"""Operator CLI for exact pricing, catalog maintenance, previews and exports."""
from __future__ import annotations

import argparse
import csv
from io import BytesIO
import json
from pathlib import Path
import sys
from typing import Any

from openpyxl import Workbook
from .data_routes import EXPORT_COLUMNS, _export_values
from .schemas import PricingBundle, QuoteCreate, QuoteResponse
from .store import WorkspaceStore
from .units import pricing_units_payload


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str))


def _load_bundle(path: Path) -> PricingBundle:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        value = {"create": value}
    elif isinstance(value, dict) and set(value) == {"items"}:
        value = {"create": value["items"]}
    return PricingBundle.model_validate(value)


def _schema() -> dict[str, Any]:
    return {
        "quote": QuoteCreate.model_json_schema(),
        "bundle": PricingBundle.model_json_schema(),
        "apply": {"models": [], "official_prices": [], "create": [], "update": [], "delete": []},
        "notes": [
            "Only model_id is required; missing models are created automatically. No physical identity field.",
            "New rows default omitted currency to CNY; explicit null remains unknown and unit IDs determine their own currency.",
            "Use exact Decimal strings. A complete pricing unit must be one of the 12 pricing_unit_id values.",
            "currency/unit/unit_quantity are strict compatibility input only and may not conflict with pricing_unit_id.",
            "Preserve independent source amount and discount_rate, including discount-only records.",
            "Partial known currency/unit/quantity context is preserved; Tokens never implies 1K or 1M.",
            "All official-price, discount, currency and unit conversions are frontend display/export only.",
            "Missing rates or token equivalences are issues, never guessed estimates.",
            "model_id is the only model identity; billing items never become model IDs.",
            "Delete operations physically remove selected quote rows and retain before snapshots in the audit log.",
            "discount_rate is the paid-price multiplier, and may exceed one.",
            "Official records allow nullable business metadata; provide source/source_ref evidence whenever known.",
            "Conversion requires original_currency/original_amount/exchange_rate/rate_source.",
            "Preview evaluates only in memory. Apply is atomic and --import-id makes retries idempotent.",
        ],
    }


def _write_export(path: Path, items: list[QuoteResponse]) -> None:
    rows = [_export_values(item) for item in items]
    if path.suffix.lower() == ".xlsx":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Quotes")
        sheet.append(EXPORT_COLUMNS)
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        path.write_bytes(output.getvalue())
    else:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain the normalized TokenSQL pricing workspace.")
    parser.add_argument("--db", required=True, help="SQLite path, or :memory: for isolated checks")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print exact quote and bundle JSON schemas")
    commands.add_parser("pricing-units", help="print the closed 12-item pricing-unit dictionary")
    commands.add_parser("pricing-context", help="read versioned FX and token-equivalence context")
    context = commands.add_parser("context-set", help="replace context from a JSON object, retaining history")
    context.add_argument("input", type=Path)
    context.add_argument("--actor", default="manual/cli")
    commands.add_parser("catalog", help="read catalog, channels, quotes and preserved legacy notice")
    for command in ("apply", "preview", "catalog-load"):
        operation = commands.add_parser(command, help="validate/apply a normalized pricing bundle")
        operation.add_argument("input", type=Path)
        operation.add_argument("--actor", default="codex/catalog" if command == "catalog-load" else "manual/cli")
        operation.add_argument("--import-id")
    for command in ("list", "export"):
        operation = commands.add_parser(command)
        for field in ("q", "model-id", "model-name", "manufacturer", "vendor", "provider", "supplier",
                      "currency", "pricing-unit-id", "metric", "billing-item", "dimension", "tag", "supplier-model-id"):
            operation.add_argument("--" + field)
        if command == "list":
            operation.add_argument("--limit", type=int, default=500)
            operation.add_argument("--offset", type=int, default=0)
        else:
            operation.add_argument("output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        _print(_schema())
        return 0
    if args.command == "pricing-units":
        _print(pricing_units_payload())
        return 0
    store = WorkspaceStore(args.db)
    try:
        if args.command == "pricing-context":
            _print(store.pricing_context())
        elif args.command == "context-set":
            value = json.loads(args.input.read_text(encoding="utf-8"))
            value.setdefault("actor", args.actor)
            _print(store.set_pricing_context(**value))
        elif args.command == "catalog":
            _print(store.catalog_snapshot())
        elif args.command in ("apply", "preview", "catalog-load"):
            bundle = _load_bundle(args.input)
            if args.command == "catalog-load" and (bundle.create or bundle.update or bundle.delete):
                raise ValueError("catalog-load accepts only models and official_prices")
            result = store.preview_bundle(bundle) if args.command == "preview" else store.apply_bundle(
                bundle, args.actor, args.import_id)
            _print(result)
        elif args.command in ("list", "export"):
            filters = {key: getattr(args, key) for key in
                       ("q", "model_id", "model_name", "manufacturer", "vendor", "provider", "supplier",
                        "currency", "pricing_unit_id", "metric", "billing_item", "dimension", "tag", "supplier_model_id")}
            items, total = store.list_quotes(**filters,
                limit=args.limit if args.command == "list" else 100000,
                offset=args.offset if args.command == "list" else 0)
            if args.command == "list":
                _print({"items": [v.model_dump(mode="json") for v in items], "total": total,
                        "filter_options": store.filter_options()})
            else:
                _write_export(args.output, items)
                _print({"exported": len(items), "path": str(args.output)})
        return 0
    except Exception as error:
        _print({"error": " ".join(str(error).split())[:1500]})
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
