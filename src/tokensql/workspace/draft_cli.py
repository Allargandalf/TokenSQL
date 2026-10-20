"""Draft-only CLI. It has no production database argument or commit operation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .memory import atomic_json
from .clarification import NeedsInput
from .pricing import convert_amount, scaled_amount
from .schemas import PricingBundle
from .workflow import normalize_bundle_discounts

def prepare(bundle: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    issues = bundle.get("issues", []) if isinstance(bundle.get("issues"), list) else []
    bundle = bundle.get("bundle", bundle)
    # Raw amount + pricing_unit_id are never replaced with converted display prices.
    validated = PricingBundle.model_validate(normalize_bundle_discounts(bundle)).model_dump(mode="json")
    from .store import WorkspaceStore
    store = WorkspaceStore.from_snapshot(snapshot)
    try:
        preview = store.preview_bundle(validated)
        return {"bundle": validated, "preview": preview, "issues": issues[:2000]}
    finally:
        store.close()

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("Only model_id is required. Other business fields may be null; pure discounts and "
                "partial units are valid. A complete pricing_unit_id uses one of the 12 dictionary "
                "values: USD/HKD/CNY x ktoken/mtoken/second/image. Never derive raw amount or "
                "discount_rate. Observed discount_rate is normalized by deterministic code to exactly "
                "two decimal places using ROUND_HALF_UP (0.80 means eight-tenths payment; "
                "0.15 means 1.5 Chinese zhe). Null stays null; negative input is rejected before "
                "rounding. Original amounts and FX values are not rounded. Omitted currency defaults "
                "to CNY; explicit USD/HKD or null stays as "
                "provided. Only model_id identifies a model, with verified aliases for spelling "
                "variants; never emit physical_key. Confirmed delete operations are permanent, "
                "with audit history retained. All conversions and official-price calculations are frontend-only "
                "display/export views, never stored draft data."),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema")
    ask = commands.add_parser("ask", help="Pause this attempt for a consolidated human clarification")
    ask.add_argument("--question", action="append", required=True)
    ask.add_argument("--summary")
    ask.add_argument("--output", type=Path, required=True)
    draft = commands.add_parser("prepare")
    draft.add_argument("--catalog", type=Path, required=True)
    draft.add_argument("--input", type=Path, required=True)
    draft.add_argument("--output", type=Path, required=True)
    fx = commands.add_parser("fx", help="Explicit-rate calculator; output is derived and never a raw quote")
    fx.add_argument("--original-amount", required=True)
    fx.add_argument("--original-currency", required=True)
    fx.add_argument("--exchange-rate", required=True)
    fx.add_argument("--rate-source", required=True)
    scale = commands.add_parser("scale", help="Scale quantities within the same measurement unit only")
    scale.add_argument("--amount", required=True)
    scale.add_argument("--from-quantity", required=True)
    scale.add_argument("--to-quantity", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            print(json.dumps(PricingBundle.model_json_schema(), ensure_ascii=False, indent=2))
        elif args.command == "ask":
            output = args.output.resolve()
            if output.name != "needs_input.json" or args.output.is_symlink():
                raise ValueError("question output must be a non-symlink needs_input.json")
            proposal = output.with_name("proposal.json")
            if proposal.exists() or proposal.is_symlink():
                raise ValueError("a proposal already exists; questions and proposals are mutually exclusive")
            payload = NeedsInput.model_validate({"questions": args.question, "summary": args.summary}).model_dump(mode="json")
            atomic_json(output, payload)
            print(json.dumps({"output": str(output), "status": "waiting_for_input", **payload}, ensure_ascii=False))
        elif args.command == "fx":
            if not args.rate_source.strip() or args.original_currency.upper() not in {"USD", "HKD"}:
                raise ValueError("FX requires USD/HKD and an explicit rate source")
            print(json.dumps({"raw": {"amount": args.original_amount, "currency": args.original_currency.upper()},
                              "derived": {"amount": format(convert_amount(args.original_amount, args.exchange_rate), "f"),
                                          "currency": "CNY", "exchange_rate": args.exchange_rate,
                                          "rate_source": args.rate_source},
                              "notice": "Derived calculation only; preserve original quoted amount and currency in the bundle"}))
        elif args.command == "scale":
            print(json.dumps({"amount": format(scaled_amount(args.amount, args.from_quantity, args.to_quantity), "f"),
                              "unit_quantity": args.to_quantity}))
        else:
            output = args.output.resolve()
            candidate = args.input.resolve()
            if output.name != "proposal.json" or output.parent != candidate.parent or output == args.catalog.resolve():
                raise ValueError("output must be proposal.json beside the candidate")
            if args.input.is_symlink() or args.output.is_symlink():
                raise ValueError("draft paths must not be symlinks")
            clarification = output.with_name("needs_input.json")
            if clarification.exists() or clarification.is_symlink():
                raise ValueError("clarification already exists; questions and proposals are mutually exclusive")
            result = prepare(json.loads(candidate.read_text(encoding="utf-8")),
                             json.loads(args.catalog.read_text(encoding="utf-8")))
            atomic_json(output, result)
            print(json.dumps({"output": str(output), "changes": result["preview"].get("changes", {}),
                              "issues": result["preview"].get("issues", []) + result["issues"]}, ensure_ascii=False))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, "Draft rejected: " + str(error) + "\n")

if __name__ == "__main__":
    main()
