"""Command-line access used by OpenCode and workspace operators."""
from __future__ import annotations

import argparse
import csv
from io import BytesIO
import json
from pathlib import Path
import sys
from typing import Any

from openpyxl import Workbook

from tokensql.workspace.schemas import QuoteCreate, QuoteResponse
from tokensql.workspace.store import WorkspaceStore

EXPORT_COLUMNS = (
    "id", "model_name", "model_version", "manufacturer", "provider",
    "provider_type", "amount", "currency", "unit", "unit_quantity", "metric",
    "dimension", "direction", "component_type", "discount_rate", "conditions",
    "effective_date", "source", "source_ref", "note", "status", "revision",
    "created_at", "updated_at",
)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_default))


def _response_dict(item: QuoteResponse) -> dict[str, Any]:
    return item.model_dump(mode="json")


def _load_apply(path: Path) -> tuple[
    list[QuoteCreate],
    list[tuple[int, int, dict[str, Any]]],
    list[tuple[int, int]],
]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        payload = {"create": payload}
    if not isinstance(payload, dict):
        raise ValueError("apply input must be a JSON object or quote array")
    if "items" in payload:
        if set(payload) - {"items"}:
            raise ValueError("items shorthand cannot be mixed with apply operations")
        payload = {"create": payload["items"]}
    unknown = set(payload) - {"create", "update", "delete"}
    if unknown:
        raise ValueError(f"unknown apply sections: {', '.join(sorted(unknown))}")

    creates_raw = payload.get("create", [])
    updates_raw = payload.get("update", [])
    deletes_raw = payload.get("delete", [])
    if not all(isinstance(value, list) for value in (creates_raw, updates_raw, deletes_raw)):
        raise ValueError("create, update, and delete must be arrays")
    creates = [QuoteCreate.model_validate(value) for value in creates_raw]

    updates: list[tuple[int, int, dict[str, Any]]] = []
    for value in updates_raw:
        if not isinstance(value, dict) or set(value) != {
            "id", "expected_revision", "changes"
        }:
            raise ValueError(
                "each update requires exactly id, expected_revision, and changes"
            )
        if not isinstance(value["changes"], dict):
            raise ValueError("update changes must be an object")
        updates.append(
            (int(value["id"]), int(value["expected_revision"]), value["changes"])
        )

    deletes: list[tuple[int, int]] = []
    for value in deletes_raw:
        if not isinstance(value, dict) or set(value) != {"id", "expected_revision"}:
            raise ValueError("each delete requires exactly id and expected_revision")
        deletes.append((int(value["id"]), int(value["expected_revision"])))
    if not creates and not updates and not deletes:
        raise ValueError("apply input contains no changes")
    return creates, updates, deletes


def _schema() -> dict[str, Any]:
    return {
        "quote": QuoteCreate.model_json_schema(),
        "apply": {
            "create": ["QuoteCreate", "..."],
            "update": [
                {"id": 1, "expected_revision": 1, "changes": {"amount": "2.50"}}
            ],
            "delete": [{"id": 2, "expected_revision": 3}],
        },
        "notes": [
            "Decimal values must be JSON strings.",
            "component_type monetary requires amount, currency, and unit.",
            "component_type discount requires discount_rate from 0 through 1.",
            "All create/update/delete operations are committed atomically.",
        ],
    }


def _write_export(path: Path, items: list[QuoteResponse]) -> None:
    rows: list[list[Any]] = []
    for item in items:
        value = item.model_dump(mode="json")
        value["conditions"] = json.dumps(
            value["conditions"], ensure_ascii=False, sort_keys=True
        )
        rows.append([
            "" if value.get(column) is None else value.get(column)
            for column in EXPORT_COLUMNS
        ])
    if path.suffix.lower() == ".xlsx":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Quotes")
        sheet.append(EXPORT_COLUMNS)
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        path.write_bytes(output.getvalue())
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows(rows)



def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain the TokenSQL flat quote workspace."
    )
    parser.add_argument("--db", required=True, type=Path, help="workspace SQLite path")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "schema", help="print the QuoteCreate schema and an apply envelope example"
    )

    listing = commands.add_parser("list", help="list active quotes as JSON")
    listing.add_argument("--q")
    listing.add_argument("--model-name")
    listing.add_argument("--manufacturer")
    listing.add_argument("--provider")
    listing.add_argument("--currency")
    listing.add_argument("--metric")
    listing.add_argument("--dimension")
    listing.add_argument("--limit", type=int, default=500)
    listing.add_argument("--offset", type=int, default=0)

    apply = commands.add_parser(
        "apply",
        help=(
            "atomically apply JSON create/update/delete arrays; run the schema "
            "command for a complete example"
        ),
    )
    apply.add_argument("input", type=Path)
    apply.add_argument("--actor", default="manual/cli")

    export = commands.add_parser("export", help="export filtered quotes to CSV/XLSX")
    export.add_argument("output", type=Path)
    export.add_argument("--model-name")
    export.add_argument("--manufacturer")
    export.add_argument("--provider")
    export.add_argument("--currency")
    export.add_argument("--metric")
    export.add_argument("--dimension")

    legacy = commands.add_parser(
        "import-legacy", help="copy verified quotes from a legacy SQLite database"
    )
    legacy.add_argument("source", type=Path)
    legacy.add_argument("--actor", default="manual/cli")
    legacy.add_argument("--idempotency-key")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = WorkspaceStore(args.db)
    try:
        if args.command == "schema":
            _print(_schema())
            return 0
        if args.command == "list":
            items, total = store.list_quotes(
                q=args.q, model_name=args.model_name,
                manufacturer=args.manufacturer, provider=args.provider,
                currency=args.currency, metric=args.metric,
                dimension=args.dimension, limit=args.limit, offset=args.offset,
            )
            _print({"items": [_response_dict(item) for item in items], "total": total})
            return 0
        if args.command == "apply":
            creates, updates, deletes = _load_apply(args.input)
            items = store.apply_changes(
                creates=creates, updates=updates, deletes=deletes, actor=args.actor
            )
            _print({
                "changes": store.change_summary(args.actor),
                "items": [_response_dict(item) for item in items],
            })
            return 0
        if args.command == "export":
            items, _ = store.list_quotes(
                model_name=args.model_name, manufacturer=args.manufacturer,
                provider=args.provider, currency=args.currency, metric=args.metric,
                dimension=args.dimension, limit=100_000, offset=0,
            )
            _write_export(args.output, items)
            _print({"exported": len(items), "path": str(args.output)})
            return 0
        if args.command == "import-legacy":
            items, reused = store.import_legacy_sqlite(
                args.source, actor=args.actor,
                idempotency_key=args.idempotency_key,
            )
            _print({
                "changes": store.change_summary(args.actor),
                "items": [_response_dict(item) for item in items],
                "reused": reused,
            })
            return 0
        raise AssertionError(f"unsupported command: {args.command}")
    except Exception as error:
        _print({"error": " ".join(str(error).split())[:1000]})
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
