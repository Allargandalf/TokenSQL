"""Narrow sandbox and optional comparison regressions added by the integrator."""
from copy import deepcopy
import shutil

import pytest

from tokensql.workspace.compare import compare_snapshots


@pytest.mark.parametrize("field", ["unit", "billing_item", "supplier"])
def test_identical_blank_optional_price_fields_are_not_validation_errors(field):
    value = {
        "models": [{"model_id": "partial-a"}],
        "quotes": [{"model_id": "partial-a", "amount": "1", "discount_rate": "0.8",
                    "currency": "CNY", "unit": "token", "unit_quantity": "1000"}],
    }
    value["quotes"][0][field] = ""
    assert compare_snapshots(value, deepcopy(value))["pass"]


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")
def test_maintenance_backups_are_explicitly_hidden_from_the_worker(tmp_path, monkeypatch):
    from tokensql.workspace import sandbox
    from tokensql.workspace.config import WorkspaceSettings
    project = tmp_path / "repo"
    project.mkdir()
    backup = tmp_path / "TokenSQL-maintenance-backups"
    backup.mkdir()
    monkeypatch.setattr(sandbox, "PROJECT_ROOT", project)
    settings = WorkspaceSettings(workspace_dir=tmp_path / "formal", agent_dir=tmp_path / "worker",
                                 input_dir=tmp_path / "input", progress_dir=tmp_path / "progress",
                                 data_dir=tmp_path / "data")
    command = sandbox.sandbox_command(settings, ["/bin/true"])
    assert any(command[index:index + 2] == ["--tmpfs", str(backup)]
               for index in range(len(command) - 1))
