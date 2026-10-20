from __future__ import annotations

import json
from pathlib import Path
import signal

import pytest

from tokensql.workspace.config import WorkspaceSettings
from tokensql.workspace import service


def settings(workspace: Path) -> WorkspaceSettings:
    return WorkspaceSettings(
        _env_file=None,
        workspace_dir=workspace,
        test_mode=True,
    )


def controller(tmp_path, args=()):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return service.Controller(
        args,
        root=root,
        proc=tmp_path / "proc",
        settings=settings(tmp_path / "workspace"),
    )


def process(ctrl, *, pid=1234, starttime=99, token=None, argv=None):
    environment = {}
    if token is not None:
        environment["TOKENSQL_SERVICE_TOKEN"] = token
    return service.Process(
        pid=pid,
        starttime=starttime,
        state="S",
        cwd=ctrl.root,
        argv=tuple(argv or (
            str(ctrl.root / ".venv/bin/python"),
            "-m",
            service.MODULE,
            "--port",
            str(ctrl.spec.port),
            "--workspace-dir",
            str(ctrl.workspace),
        )),
        environ=environment,
    )


def test_option_and_workspace_identity_follow_runtime_last_value(tmp_path):
    ctrl = controller(
        tmp_path,
        ("--port", "8795", "--workspace-dir", str(tmp_path / "workspace")),
    )
    other = tmp_path / "other"
    item = process(
        ctrl,
        argv=(
            "python", "-m", service.MODULE,
            "--port", "8765", "--port", "8795",
            "--workspace-dir", str(other),
            "--workspace-dir", str(ctrl.workspace),
        ),
    )
    assert service.option(item.argv, "--port") == "8795"
    assert service.matches(
        item, ctrl.spec, ctrl.root, ctrl.settings.workspace_dir,
        ctrl.settings.agent_dir,
    )
    wrong = service.Process(
        item.pid, item.starttime, item.state, item.cwd, item.argv,
        {"TOKENSQL_WORKSPACE_DIR": str(other)},
    )
    explicit = tuple(value for value in wrong.argv if value != str(ctrl.workspace))
    wrong = service.Process(
        wrong.pid, wrong.starttime, wrong.state, wrong.cwd,
        explicit, wrong.environ,
    )
    assert not service.matches(
        wrong, ctrl.spec, ctrl.root, ctrl.settings.workspace_dir,
        ctrl.settings.agent_dir,
    )


def test_lock_rejects_concurrent_mutation(tmp_path):
    first = controller(tmp_path)
    second = controller(tmp_path)
    with first.lock():
        with pytest.raises(service.ServiceError, match="operation"):
            second.start()


def test_metadata_rejects_pid_reuse_and_wrong_token(tmp_path, monkeypatch):
    ctrl = controller(tmp_path)
    current = process(ctrl, token="secret")
    ctrl.workspace.mkdir(parents=True)
    ctrl.save(current, "secret")
    value = ctrl.load()
    monkeypatch.setattr(service, "read_process", lambda *args: current)
    assert ctrl.validate(value) == current


    reused = process(ctrl, starttime=current.starttime + 1, token="secret")
    monkeypatch.setattr(service, "read_process", lambda *args: reused)
    assert ctrl.validate(value) is None
    wrong_token = process(ctrl, token="different")
    monkeypatch.setattr(service, "read_process", lambda *args: wrong_token)
    assert ctrl.validate(value) is None


def test_start_adopts_once_without_spawning_duplicate(tmp_path, monkeypatch):
    ctrl = controller(tmp_path)
    current = process(ctrl)
    monkeypatch.setattr(ctrl, "candidates", lambda: [current])
    monkeypatch.setattr(
        ctrl, "spawn",
        lambda token: pytest.fail("an adopted runtime must not be spawned"),
    )
    assert ctrl.start() == 0
    value = json.loads(ctrl.metadata.read_text())
    assert value["pid"] == current.pid
    assert value["starttime"] == current.starttime

    monkeypatch.setattr(service, "read_process", lambda *args: current)
    monkeypatch.setattr(
        ctrl, "candidates",
        lambda: pytest.fail("managed process should be recognized first"),
    )
    assert ctrl.start() == 0


def test_foreign_port_is_refused_without_a_signal_or_data_change(
    tmp_path, monkeypatch
):
    ctrl = controller(tmp_path)
    ctrl.workspace.mkdir(parents=True)
    business = {
        name: ctrl.workspace / name
        for name in ("quotes.sqlite3", "quotes.sqlite3-wal", "quotes.sqlite3-shm")
    }
    for path in business.values():
        path.write_bytes(path.name.encode())
    before = {name: path.read_bytes() for name, path in business.items()}
    monkeypatch.setattr(ctrl, "candidates", lambda: [])
    monkeypatch.setattr(ctrl, "port_available", lambda *args: False)
    monkeypatch.setattr(
        service.os, "kill", lambda *args: pytest.fail("foreign process was signalled")
    )

    with pytest.raises(service.ServiceError, match="foreign"):
        ctrl.start()
    assert {name: path.read_bytes() for name, path in business.items()} == before


def test_stop_is_term_only_repeatable_and_preserves_business_files(
    tmp_path, monkeypatch
):
    ctrl = controller(tmp_path)
    current = process(ctrl, token="service-token")
    ctrl.workspace.mkdir(parents=True)
    database = ctrl.workspace / "quotes.sqlite3"
    attachment = ctrl.workspace / "attachment.xlsx"
    database.write_bytes(b"database")
    attachment.write_bytes(b"attachment")
    ctrl.save(current, "service-token")
    alive = True
    signals = []

    def read(*args):
        return current if alive else None

    def kill(pid, signum):
        nonlocal alive
        signals.append((pid, signum))
        alive = False

    monkeypatch.setattr(service, "read_process", read)
    monkeypatch.setattr(service.os, "kill", kill)
    assert ctrl.stop() == 0
    monkeypatch.setattr(ctrl, "candidates", lambda: [])
    assert ctrl.stop() == 0
    assert signals == [(current.pid, signal.SIGTERM)]
    assert database.read_bytes() == b"database"
    assert attachment.read_bytes() == b"attachment"
