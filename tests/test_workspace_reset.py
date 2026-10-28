"""Independent recoverable-reset acceptance, restricted to pytest-owned synthetic trees."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from tokensql.workspace import reset as cleanup
from tokensql.workspace import service
from tokensql.workspace.config import WorkspaceSettings
from tokensql.workspace.memory import AgentMemory, atomic_json
from tokensql.workspace.store import WorkspaceStore
from tokensql.workspace.workflow import WorkflowService


def make_settings(root: Path, **updates):
    return WorkspaceSettings(_env_file=None, workspace_dir=root / "var" / "workspace",
        agent_dir=root.parent / (root.name + "-opencode"), input_dir=root / "input",
        progress_dir=root / "progress", data_dir=root / "data", test_mode=True,
        **updates)


def file_bytes(root):
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()}


def fingerprint(root):
    values = {}
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        relative = str(path.relative_to(root))
        if path.is_symlink():
            payload = ("symlink", os.readlink(path))
        elif path.is_file():
            payload = ("file", path.read_bytes())
        else:
            payload = ("directory",)
        values[relative] = (stat.S_IMODE(info.st_mode), info.st_ino, payload)
    return values


def seeded(tmp_path, monkeypatch, *, running=False, args=()):
    root = tmp_path / "TokenSQL"
    root.mkdir()
    settings = make_settings(root)
    for relative in ("input", "progress/job", "data/job", "tests", "src", "docs", "scripts", ".git"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative, data in {
        "input/new.xlsx": b"unprocessed original workbook",
        "progress/job/current.xlsx": b"in-progress original workbook",
        "data/job/archived.xlsx": b"archived original workbook",
        "tests/benchmark.sqlite": b"Codex reference test database",
        "src/business.py": b"source code",
        "docs/rules.md": b"documentation",
        "scripts/keep.sh": b"deployment code",
        ".env": b"TOKENSQL_SECRET=fixture-not-real\n",
        ".git/HEAD": b"ref: refs/heads/main\n",
    }.items():
        (root / relative).write_bytes(data)
    store = WorkspaceStore(settings.database_path)
    store.apply_bundle({
        "models": [{"model_id": "old-model", "vendor": "Old Vendor", "aliases": ["old alias"]}],
        "official_prices": [{"model_id": "old-model", "amount": "12.3456789", "explicit_official": True}],
        "create": [{"model_id": "old-model", "supplier": "Old Supplier", "amount": "1.23456789",
                    "discount_rate": "0.8"}],
    }, actor="old-user", import_id="old-import")
    memory = AgentMemory(settings.agent_dir)
    memory.update([{"scope": "global", "text": "PRIVATE_OLD_CORRECTION_SENTINEL"}], memory.read()["revision"])
    WorkflowService(settings, store, memory).create_draft({"create": [{"model_id": "unconfirmed"}]})
    store.close()
    atomic_json(settings.agent_dir / "sessions.json", {"ses_old": {"id": "ses_old", "title": "old session"}})
    (settings.agent_dir / "old-model-answer.log").write_bytes(b"old conversation answer")
    old_sdk = settings.agent_dir / "opencode" / "data"
    old_sdk.mkdir(parents=True)
    connection = sqlite3.connect(old_sdk / "opencode.db")
    connection.execute("CREATE TABLE history(message TEXT)")
    connection.execute("INSERT INTO history VALUES ('old sdk session text')")
    connection.commit()
    connection.close()
    (settings.workspace_dir / "runtime.log").write_bytes(b"old service log")
    (settings.workspace_dir / "extra-cache").mkdir()
    (settings.workspace_dir / "extra-cache" / "artifact.json").write_bytes(b"old cached draft")
    (settings.workspace_dir / "service.lock").write_bytes(b"preserve this lock inode")
    proc = tmp_path / "proc"
    proc.mkdir()
    controller = service.Controller(args, root=root, proc=proc, settings=settings)
    state = {"running": running}
    original_args = ["--workspace-dir", str(settings.workspace_dir), "--agent-dir", str(settings.agent_dir),
                     "--port", str(controller.spec.port), "--opencode-port", str(controller.spec.opencode_port),
                     "--no-auto-refresh-fx", "--provider-config", str(root / "fixture-provider.json")]
    process = service.Process(12345, 999, "S", root,
        ("python", "-m", service.MODULE, *original_args), {})
    calls = []
    monkeypatch.setattr(controller, "candidates", lambda: [process] if state["running"] else [])
    monkeypatch.setattr(controller, "port_available", lambda *unused: True)
    monkeypatch.setattr(cleanup, "read_process", lambda *unused: None)
    def stop():
        with controller.lock():
            calls.append(("stop", list(controller.args)))
            state["running"] = False
            controller.metadata.unlink(missing_ok=True)
        return 0
    def start():
        with controller.lock():
            calls.append(("start", list(controller.args)))
            state["running"] = True
        return 0
    monkeypatch.setattr(controller, "stop", stop)
    monkeypatch.setattr(controller, "start", start)
    paths = cleanup.validate_paths(root, settings)
    return SimpleNamespace(root=root, settings=settings, controller=controller, paths=paths,
                           calls=calls, state=state, process=process, original_args=original_args)


def assert_fresh(rig):
    store = WorkspaceStore(rig.settings.database_path)
    try:
        for table in ("pricing_vendor", "pricing_model", "pricing_alias", "pricing_official",
                      "pricing_supplier", "pricing_channel", "pricing_quote", "pricing_audit", "pricing_import"):
            assert store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        context = store.pricing_context()
        assert context["revision"] == 1 and [x["currency"] for x in context["fx_rates"]] == ["CNY"]
    finally:
        store.close()
    memory = json.loads((rig.settings.agent_dir / "memory.json").read_text())
    workflow = json.loads((rig.settings.agent_dir / "workflow.json").read_text())
    assert memory["rules"] == [] and memory["confirmed_imports"] == []
    assert workflow["jobs"] == {} and workflow["uploads"] == {}
    assert "PRIVATE_OLD_CORRECTION_SENTINEL" not in (rig.settings.agent_dir / "agent.md").read_text()
    assert not (rig.settings.agent_dir / "sessions.json").exists()
    assert not (rig.settings.agent_dir / "opencode" / "data" / "opencode.db").exists()


def test_reset_moves_all_records_and_agent_bytes_but_preserves_excel_code_env_and_lock(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    original_workspace = file_bytes(rig.settings.workspace_dir)
    original_agent = file_bytes(rig.settings.agent_dir)
    preserved = {str(path.relative_to(rig.root)): fingerprint(path) for path in
                 (rig.settings.input_dir, rig.settings.progress_dir, rig.settings.data_dir,
                  rig.root / "tests", rig.root / "src", rig.root / "docs", rig.root / "scripts", rig.root / ".env", rig.root / ".git")}
    lock_inode = rig.controller.lockfile.stat().st_ino
    workspace_inode = rig.settings.workspace_dir.stat().st_ino
    backup = cleanup.reset(rig.controller, rig.paths)
    assert backup.parent == rig.paths.backup_base and stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert rig.settings.workspace_dir.stat().st_ino == workspace_inode
    assert rig.controller.lockfile.stat().st_ino == lock_inode
    assert rig.controller.lockfile.read_bytes() == original_workspace.pop("service.lock")
    assert not (backup / "workspace" / "service.lock").exists()
    assert file_bytes(backup / "workspace") == original_workspace
    assert file_bytes(backup / "agent") == original_agent
    for relative, expected in preserved.items():
        assert fingerprint(rig.root / relative) == expected
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["restarted"] is False
    assert stat.S_IMODE((backup / "manifest.json").stat().st_mode) == 0o600
    assert rig.calls == [("stop", [])] and rig.state["running"] is False
    assert_fresh(rig)
    old = sqlite3.connect("file:" + str(backup / "workspace" / "quotes.sqlite3") + "?mode=ro", uri=True)
    try:
        assert old.execute("SELECT count(*) FROM pricing_quote").fetchone()[0] == 1
        assert old.execute("SELECT count(*) FROM pricing_audit").fetchone()[0] > 0
    finally:
        old.close()


@pytest.mark.parametrize("restart", [True, False])
def test_running_reset_restarts_only_when_requested_with_exact_original_runtime_args(tmp_path, monkeypatch, restart):
    rig = seeded(tmp_path, monkeypatch, running=True)
    backup = cleanup.reset(rig.controller, rig.paths, restart=restart)
    manifest = json.loads((backup / "manifest.json").read_text())
    assert [item[0] for item in rig.calls] == (["stop", "start"] if restart else ["stop"])
    assert manifest["restarted"] is restart and rig.state["running"] is restart
    if restart:
        assert rig.calls[-1][1] == rig.original_args
    assert_fresh(rig)


@pytest.mark.parametrize("argv,answer,expected", [
    (["--dry-run"], None, 0), (["--dry-run", "--yes"], None, 0),
    ([], "cancel", 0), ([], "", 0), ([], EOFError, 0), ([], KeyboardInterrupt, 130),
])
def test_main_preview_cancel_eof_and_interrupt_are_entire_tree_zero_write(tmp_path, monkeypatch, argv, answer, expected):
    rig = seeded(tmp_path, monkeypatch, running=True)
    monkeypatch.setattr(cleanup, "Controller", lambda **unused: rig.controller)
    def input_answer(prompt):
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        if answer is None:
            pytest.fail("dry-run must not ask for confirmation")
        return answer
    monkeypatch.setattr("builtins.input", input_answer)
    before = fingerprint(tmp_path)
    assert cleanup.main(argv, root=rig.root, settings=rig.settings) == expected
    assert fingerprint(tmp_path) == before
    assert rig.calls == [] and rig.state["running"] is True


@pytest.mark.parametrize("target", ["workspace", "agent", "backup", "workspace-parent"])
def test_symlink_cleanup_targets_and_redirected_ancestors_are_refused(tmp_path, target):
    root = tmp_path / "TokenSQL"
    root.mkdir()
    settings = make_settings(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    backup = root.parent / "TokenSQL-maintenance-backups"
    if target == "workspace-parent":
        (root / "var").symlink_to(outside, target_is_directory=True)
    else:
        value = {"workspace": settings.workspace_dir, "agent": settings.agent_dir, "backup": backup}[target]
        value.parent.mkdir(parents=True, exist_ok=True)
        value.symlink_to(outside, target_is_directory=True)
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError):
        cleanup.validate_paths(root, settings)
    assert fingerprint(tmp_path) == before


@pytest.mark.parametrize("update", ["workspace", "agent", "input", "progress", "data", "backup-input"])
def test_wrong_targets_and_preserved_directory_overlaps_are_refused(tmp_path, update):
    root = tmp_path / "TokenSQL"
    root.mkdir()
    settings = make_settings(root)
    changes = {
        "workspace": {"workspace_dir": tmp_path / "unrelated"},
        "agent": {"agent_dir": root / "agent"},
        "input": {"input_dir": settings.workspace_dir / "uploads"},
        "progress": {"progress_dir": settings.agent_dir / "progress"},
        "data": {"data_dir": root.parent},
        "backup-input": {"input_dir": root.parent / "TokenSQL-maintenance-backups" / "input"},
    }[update]
    settings = settings.model_copy(update=changes)
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError):
        cleanup.validate_paths(root, settings)
    assert fingerprint(tmp_path) == before


def test_preserved_docs_symlink_resolves_into_workspace_and_blocks_reset(tmp_path):
    root = tmp_path / "TokenSQL"
    root.mkdir()
    settings = make_settings(root)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "important-doc.md").write_bytes(b"must not clear via docs alias")
    (root / "docs").symlink_to(settings.workspace_dir, target_is_directory=True)
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError):
        cleanup.validate_paths(root, settings)
    assert fingerprint(tmp_path) == before


@pytest.mark.parametrize("args", [["--external-opencode"], ["--agent-dir", "/tmp/not-this-project-agent"]])
def test_external_shared_or_mismatched_agent_spec_refused_before_stop_or_backup(tmp_path, monkeypatch, args):
    rig = seeded(tmp_path, monkeypatch, args=args)
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before and rig.calls == []


@pytest.mark.parametrize("blocked", ["web", "opencode"])
def test_occupied_ports_refuse_all_moves_without_signalling_foreign_services(tmp_path, monkeypatch, blocked):
    rig = seeded(tmp_path, monkeypatch)
    blocked_port = rig.controller.spec.port if blocked == "web" else rig.controller.spec.opencode_port
    monkeypatch.setattr(rig.controller, "port_available", lambda host, port: port != blocked_port)
    monkeypatch.setattr(service.os, "kill", lambda *unused: pytest.fail("must not signal a foreign service"))
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError, match="occupied"):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before
    assert rig.calls == [("stop", [])]


@pytest.mark.parametrize("kind", ["other-port-runtime", "overlap-runtime", "agent-cwd", "xdg-worker"])
def test_other_port_runtimes_and_orphan_workers_using_target_are_refused(tmp_path, monkeypatch, kind):
    rig = seeded(tmp_path, monkeypatch)
    entry = rig.controller.proc / "45678"
    entry.mkdir()
    process = service.Process(45678, 1000, "S", tmp_path, ("opencode", "serve", "--port", "9999"), {})
    if kind == "other-port-runtime":
        process = replace(process, cwd=rig.root, argv=("python", "-m", service.MODULE,
                           "--port", "9999", "--workspace-dir", str(rig.paths.workspace)))
    elif kind == "overlap-runtime":
        process = replace(process, argv=("python", "-m", service.MODULE,
                           "--workspace-dir", str(rig.paths.workspace.parent)))
    elif kind == "agent-cwd":
        process = replace(process, cwd=rig.paths.agent / "runs" / "old")
    else:
        process = replace(process, environ={"XDG_DATA_HOME": str(rig.paths.agent / "opencode" / "data")})
    monkeypatch.setattr(cleanup, "read_process", lambda pid, *unused: process if pid == process.pid else None)
    monkeypatch.setattr(service.os, "kill", lambda *unused: pytest.fail("orphan must not be signalled"))
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError, match="still uses"):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before


def test_unrelated_process_does_not_block_or_get_signalled(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    entry = rig.controller.proc / "45678"
    entry.mkdir()
    outsider = service.Process(45678, 1000, "S", tmp_path / "other",
                               ("python", "-m", "unrelated.service"), {"XDG_DATA_HOME": str(tmp_path / "shared-data")})
    monkeypatch.setattr(cleanup, "read_process", lambda *unused: outsider)
    monkeypatch.setattr(service.os, "kill", lambda *unused: pytest.fail("unrelated service must not be signalled"))
    backup = cleanup.reset(rig.controller, rig.paths)
    assert (backup / "manifest.json").exists()
    assert_fresh(rig)


def test_existing_lock_blocks_reset_with_zero_file_moves(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    before = fingerprint(tmp_path)
    with rig.controller.lock():
        with pytest.raises(service.ServiceError, match="operation"):
            cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before and rig.calls == []


def test_move_time_competing_controller_sees_same_lock_inode_and_is_refused(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    other = service.Controller(root=rig.root, settings=rig.settings, proc=rig.controller.proc)
    inode = rig.controller.lockfile.stat().st_ino
    rename = Path.rename
    checked = []
    def guarded(path, destination):
        if path.parent == rig.paths.workspace and path.name != "service.lock":
            assert other.lockfile.stat().st_ino == inode
            with pytest.raises(service.ServiceError, match="operation"):
                with other.lock():
                    pytest.fail("competing service operation acquired reset's lock")
            checked.append(path.name)
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", guarded)
    cleanup.reset(rig.controller, rig.paths)
    assert checked and rig.controller.lockfile.stat().st_ino == inode
    assert_fresh(rig)


def assert_original_bytes_retained(rig, backup, old_workspace, old_agent):
    for relative, expected in old_workspace.items():
        if relative == "service.lock":
            assert rig.controller.lockfile.read_bytes() == expected
            continue
        choices = [rig.paths.workspace / relative, backup / "workspace" / relative]
        assert any(path.is_file() and path.read_bytes() == expected for path in choices), relative
    for relative, expected in old_agent.items():
        choices = [rig.paths.agent / relative, backup / "agent" / relative]
        assert any(path.is_file() and path.read_bytes() == expected for path in choices), relative


def test_partial_move_failure_marks_failed_preserves_all_bytes_and_does_not_restart_or_restore(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch, running=True)
    old_workspace, old_agent = file_bytes(rig.paths.workspace), file_bytes(rig.paths.agent)
    rename = Path.rename
    moved = []
    def fail_second_workspace_entry(path, destination):
        if path.parent == rig.paths.workspace and path.name != "service.lock":
            if moved:
                raise OSError("injected second entry move failure")
            moved.append(path)
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", fail_second_workspace_entry)
    with pytest.raises(OSError, match="injected"):
        cleanup.reset(rig.controller, rig.paths)
    backups = list(rig.paths.backup_base.iterdir())
    assert len(backups) == 1
    manifest = json.loads((backups[0] / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["restarted"] is False
    assert len(manifest["moved"]) == 1 and len(moved) == 1
    assert [item[0] for item in rig.calls] == ["stop"] and not rig.state["running"]
    assert_original_bytes_retained(rig, backups[0], old_workspace, old_agent)
    # No automatic rollback has copied the first moved entry back from a backup.
    assert not moved[0].exists()
    assert (backups[0] / "workspace" / moved[0].name).exists()


def test_initialization_failure_keeps_archived_bytes_partial_artifacts_and_stopped_state(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch, running=True)
    old_workspace, old_agent = file_bytes(rig.paths.workspace), file_bytes(rig.paths.agent)
    def fail_initialize(settings):
        (settings.workspace_dir / "partial-init.txt").write_bytes(b"new incomplete initialization output")
        raise OSError("injected new initialization failure")
    monkeypatch.setattr(cleanup, "_initialize", fail_initialize)
    with pytest.raises(OSError, match="injected new"):
        cleanup.reset(rig.controller, rig.paths)
    backup = next(rig.paths.backup_base.iterdir())
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["restarted"] is False
    assert [item[0] for item in rig.calls] == ["stop"] and not rig.state["running"]
    assert_original_bytes_retained(rig, backup, old_workspace, old_agent)
    assert (rig.paths.workspace / "partial-init.txt").read_bytes() == b"new incomplete initialization output"
    assert not (rig.paths.workspace / "quotes.sqlite3").exists()
    assert not rig.paths.agent.exists()


def test_stop_failure_does_not_move_anything_or_create_backup(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch, running=True)
    def fail_stop():
        raise service.ServiceError("owned runtime would not stop")
    monkeypatch.setattr(rig.controller, "stop", fail_stop)
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError, match="would not stop"):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before and rig.calls == []


def test_restart_failure_marks_failed_without_restoring_or_losing_archived_data(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch, running=True)
    old_workspace, old_agent = file_bytes(rig.paths.workspace), file_bytes(rig.paths.agent)
    def fail_start():
        rig.calls.append(("start-failed", list(rig.controller.args)))
        raise service.ServiceError("synthetic startup failure")
    monkeypatch.setattr(rig.controller, "start", fail_start)
    with pytest.raises(service.ServiceError, match="synthetic startup"):
        cleanup.reset(rig.controller, rig.paths)
    backup = next(rig.paths.backup_base.iterdir())
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["restarted"] is False
    assert [item[0] for item in rig.calls] == ["stop", "start-failed"] and not rig.state["running"]
    assert_original_bytes_retained(rig, backup, old_workspace, old_agent)
    assert_fresh(rig)


def test_repeated_reset_uses_unique_backups_and_never_reimports_previous_data(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    first = cleanup.reset(rig.controller, rig.paths)
    original_first = file_bytes(first)
    lock_inode = rig.controller.lockfile.stat().st_ino
    second = cleanup.reset(rig.controller, rig.paths)
    assert second != first and second.parent == first.parent
    assert file_bytes(first) == original_first
    assert len(list(first.parent.iterdir())) == 2
    assert rig.controller.lockfile.stat().st_ino == lock_inode
    assert [item[0] for item in rig.calls] == ["stop", "stop"]
    assert_fresh(rig)


@pytest.mark.parametrize("flag,target", [
    ("--provider-config", "agent"), ("--opencode-bin", "workspace"),
])
@pytest.mark.parametrize("running", [False, True])
def test_runtime_resources_inside_cleanup_targets_are_refused_before_stop(tmp_path, monkeypatch, flag, target, running):
    rig = seeded(tmp_path, monkeypatch, running=running)
    resource = (rig.paths.agent if target == "agent" else rig.paths.workspace) / "do-not-move"
    resource.write_bytes(b"runtime resource")
    if running:
        current = replace(rig.process, argv=(*rig.process.argv, flag, str(resource)))
        monkeypatch.setattr(rig.controller, "candidates", lambda: [current])
    else:
        rig.controller.args += [flag, str(resource)]
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError, match="required"):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before and rig.calls == []


def test_mismatched_live_metadata_is_refused_without_stop(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch)
    atomic_json(rig.controller.metadata, {"pid": 45678, "project_cwd": "wrong project", "workspace_dir": "wrong workspace"})
    before = fingerprint(tmp_path)
    with pytest.raises(service.ServiceError, match="Mismatched"):
        cleanup.reset(rig.controller, rig.paths)
    assert fingerprint(tmp_path) == before and rig.calls == []


def test_cli_yes_and_no_restart_run_once_without_confirmation_prompt(tmp_path, monkeypatch):
    rig = seeded(tmp_path, monkeypatch, running=True)
    monkeypatch.setattr(cleanup, "Controller", lambda **unused: rig.controller)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("--yes must not prompt"))
    assert cleanup.main(["--yes", "--no-restart"], root=rig.root, settings=rig.settings) == 0
    assert [item[0] for item in rig.calls] == ["stop"] and not rig.state["running"]
    assert_fresh(rig)
