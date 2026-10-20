import os
from pathlib import Path
import shutil
import subprocess

import pytest
from tokensql.workspace.config import WorkspaceSettings
from tokensql.workspace.sandbox import sandbox_command


def settings_for(tmp_path):
    return WorkspaceSettings(workspace_dir=tmp_path / "formal",
        agent_dir=tmp_path / "worker", input_dir=tmp_path / "input",
        progress_dir=tmp_path / "progress", data_dir=tmp_path / "data")


def test_worker_must_not_live_in_repository():
    from tokensql.workspace.config import PROJECT_ROOT
    settings = WorkspaceSettings(agent_dir=PROJECT_ROOT / "var" / "agent")
    with pytest.raises(ValueError, match="outside"):
        sandbox_command(settings, ["/bin/true"], unsafe=True)


def test_sandbox_missing_requires_explicit_opt_out(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="bubblewrap"):
        sandbox_command(settings, ["/bin/true"])
    assert sandbox_command(settings, ["/bin/true"], unsafe=True) == ["/bin/true"]


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")
def test_real_sandbox_blocks_host_write_and_hides_database(tmp_path):
    settings = settings_for(tmp_path)
    settings.workspace_dir.mkdir()
    secret = settings.workspace_dir / "secret"
    secret.write_text("formal secret")
    host = tmp_path / "host"
    host.write_text("preserve")
    script = 'test ! -e "$1" && (echo changed > "$2" || true) && echo draft > runs/probe'
    command = sandbox_command(settings, ["/bin/sh", "-c", script, "test",
                                       str(secret), str(host)])
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert host.read_text() == "preserve"
    assert secret.read_text() == "formal secret"
    assert (settings.agent_dir / "runs" / "probe").read_text().strip() == "draft"
