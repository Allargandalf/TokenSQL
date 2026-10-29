from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from fastapi.testclient import TestClient

import tokensql
from tokensql.workspace import app as app_module
from tokensql.workspace.config import WorkspaceSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODULE_PREFIXES = (
    "tokensql.api",
    "tokensql.cli",
    "tokensql.config",
    "tokensql.db",
    "tokensql.importers",
    "tokensql.llm",
    "tokensql.matching",
    "tokensql.official",
    "tokensql.pricing",
    "tokensql.scheduler",
    "tokensql.services",
    "tokensql.ui",
)


class OfflineManager:
    """The minimum manager surface needed to assemble the app without OpenCode."""

    config = SimpleNamespace(
        max_files=1,
        max_file_bytes=1024,
        max_total_bytes=1024,
    )

    def __init__(self) -> None:
        self.stopped = False

    async def shutdown(self) -> None:
        self.stopped = True


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    source = str(PROJECT_ROOT / "src")
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not previous else source + os.pathsep + previous
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _workspace_cli(database: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tokensql.workspace.cli",
            "--db",
            str(database),
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=_python_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_create_app_starts_with_only_empty_workspace_state(
    tmp_path: Path, monkeypatch
) -> None:
    # These deliberately invalid legacy-looking files prove startup does not scan
    # or try to migrate the former repository test-data location.
    legacy = tmp_path / "test"
    legacy.mkdir()
    (legacy / "legacy.xlsx").write_bytes(b"not an Excel workbook")
    (legacy / "legacy.sqlite3").write_bytes(b"not a SQLite database")
    monkeypatch.chdir(tmp_path)

    workspace = tmp_path / "workspace"
    manager = OfflineManager()
    api = app_module.create_app(
        WorkspaceSettings(workspace_dir=workspace),
        agent_manager=manager,
    )
    with TestClient(api) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "application": "tokensql-workspace",
        }
        assert api.version == "2.4.0"

    assert manager.stopped
    database = workspace / "quotes.sqlite3"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected = {
            "pricing_vendor", "pricing_model", "pricing_alias",
            "pricing_official", "pricing_supplier", "pricing_channel",
            "pricing_quote", "pricing_audit", "pricing_import",
        }
        assert expected <= tables
        assert {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in expected
        } == {table: 0 for table in expected}

    inbox = workspace / "inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


def test_workspace_cli_schema_list_and_removed_legacy_command(tmp_path: Path) -> None:
    database = tmp_path / "workspace" / "quotes.sqlite3"

    schema_result = _workspace_cli(database, "schema")
    assert schema_result.returncode == 0, schema_result.stderr
    schema = json.loads(schema_result.stdout)
    assert "quote" in schema
    assert set(schema["apply"]) == {"models", "official_prices", "create", "update", "delete"}

    list_result = _workspace_cli(database, "list")
    assert list_result.returncode == 0, list_result.stderr
    result = json.loads(list_result.stdout)
    assert result["items"] == [] and result["total"] == 0
    assert "filter_options" in result

    legacy_result = _workspace_cli(
        database, "import-legacy", str(tmp_path / "old.sqlite3")
    )
    assert legacy_result.returncode != 0
    assert "invalid choice" in legacy_result.stderr


def test_package_and_project_versions_are_2_3_0() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert tokensql.__version__ == "2.4.0"
    assert project["project"]["version"] == "2.4.0"


def test_supported_entrypoints_import_without_legacy_modules() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = project["project"].get("scripts", {})
    assert scripts
    assert all(target.startswith("tokensql.workspace.") for target in scripts.values())

    code = f"""
import importlib
import json
import sys

targets = {list(scripts.values())!r}
for target in targets:
    module_name, separator, attribute = target.partition(":")
    assert separator and attribute
    getattr(importlib.import_module(module_name), attribute)
importlib.import_module("tokensql.workspace.app")
prefixes = {LEGACY_MODULE_PREFIXES!r}
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)
print(json.dumps(loaded))
raise SystemExit(bool(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=_python_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
