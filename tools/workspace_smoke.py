#!/usr/bin/env python3
"""Run one isolated OpenCode-to-workspace smoke test with generated data."""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
import tempfile

from openpyxl import Workbook

from tokensql.workspace.agent import AgentConfig, AgentUpload, OpenCodeAgentManager
from tokensql.workspace.store import WorkspaceStore


def _workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet.append(["模型", "输入价格", "输出价格"])
    sheet.append(["Alpha-1", 1.25, 3.5])
    sheet.append(["Beta 2", 0.8, 2.4])
    workbook.save(path)

def _validation_errors(result: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if result.get("status") != "completed":
        errors.append("run did not complete")
    if result.get("database_total") != 4:
        errors.append("database does not contain exactly four quotes")
    changes = result.get("changes")
    if not isinstance(changes, dict) or changes.get("added") != 4 or changes.get("total") != 4:
        errors.append("audit change summary is not added=4,total=4")
    quotes = result.get("quotes")
    if not isinstance(quotes, list):
        return [*errors, "quotes payload is not a list"]
    indexed = {
        (item.get("model_name"), item.get("direction")): item
        for item in quotes
        if isinstance(item, dict)
    }
    expected = {
        ("Alpha-1", "input"): ("1.25", "B2"),
        ("Alpha-1", "output"): ("3.5", "C2"),
        ("Beta 2", "input"): ("0.8", "B3"),
        ("Beta 2", "output"): ("2.4", "C3"),
    }
    if len(quotes) != 4 or set(indexed) != set(expected):
        errors.append("model/direction pairs differ from the generated workbook")
    for key, (amount, cell) in expected.items():
        item = indexed.get(key)
        if item is None:
            continue
        try:
            amount_matches = Decimal(str(item.get("amount"))) == Decimal(amount)
            quantity = Decimal(str(item.get("unit_quantity")))
        except (InvalidOperation, TypeError):
            amount_matches, quantity = False, Decimal("-1")
        if not amount_matches:
            errors.append(f"{key} amount differs from {amount}")
        unit = str(item.get("unit") or "").strip().casefold().replace("_", " ")
        million_tokens = (
            quantity == Decimal("1000000") and unit in {"token", "tokens"}
        ) or (
            quantity == Decimal("1")
            and unit in {"million token", "million tokens", "百万token", "百万 tokens"}
        )
        if not million_tokens:
            errors.append(f"{key} is not priced per million tokens")
        context_ok = (
            item.get("currency") == "CNY"
            and item.get("provider") == "烟雾测试供应商"
            and item.get("provider_type") == "reseller"
        )
        if not context_ok:
            errors.append(f"{key} context fields are incorrect")
        source_ok = (
            item.get("source") == "两行 报价.xlsx"
            and cell in str(item.get("source_ref") or "")
        )
        if not source_ok:
            errors.append(f"{key} source reference does not include {cell}")
    return errors


async def _run(server_url: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="tokensql-workspace-smoke-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        source = root / "两行 报价.xlsx"
        _workbook(source)
        store = WorkspaceStore(workspace / "quotes.sqlite3")
        manager = OpenCodeAgentManager(
            config=AgentConfig(
                workspace_dir=workspace,
                database_path=workspace / "quotes.sqlite3",
                python_bin=Path(sys.executable),
                server_url=server_url,
                timeout_seconds=900,
            ),
            store=store,
        )
        run = await manager.submit(
            prompt=(
                "导入该Excel的全部报价。供应商是烟雾测试供应商，"
                "provider_type=reseller；币种为CNY；价格单位均为每100万token；"
                "输入价格方向input，输出价格方向output。"
            ),
            files=[
                AgentUpload(
                    filename=source.name,
                    content=source.read_bytes(),
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                )
            ],
        )
        result = await manager.wait(run.id)
        quotes, total = store.list_quotes(limit=20)
        result_payload: dict[str, object] = {
            "status": result.status,
            "conversation_id": result.conversation_id,
            "assistant_message": result.assistant_message,
            "changes": result.changes,
            "error": result.error,
            "database_total": total,
            "quotes": [
                {
                    "model_name": item.model_name,
                    "amount": str(item.amount),
                    "currency": item.currency,
                    "unit": item.unit,
                    "unit_quantity": str(item.unit_quantity),
                    "direction": item.direction,
                    "provider": item.provider,
                    "provider_type": item.provider_type,
                    "source": item.source,
                    "source_ref": item.source_ref,
                }
                for item in quotes
            ],
        }
        errors = _validation_errors(result_payload)
        result_payload["validation_errors"] = errors
        result_payload["validated"] = not errors
        return result_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", default="http://127.0.0.1:8766")
    parser.add_argument("--evidence-path", type=Path)
    args = parser.parse_args()
    result = asyncio.run(_run(args.server_url.rstrip("/")))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.evidence_path is not None:
        args.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["validated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
