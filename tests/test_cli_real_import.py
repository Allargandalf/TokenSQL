"""CLI import must pass an explicit provider into the real import service."""
import json
from pathlib import Path
import sqlite3
from typer.testing import CliRunner
from tokensql.cli import app

def test_real_workbook_cli_import_from_fresh_migrated_catalog(tmp_path):
    runner = CliRunner()
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "cli-import.db"
    url = f"sqlite+pysqlite:///{database}"
    migrated = runner.invoke(app, [
        "db-upgrade", "--database-url", url,
        "--alembic-config", str(root / "alembic.ini"),
    ])
    assert migrated.exit_code == 0, migrated.output
    seeded = runner.invoke(app, [
        "bootstrap-catalog", "--file", str(root / "examples/bootstrap-catalog.json"),
        "--database-url", url,
    ])
    assert seeded.exit_code == 0, seeded.output
    ids = json.loads(seeded.stdout)["ids"]
    profile = tmp_path / "profile.json"
    profile.write_text('{"discount_scale": 10}', encoding="utf-8")
    args = [
        "import-xlsx", str(root / "test/token折扣.xlsx"),
        "--source-id", str(ids["data_sources"]["supplier-xlsx"]),
        "--provider-id", str(ids["providers"]["example-provider"]),
        "--profile", str(profile), "--database-url", url,
    ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM import_batch").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM staged_quote").fetchone()[0] == 26
        assert connection.execute("SELECT count(*) FROM price_snapshot").fetchone()[0] == 0
        original = connection.execute("SELECT raw_content FROM source_artifact").fetchone()[0]
        assert original == (root / "test/token折扣.xlsx").read_bytes()
