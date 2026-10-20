#!/usr/bin/env python3
"""Exercise upload -> review -> cancel against an HTTP workspace endpoint.

The default path never confirms a proposal.  Confirmation is additionally gated by
an explicit isolated-test health marker and an initially empty quote catalog.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
from pathlib import Path
from typing import Any

import httpx
from openpyxl import Workbook

SYNTHETIC_FILENAME = "tokensql-synthetic-pricing.xlsx"
EXPECTED = {
    ("Alpha-1", "input"): ("1.25", "B2"),
    ("Alpha-1", "output"): ("3.5", "C2"),
    ("Beta 2", "input"): ("0.8", "B3"),
    ("Beta 2", "output"): ("2.4", "C3"),
}
TERMINAL_REVIEW_STATUSES = {"ready", "failed", "cancelled", "confirmed"}


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Synthetic Quotes"
    sheet.append(["model", "input price", "output price"])
    sheet.append(["Alpha-1", "1.25", "3.5"])
    sheet.append(["Beta 2", "0.8", "2.4"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _proposal_errors(proposal: object) -> list[str]:
    if not isinstance(proposal, dict):
        return ["proposal payload is not an object"]
    models = proposal.get("models")
    if not isinstance(models, list):
        return ["proposal models payload is not a list"]
    names = {str(item.get("model_name")) for item in models if isinstance(item, dict)}
    identifiers = {str(item.get("model_id")) for item in models if isinstance(item, dict)}
    errors = []
    if names != {"Alpha-1", "Beta 2"} or len(models) != 2:
        errors.append("proposal does not contain exactly the two synthetic models")
    if len(identifiers) != 2 or "None" in identifiers:
        errors.append("synthetic models do not have two independent model IDs")
    creates = proposal.get("create")
    if not isinstance(creates, list) or len(creates) != 4:
        errors.append("proposal does not contain exactly four quote creates")
    return errors


def _validation_errors(result: dict[str, object]) -> list[str]:
    errors = _proposal_errors(result.get("proposal"))
    status = result.get("status")
    if status not in {"ready", "confirmed"}:
        errors.append("workflow is neither review-ready nor confirmed")
    expected_total = 0 if status == "ready" else 4
    if result.get("database_total") != expected_total:
        errors.append(f"database total is not {expected_total} for {status}")
    changes = result.get("changes")
    if not isinstance(changes, dict) or changes.get("added") != 4 or changes.get("total") != 4:
        errors.append("preview/commit changes are not added=4,total=4")
    quotes = result.get("quotes")
    if not isinstance(quotes, list):
        return [*errors, "quotes payload is not a list"]
    indexed = {(item.get("model_name"), item.get("billing_item")): item
               for item in quotes if isinstance(item, dict)}
    if len(quotes) != 4 or set(indexed) != set(EXPECTED):
        errors.append("model/billing_item pairs differ from the synthetic workbook")
    proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else {}
    proposed_models = {item.get("model_id") for item in proposal.get("models", []) if isinstance(item, dict)}
    seen_models = {item.get("model_id") for item in quotes if isinstance(item, dict)}
    if len(proposed_models) != 2 or seen_models != proposed_models:
        errors.append("quote rows do not reference the two independent proposed models")
    for key, (amount, cell) in EXPECTED.items():
        item = indexed.get(key)
        if item is None:
            continue
        try:
            amount_matches = Decimal(str(item.get("amount"))) == Decimal(amount)
        except (InvalidOperation, TypeError):
            amount_matches = False
        if not amount_matches:
            errors.append(f"{key} amount differs from {amount}")
        if item.get("pricing_unit_id") != "cny_mtoken":
            errors.append(f"{key} pricing_unit_id is not cny_mtoken")
        context_ok = (item.get("currency") == "CNY"
                      and (item.get("supplier") or item.get("provider")) == "Synthetic smoke supplier"
                      and item.get("provider_type") == "reseller")
        if not context_ok:
            errors.append(f"{key} context fields are incorrect")
        source_ok = (item.get("source") == SYNTHETIC_FILENAME
                     and cell in str(item.get("source_ref") or ""))
        if not source_ok:
            errors.append(f"{key} source reference does not include {cell}")
    return errors


def _review_result(job: dict[str, Any], database_total: int) -> dict[str, object]:
    preview = job.get("preview") if isinstance(job.get("preview"), dict) else {}
    return {
        "status": job.get("status"), "job_id": job.get("id"),
        "conversation_id": job.get("conversation_id"), "proposal": job.get("bundle"),
        "changes": preview.get("changes"), "database_total": database_total,
        "quotes": preview.get("items"), "issues": job.get("issues", []),
        "error": job.get("error"),
    }


async def _run(base_url: str, *, confirm: bool = False, timeout_seconds: float = 900,
               transport: httpx.AsyncBaseTransport | None = None,
               poll_interval: float = 0.5) -> dict[str, object]:
    """Run only against the HTTP boundary; never instantiate a store or agent manager."""
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport) as client:
        health_response = await client.get("/health")
        health_response.raise_for_status()
        health = health_response.json()
        quotes_response = await client.get("/api/workspace/quotes", params={"limit": 1})
        quotes_response.raise_for_status()
        initial_total = quotes_response.json().get("total")
        if initial_total != 0:
            return {"status": "refused", "database_total": initial_total, "validated": False,
                    "validation_errors": ["smoke requires an initially empty quote catalog"]}
        if confirm and health.get("isolated_test") is not True:
            return {"status": "refused", "database_total": initial_total, "validated": False,
                    "validation_errors": ["--confirm requires health.isolated_test=true"]}
        upload_response = await client.post("/api/workspace/imports/upload", files={
            "files": (SYNTHETIC_FILENAME, _workbook_bytes(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        upload_response.raise_for_status()
        uploads = upload_response.json().get("items", [])
        if len(uploads) != 1 or not uploads[0].get("id"):
            raise RuntimeError("workspace did not return one upload ID")
        prompt = (
            "SYNTHETIC QA DATA ONLY. Import all four supplier quotes from the attached workbook. "
            "Create exactly two physical models Alpha-1 and Beta 2. Use billing_item input/output, "
            "supplier='Synthetic smoke supplier', provider_type='reseller', and pricing_unit_id='cny_mtoken'. "
            f"Keep source='{SYNTHETIC_FILENAME}' and exact worksheet cell references in source_ref. "
            "Generate a review proposal only; do not claim these are official prices."
        )
        start_response = await client.post("/api/workspace/imports/start", json={
            "upload_ids": [uploads[0]["id"]], "prompt": prompt})
        start_response.raise_for_status()
        job = start_response.json()
        identifier = job.get("id")
        if not identifier:
            raise RuntimeError("workspace did not return a review job ID")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while job.get("status") not in TERMINAL_REVIEW_STATUSES:
            if loop.time() >= deadline:
                raise TimeoutError("workspace review did not become ready before timeout")
            await asyncio.sleep(poll_interval)
            response = await client.get(f"/api/workspace/imports/{identifier}")
            response.raise_for_status()
            job = response.json()
        review = _review_result(job, initial_total)
        if job.get("status") != "ready":
            review["validated"] = False
            review["validation_errors"] = [f"review ended as {job.get('status')}: {job.get('error') or 'no error detail'}"]
            return review
        errors = _validation_errors(review)
        if errors:
            # A malformed proposal is still cancelled so the synthetic upload returns to input.
            cancel_response = await client.post(f"/api/workspace/imports/{identifier}/cancel")
            cancel_response.raise_for_status()
            final_status = cancel_response.json().get("status")
            post_response = await client.get("/api/workspace/quotes", params={"limit": 1})
            post_response.raise_for_status()
            post_total = post_response.json().get("total")
            if final_status != "cancelled":
                errors.append("invalid review did not cancel")
            if post_total != 0:
                errors.append("invalid review changed the quote database")
            review.update(final_status=final_status, post_action_database_total=post_total,
                          validation_errors=errors, validated=False)
            return review
        if not confirm:
            cancel_response = await client.post(f"/api/workspace/imports/{identifier}/cancel")
            cancel_response.raise_for_status()
            final = cancel_response.json()
            post_response = await client.get("/api/workspace/quotes", params={"limit": 1})
            post_response.raise_for_status()
            post_total = post_response.json().get("total")
            if final.get("status") != "cancelled":
                errors.append("review did not cancel")
            if post_total != 0:
                errors.append("default cancellation changed the quote database")
            review.update(final_status=final.get("status"), post_action_database_total=post_total,
                          validation_errors=errors, validated=not errors)
            return review
        confirm_response = await client.post(f"/api/workspace/imports/{identifier}/confirm", json={
            "expected_revision": job["revision"], "idempotency_key": f"synthetic-smoke-{identifier}"})
        confirm_response.raise_for_status()
        confirmed = confirm_response.json()
        committed_response = await client.get("/api/workspace/quotes", params={"limit": 20})
        committed_response.raise_for_status()
        committed = committed_response.json()
        result = {
            "status": confirmed.get("status"), "job_id": identifier,
            "proposal": job.get("bundle"), "changes": confirmed.get("changes"),
            "database_total": committed.get("total"), "quotes": committed.get("items"),
            "final_status": confirmed.get("status"), "health": health,
        }
        errors = _validation_errors(result)
        result.update(validation_errors=errors, validated=not errors)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe synthetic HTTP review smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--confirm", action="store_true",
                        help="commit only when health.isolated_test=true and the quote catalog is empty")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--evidence-path", type=Path)
    args = parser.parse_args(argv)
    result = asyncio.run(_run(args.base_url.rstrip("/"), confirm=args.confirm,
                              timeout_seconds=args.timeout_seconds))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.evidence_path is not None:
        args.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result.get("validated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
