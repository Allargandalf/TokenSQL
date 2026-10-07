from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from tokensql.cli import (
    CliRuntimeError,
    _resolve_alembic_paths,
    _upgrade_database,
    app,
)


runner = CliRunner()


def _profile_result() -> SimpleNamespace:
    price_kind = SimpleNamespace(value="discount")
    quote = SimpleNamespace(
        parsed_price=SimpleNamespace(kind=price_kind),
        needs_review=False,
    )
    sheet = SimpleNamespace(
        title="报价",
        max_row=2,
        max_column=3,
        header_row=1,
        data_row_count=1,
        merge_ranges=(),
        needs_review=False,
        review_reasons=(),
    )
    workbook = SimpleNamespace(
        source_name="quotes.xlsx",
        sha256="a" * 64,
        byte_size=123,
        needs_review=False,
        review_reasons=(),
        sheets=(sheet,),
    )
    return SimpleNamespace(workbook=workbook, staged_quotes=(quote,))


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "db-upgrade",
        "bootstrap-catalog",
        "profile-xlsx",
        "import-xlsx",
        "official-refresh",
        "serve-api",
        "run-ui",
    ):
        assert command in result.stdout


def test_profile_xlsx_is_preview_only_and_can_emit_json(tmp_path: Path) -> None:
    workbook = tmp_path / "quotes.xlsx"
    workbook.write_bytes(b"placeholder")
    profile = object()

    with (
        patch("tokensql.cli._build_profile", return_value=profile),
        patch("tokensql.cli._profile_workbook", return_value=_profile_result()) as call,
    ):
        result = runner.invoke(app, ["profile-xlsx", str(workbook), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["staged_quote_count"] == 1
    assert payload["price_kind_counts"] == {"discount": 1}
    call.assert_called_once_with(workbook.resolve(), profile)


def test_import_xlsx_requires_source_id_and_profile(tmp_path: Path) -> None:
    workbook = tmp_path / "quotes.xlsx"
    workbook.write_bytes(b"placeholder")
    profile = tmp_path / "profile.json"
    profile.write_text('{"header_row": 1}', encoding="utf-8")

    missing_source = runner.invoke(
        app, ["import-xlsx", str(workbook), "--profile", str(profile)]
    )
    missing_profile = runner.invoke(
        app, ["import-xlsx", str(workbook), "--source-id", "1", "--provider-id", "2"]
    )

    assert missing_source.exit_code != 0
    assert "--source-id" in missing_source.output
    assert missing_profile.exit_code != 0
    assert "--profile" in missing_profile.output


def test_import_xlsx_passes_reviewed_context_without_starting_services(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "quotes.xlsx"
    workbook.write_bytes(b"placeholder")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"header_row": 1}', encoding="utf-8")
    profile = object()

    with (
        patch("tokensql.cli._load_profile_file", return_value=profile),
        patch("tokensql.cli._import_workbook", return_value="batch=7") as call,
    ):
        result = runner.invoke(
            app,
            [
                "import-xlsx",
                str(workbook),
                "--source-id",
                "42",
                "--provider-id",
                "7",
                "--profile",
                str(profile_path),
                "--parser-version",
                "test-v1",
            ],
        )

    assert result.exit_code == 0
    assert "batch=7" in result.stdout
    call.assert_called_once_with(
        path=workbook.resolve(),
        source_id=42,
        profile=profile,
        provider_id=7,
        parser_version="test-v1",
        database_url=None,
    )


def test_bootstrap_catalog_is_mocked(tmp_path: Path) -> None:
    document = tmp_path / "catalog.json"
    document.write_text('{"schema_version": 1}', encoding="utf-8")
    result_value = SimpleNamespace(
        to_dict=lambda: {
            "created": {"data_sources": 1},
            "reused": {"data_sources": 0},
            "ids": {"data_sources": {"source": 7}},
        }
    )
    database_url = f"sqlite+pysqlite:///{tmp_path / 'catalog.db'}"
    with patch(
        "tokensql.cli._bootstrap_catalog", return_value=result_value
    ) as bootstrap:
        result = runner.invoke(
            app,
            [
                "bootstrap-catalog",
                "--file",
                str(document),
                "--database-url",
                database_url,
            ],
        )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["ids"]["data_sources"]["source"] == 7
    bootstrap.assert_called_once_with(document.resolve(), database_url)


def test_bootstrap_catalog_runs_on_fresh_migrated_database(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "bootstrap-cli.db"
    database_url = f"sqlite+pysqlite:///{database}"
    _upgrade_database(
        database_url,
        "head",
        project_root / "alembic.ini",
    )

    arguments = [
        "bootstrap-catalog",
        "--file",
        str(project_root / "examples" / "bootstrap-catalog.json"),
        "--database-url",
        database_url,
    ]
    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert sum(json.loads(first.stdout)["created"].values()) == 9
    assert sum(json.loads(second.stdout)["created"].values()) == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM data_source"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM pricing_plan"
        ).fetchone()[0] == 1


def test_db_upgrade_is_mocked(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
    with patch("tokensql.cli._upgrade_database") as upgrade:
        result = runner.invoke(
            app,
            ["db-upgrade", "--database-url", database_url],
        )

    assert result.exit_code == 0
    upgrade.assert_called_once_with(database_url, "head", None)


def test_alembic_path_falls_back_to_source_tree(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path, migrations_path = _resolve_alembic_paths()
    project_root = Path(__file__).resolve().parents[1]

    assert config_path == project_root / "alembic.ini"
    assert migrations_path == project_root / "alembic"


def _sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        return {row[0] for row in rows}


def test_db_upgrade_runs_real_migration_from_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "migrated.db"
    monkeypatch.chdir(project_root)

    _upgrade_database(f"sqlite+pysqlite:///{database}", "head")

    assert {"alembic_version", "manufacturer", "price_snapshot"} <= _sqlite_tables(
        database
    )


def test_db_upgrade_explicit_url_wins_over_settings_url(
    tmp_path: Path, monkeypatch
) -> None:
    from tokensql.config import get_settings

    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    intended = tmp_path / "intended.db"
    configured = tmp_path / "configured.db"
    monkeypatch.setenv(
        "TOKENSQL_DATABASE_URL", f"sqlite+pysqlite:///{configured}"
    )
    get_settings.cache_clear()
    try:
        _upgrade_database(
            f"sqlite+pysqlite:///{intended}",
            "head",
            project_root / "alembic.ini",
        )
    finally:
        get_settings.cache_clear()

    assert "alembic_version" in _sqlite_tables(intended)
    assert not configured.exists()


def test_alembic_empty_url_falls_back_to_settings(
    tmp_path: Path, monkeypatch
) -> None:
    from alembic import command
    from alembic.config import Config
    from tokensql.config import get_settings

    project_root = Path(__file__).resolve().parents[1]
    fallback = tmp_path / "fallback.db"
    monkeypatch.setenv(
        "TOKENSQL_DATABASE_URL", f"sqlite+pysqlite:///{fallback}"
    )
    get_settings.cache_clear()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    try:
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    assert "alembic_version" in _sqlite_tables(fallback)


def test_serve_api_is_mocked_and_does_not_start_server() -> None:
    with patch("tokensql.cli._run_api") as run_api:
        result = runner.invoke(
            app,
            ["serve-api", "--host", "127.0.0.1", "--port", "9000"],
        )

    assert result.exit_code == 0
    run_api.assert_called_once_with("127.0.0.1", 9000, False)


def test_run_ui_is_mocked_and_does_not_start_server() -> None:
    with patch("tokensql.cli._run_ui") as run_ui:
        result = runner.invoke(
            app,
            ["run-ui", "--host", "127.0.0.1", "--port", "8600"],
        )

    assert result.exit_code == 0
    run_ui.assert_called_once_with("127.0.0.1", 8600)


def test_missing_optional_module_is_reported_cleanly() -> None:
    with patch(
        "tokensql.cli._run_api",
        side_effect=CliRuntimeError(
            "FastAPI application is unavailable because 'tokensql.api.app' "
            "could not be loaded."
        ),
    ):
        result = runner.invoke(app, ["serve-api"])

    assert result.exit_code == 2
    assert "tokensql.api.app" in result.output
    assert "Traceback" not in result.output
