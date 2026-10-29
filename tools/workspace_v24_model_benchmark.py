#!/usr/bin/env python3
"""Opt-in real OpenCode/Qwen benchmark using synthetic workbooks and an isolated database."""
from __future__ import annotations
import argparse
from decimal import Decimal
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from openpyxl import Workbook
from openpyxl.styles import Font
from tokensql.workspace.config import PROJECT_ROOT, WorkspaceSettings
from tokensql.workspace.service import FALLBACK_BIN, FALLBACK_CONFIG


def workbook(rows, *, presentation=False, unit="USD/1M tokens"):
    book = Workbook()
    sheet = book.active
    sheet.title = "Rates"
    sheet.append(["model_id", "billing_metric", "amount", "pricing_unit"])
    for model, amount in rows:
        sheet.append([model, "input", amount, unit])
    if presentation:
        for row in sheet:
            for cell in row:
                cell.font = Font(name="Arial", bold=True)
        sheet.row_dimensions[1].height = 35
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def canonical_entries(job):
    bundle = job.get("proposal_bundle") or job.get("bundle") or {}
    result = {}
    for row in bundle.get("create", []):
        key = row["model_id"]
        if key in result:
            raise AssertionError("Synthetic physical model split/duplicated: " + key)
        result[key] = {
            "amount": str(Decimal(str(row["amount"])).normalize()),
            "metric": row.get("billing_metric"),
            "unit": row.get("pricing_unit_id"),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18875)
    parser.add_argument("--opencode-port", type=int, default=18876)
    parser.add_argument("--rows", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    root = args.evidence_dir or Path(tempfile.mkdtemp(prefix="tokensql-v24-real-model-"))
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    for port in (args.port, args.opencode_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    settings = WorkspaceSettings()
    binary = settings.opencode_bin or FALLBACK_BIN
    provider = settings.opencode_config or FALLBACK_CONFIG
    if not binary.is_file() or not provider.is_file():
        raise RuntimeError("Configure the existing OpenCode executable and provider config")
    env = os.environ.copy()
    for part in ("workspace", "agent", "input", "progress", "data"):
        env["TOKENSQL_" + part.upper() + "_DIR"] = str(root / part)
    env.update(PYTHONDONTWRITEBYTECODE="1", TOKENSQL_TEST_MODE="true",
               TOKENSQL_FX_AUTO_REFRESH="false", TOKENSQL_OPENCODE_TIMEOUT_SECONDS=str(args.timeout),
               PYTHONPATH=str(PROJECT_ROOT / "src"))
    command = [sys.executable, "-B", "-m", "tokensql.workspace.runtime",
               "--port", str(args.port), "--opencode-port", str(args.opencode_port),
               "--workspace-dir", str(root / "workspace"), "--agent-dir", str(root / "agent"),
               "--opencode-bin", str(binary), "--provider-config", str(provider),
               "--no-auto-refresh-fx"]
    log = (root / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url="http://127.0.0.1:" + str(args.port), trust_env=False, timeout=30)
    evidence = {"kind": "real_local_model_synthetic_workbooks", "rows": args.rows,
                "model": settings.opencode_provider + "/" + settings.opencode_model, "cases": []}

    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def persist():
        (root / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Isolated runtime exited; inspect evidence server.log")
            try:
                if request("GET", "/health").get("isolated_test"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("Isolated runtime did not start")
        supplier = request("POST", "/api/workspace/suppliers", json={"name": "Synthetic v2.4 Benchmark"})
        sid = supplier["supplier_id"]
        rows = [("qa-v24-model-" + str(i).zfill(3), str(Decimal(i) / Decimal(10)))
                for i in range(1, args.rows + 1)]
        prompt = ("Read this synthetic supplier quote workbook. Each model_id is already the unique canonical ID. "
                  "Read all Rates rows and preserve the explicit input metric, exact amount and unit in each row. "
                  "All rows belong to the selected supplier and source_scope. There are no official prices. "
                  "Use source_sheet=Rates and exact workbook cell/range evidence in source_ref for every candidate. "
                  "Do not infer aliases, discounts or extra models. Prepare the review proposal with full sheet coverage.")

        def run_case(name, content, expected_rows, *, full=False, unit="usd_mtoken", confirm=False):
            started = time.monotonic()
            uploaded = request("POST", "/api/workspace/imports/upload",
                               files=[("files", ("synthetic-rates.xlsx", content,
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))])
            job = request("POST", "/api/workspace/imports/start", json={
                "upload_ids": [item["id"] for item in uploaded["items"]], "prompt": prompt,
                "supplier_id": sid, "source_scope": "benchmark", "full_reparse": full,
                "update_request": {"supplier_id": sid, "source_scope": "benchmark", "latest_confirmed": True}})
            deadline = time.monotonic() + args.timeout + 60
            while job["status"] in {"preparing", "queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.5)
                job = request("GET", "/api/workspace/imports/" + job["id"])
            (root / (name + "-job.json")).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            record = {"name": name, "job_id": job["id"], "status": job["status"],
                      "measured_wall_seconds": round(time.monotonic() - started, 6),
                      "metrics": job.get("metrics"), "context_usage": job.get("context_usage"),
                      "reading_plan": job.get("reading_plan"),
                      "clarification_count": len(job.get("clarifications", []))}
            evidence["cases"].append(record)
            persist()
            print(json.dumps({"case": name, "status": job["status"], "seconds": record["measured_wall_seconds"],
                              "metrics": job.get("metrics")}, ensure_ascii=False), flush=True)
            if job["status"] != "ready":
                raise AssertionError(name + ": expected a ready proposal; inspect private job evidence")
            expected = {model: {"amount": str(Decimal(amount).normalize()), "metric": "input", "unit": unit}
                        for model, amount in expected_rows}
            actual = canonical_entries(job)
            assert actual == expected, (name, actual, expected)
            assert job.get("preview", {}).get("ready", True), name + ": reconciliation needs review"
            metrics = job.get("metrics") or {}
            if name in {"unchanged", "presentation"}:
                assert metrics.get("semantic_requests") == 0 and metrics.get("reused_entries") == len(expected_rows), (name, metrics)
                groups = job.get("preview", {}).get("groups", {})
                assert not any(groups.get(key) for key in ("create", "update", "delete", "needs_confirmation")), (name, groups)
            if name == "small_change":
                assert (job.get("reading_plan") or {}).get("reuse_mode") == "partial", (name, job.get("reading_plan"))
                assert metrics.get("reused_entries") == len(expected_rows) - 1, (name, metrics)
                assert metrics.get("reinterpreted_entries") == 1 and metrics.get("semantic_requests") == 1, (name, metrics)
            if full:
                assert metrics.get("reused_entries") == 0 and metrics.get("semantic_requests") == 1, (name, metrics)
            record["processing_path_verified"] = True
            record["exact_expected_entries"] = True
            record["candidate_count"] = len(actual)
            if confirm:
                outcome = request("POST", "/api/workspace/imports/" + job["id"] + "/confirm",
                                  json={"expected_revision": job["revision"]})
                assert outcome["status"] == "confirmed"
            else:
                request("POST", "/api/workspace/imports/" + job["id"] + "/cancel")
            persist()
            return actual

        run_case("initial", workbook(rows), rows, confirm=True)
        run_case("unchanged", workbook(rows), rows)
        run_case("presentation", workbook(rows, presentation=True), rows)
        changed = list(rows)
        changed[args.rows // 2] = (changed[args.rows // 2][0], "9.875")
        changed = list(reversed(changed))
        delta = run_case("small_change", workbook(changed), changed)
        full = run_case("forced_full", workbook(changed), changed, full=True)
        assert delta == full
        run_case("unit_change", workbook(changed, unit="USD/1K tokens"), changed, unit="usd_ktoken")
        evidence["delta_equals_full"] = True
        snapshot = request("GET", "/api/workspace/catalog")
        evidence["final_confirmed_models"] = len(snapshot.get("models", []))
        evidence["completed"] = True
        persist()
        print(json.dumps({"evidence_dir": str(root), "completed": True}), flush=True)
    except BaseException as error:
        evidence["completed"] = False
        evidence["error"] = type(error).__name__ + ": " + str(error)
        persist()
        print(json.dumps({"evidence_dir": str(root), "completed": False, "error": evidence["error"]}), flush=True)
        raise
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


if __name__ == "__main__":
    main()
