"""Acceptance tests for strict catalog bootstrap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from tokensql.cli import _upgrade_database
from tokensql.config import get_settings
from tokensql.db.models import DataSource, ImportBatch, Provider
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.importers import ParserProfile
from tokensql.services.bootstrap_service import (
    BootstrapService,
    BootstrapValidationError,
)
from tokensql.services.import_service import ExcelImportService, ImportContext


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT_ROOT / "examples" / "bootstrap-catalog.json"


@pytest.fixture
def migrated_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "bootstrap.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("TOKENSQL_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        _upgrade_database(url, "head", PROJECT_ROOT / "alembic.ini")
        engine = create_db_engine(url)
        factory = create_session_factory(engine)
        yield factory
        engine.dispose()
    finally:
        get_settings.cache_clear()


def _workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Prices"
    sheet.append(["模型名称", "报价或折扣（最低）"])
    sheet.append(["example-model-api", "1元/秒"])
    workbook.save(path)
    workbook.close()


def test_bootstrap_is_idempotent_and_enables_import(migrated_factory, tmp_path: Path) -> None:
    with migrated_factory() as session:
        first = BootstrapService(session).apply_file(EXAMPLE)
    with migrated_factory() as session:
        second = BootstrapService(session).apply_file(EXAMPLE)
        assert sum(first.created.values()) == 9
        assert sum(second.created.values()) == 0
        assert sum(second.reused.values()) == 9
        source_id = second.ids["data_sources"]["supplier-xlsx"]
        provider_id = second.ids["providers"]["example-provider"]

    workbook = tmp_path / "quote.xlsx"
    _workbook(workbook)
    with migrated_factory() as session:
        summary = ExcelImportService().import_file(
            session,
            workbook,
            ImportContext(
                data_source_id=source_id,
                provider_id=provider_id,
                parser_version="bootstrap-e2e-v1",
                profile=ParserProfile(),
                vendor_context={"bootstrap_test": True},
            ),
        )
        session.commit()
        assert summary.total_rows == 1
        assert session.scalar(select(func.count(ImportBatch.id))) == 1


def test_bootstrap_rejects_extra_fields_and_rolls_back_conflicts(
    migrated_factory,
) -> None:
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    with migrated_factory() as session:
        with pytest.raises(BootstrapValidationError, match="invalid bootstrap"):
            BootstrapService(session).apply(payload)

    with migrated_factory() as session:
        BootstrapService(session).apply_file(EXAMPLE)

    conflict = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    conflict["data_sources"].insert(
        0,
        {
            "slug": "must-rollback",
            "name": "Must roll back",
            "source_type": "manual",
            "authority_level": "internal",
        },
    )
    conflict["providers"][0]["provider_type"] = "official"
    with migrated_factory() as session:
        with pytest.raises(BootstrapValidationError, match="provider"):
            BootstrapService(session).apply(conflict)
        assert session.scalar(
            select(func.count(DataSource.id)).where(DataSource.name == "Must roll back")
        ) == 0
        assert session.scalar(select(func.count(Provider.id))) == 1


def test_bootstrap_rejects_unknown_slug_reference(migrated_factory) -> None:
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    payload["offerings"][0]["model_slug"] = "missing-model"
    with migrated_factory() as session:
        with pytest.raises(BootstrapValidationError, match="unknown model reference"):
            BootstrapService(session).apply(payload)
