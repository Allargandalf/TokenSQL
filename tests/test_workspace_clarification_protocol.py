"""Clarification artifact/CLI validation, deliberately isolated from real services."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import json

import httpx
import pytest

from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
from tokensql.workspace.clarification import NeedsInput
from tokensql.workspace.draft_cli import main
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore


@pytest.mark.parametrize("payload", [
    {}, {"questions": []}, {"questions": [" "]}, {"questions": [None]},
    {"questions": [True]}, {"questions": [12]}, {"questions": ["x" * 2001]},
    {"questions": ["x"] * 21}, {"questions": "question"},
    {"questions": ["x"], "summary": 12}, {"questions": ["x"], "summary": "x" * 4001},
    {"questions": ["x"], "bundle": {}},
])
def test_question_protocol_is_strict_and_bounded(payload):
    with pytest.raises(ValueError):
        NeedsInput.model_validate(payload)


def test_question_protocol_trims_and_supports_multiple_questions():
    request = NeedsInput.model_validate({"questions": [" model ID? ", " original unit? "], "summary": " context "})
    assert request.model_dump(mode="json") == {"questions": ["model ID?", "original unit?"], "summary": "context"}
    assert NeedsInput.model_validate({"questions": ["unknown supplier may stay null?"]}).summary is None


def test_ask_cli_writes_only_questions_and_enforces_exclusion(tmp_path, capsys):
    directory = tmp_path / "attempt"
    directory.mkdir()
    output = directory / "needs_input.json"
    main(["ask", "--question", " Model ID? ", "--question", "Input or output?",
          "--summary", "Please clarify", "--output", str(output)])
    result = json.loads(output.read_text())
    assert result == {"questions": ["Model ID?", "Input or output?"], "summary": "Please clarify"}
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "waiting_for_input"
    snapshot = tmp_path / "snapshot.json"
    with_store = WorkspaceStore(":memory:")
    atomic_json(snapshot, with_store.catalog_snapshot())
    candidate = directory / "candidate.json"
    atomic_json(candidate, {"create": [{"model_id": "m"}]})
    with pytest.raises(SystemExit) as rejected:
        main(["prepare", "--catalog", str(snapshot), "--input", str(candidate),
              "--output", str(directory / "proposal.json")])
    assert rejected.value.code == 2 and not (directory / "proposal.json").exists()
    output.unlink()
    atomic_json(directory / "proposal.json", {"bundle": {}})
    with pytest.raises(SystemExit):
        main(["ask", "--question", "Why?", "--output", str(output)])
    assert not output.exists()
    with_store.close()


def test_ask_cli_refuses_invalid_filename_symlink_and_invalid_questions(tmp_path):
    target = tmp_path / "target.json"
    atomic_json(target, {"preserved": True})
    output = tmp_path / "needs_input.json"
    output.symlink_to(target)
    for path, question in [(output, "Q?"), (tmp_path / "other.json", "Q?"),
                           (tmp_path / "missing" / "needs_input.json", " ")]:
        with pytest.raises(SystemExit):
            main(["ask", "--question", question, "--output", str(path)])
    assert json.loads(target.read_text()) == {"preserved": True}


@pytest.mark.parametrize("failure", [
    "missing_artifact", "prose_only", "invalid_json", "invalid_questions", "extra_fields",
    "both_outputs", "question_symlink", "question_directory", "oversized",
    "http_error", "model_error", "pending_tool", "failed_tool",
])
def test_questions_never_mask_actual_execution_or_artifact_failure(tmp_path, failure):
    async def scenario():
        config = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                             python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "agent")
        store = WorkspaceStore(config.database_path)
        actual_reply = "Current assistant reply: identify the model first."
        def handler(request):
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_protocol"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(200, json=True)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            system = json.loads(request.content)["system"]
            directory = Path(re.search(r"Use only this run directory for writable output: (.+)", system)[1])
            output = directory / "needs_input.json"
            if failure not in {"missing_artifact", "prose_only"}:
                if failure == "invalid_json":
                    output.write_text("{invalid-json")
                elif failure == "invalid_questions":
                    atomic_json(output, {"questions": [" "]})
                elif failure == "extra_fields":
                    atomic_json(output, {"questions": ["Model ID?"], "bundle": {}})
                elif failure == "question_symlink":
                    target = directory / "target.json"
                    atomic_json(target, {"questions": ["Model ID?"]})
                    output.symlink_to(target)
                elif failure == "question_directory":
                    output.mkdir()
                elif failure == "oversized":
                    output.write_text(" " * (256 * 1024 + 1))
                else:
                    atomic_json(output, {"questions": ["Model ID?"]})
            if failure == "both_outputs":
                atomic_json(directory / "proposal.json", {"bundle": {}})
            info = {"role": "assistant"}
            if failure == "model_error":
                info["error"] = {"name": "ProviderError"}
            parts = [{"type": "text", "text": actual_reply}]
            if failure in {"pending_tool", "failed_tool"}:
                parts.append({"type": "tool", "tool": "bash", "state": {
                    "status": "pending" if failure == "pending_tool" else "error"}})
            return httpx.Response(503 if failure == "http_error" else 200, json={"info": info, "parts": parts})
        manager = OpenCodeAgentManager(config=config, store=store, transport=httpx.MockTransport(handler))
        run = await manager.submit(prompt="Use real structured questions")
        result = await manager.wait(run.id)
        assert result.status == "failed" and result.assistant_message == actual_reply
        assert result.questions == [] and result.error
        assert store.catalog_snapshot()["quotes"] == [] and manager.memory.read()["rules"] == []
        store.close()
    asyncio.run(scenario())


def test_legacy_job_public_defaults_do_not_change_persisted_revision(tmp_path):
    config = AgentConfig(workspace_dir=tmp_path / "workspace", database_path=tmp_path / "quotes.sqlite3",
                         python_bin=Path("/usr/bin/python3"), agent_dir=tmp_path / "agent")
    store = WorkspaceStore(config.database_path)
    manager = OpenCodeAgentManager(config=config, store=store)
    job = manager.workflow.create_draft({})
    internal = manager.workflow._job(job["id"])
    for field in ("attempt", "questions", "question_summary", "clarifications"):
        internal.pop(field)
    manager.workflow._save()
    restored = OpenCodeAgentManager(config=config, store=store)
    public = restored.get_run(job["id"]).public_dict()
    assert public["attempt"] == 1 and public["questions"] == [] and public["clarifications"] == []
    assert public["question_summary"] is None
    assert restored.workflow.get(job["id"])["revision"] == job["revision"]
    store.close()
