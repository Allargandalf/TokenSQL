"""FastAPI boundary tests for semantic import routing and re-extraction."""

from __future__ import annotations

from io import BytesIO
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tokensql.api.app import create_app
from tokensql.api.dependencies import get_session
from tokensql.config import Settings
from tokensql.db.base import Base, utc_now
from tokensql.db.models import (
    DataSource,
    ImportBatch,
    ImportRow,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.services.import_service import ImportSummary

api_module = pytest.importorskip("tokensql.api.app")


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet.append(["模型名称", "输入价格"])
    sheet.append(["alpha", "2.00"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _seed(factory: sessionmaker) -> dict[str, int]:
    now = utc_now()
    with factory.begin() as session:
        source = DataSource(
            name="Semantic API fixture source",
            source_type="provider_excel",
            authority_level="secondary",
        )
        provider = Provider(
            name="ResellerOne", slug="resellerone", provider_type="reseller"
        )
        session.add_all([source, provider])
        session.flush()
        artifact = SourceArtifact(
            data_source=source,
            uri="urn:sha256:" + "b" * 64,
            original_filename="quote.xlsx",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            sha256="b" * 64,
            fetched_at=now,
        )
        session.add(artifact)
        session.flush()
        batch = ImportBatch(
            source_artifact=artifact,
            idempotency_key="excel:" + "c" * 64,
            parser_version="semantic-v1",
            status="partial",
            total_rows=1,
            review_rows=1,
            import_context={"prompt_text": "缺省币种 CNY"},
        )
        session.add(batch)
        session.flush()
        row = ImportRow(
            import_batch=batch,
            sheet_name="S1",
            row_number=2,
            row_hash="d" * 64,
            raw_data={"row_number": 2, "cells": []},
            status="staged",
        )
        session.add(row)
        session.flush()
        staged = StagedQuote(
            import_row=row,
            extracted_model_name="alpha",
            review_status="needs_review",
            extracted_quote={"schema_version": 3},
            validation_errors=["low_confidence:0.50"],
        )
        session.add(staged)
        session.flush()
        staged.review_items.append(
            ReviewItem(
                review_type="other",
                status="open",
                severity="warning",
                reason="low_confidence:0.50",
            )
        )
        session.flush()
        return {
            "source": source.id,
            "provider": provider.id,
            "staged": staged.id,
        }


@pytest.fixture
def api() -> tuple[TestClient, dict[str, int]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    ids = _seed(factory)
    application = create_app()

    def override_session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        yield client, ids
    application.dependency_overrides.clear()
    engine.dispose()


class _RecordingSemanticService:
    instances: list["_RecordingSemanticService"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.import_calls: list[tuple[object, object, object]] = []
        self.reextract_calls: list[tuple[int, str]] = []
        _RecordingSemanticService.instances.append(self)

    def import_file(self, session, path, context) -> ImportSummary:
        self.import_calls.append((session, path, context))
        return ImportSummary(
            batch_id=9001,
            source_artifact_id=1,
            idempotency_key="stub-key",
            status="completed",
            total_rows=1,
            accepted_rows=1,
            rejected_rows=0,
            review_rows=0,
            staged_quote_count=1,
            review_item_count=0,
        )

    def reextract_staged_quote(self, session, staged_quote_id, *, actor):
        from tokensql.db.models import StagedQuote as _StagedQuote

        self.reextract_calls.append((staged_quote_id, actor))
        staged = session.get(_StagedQuote, staged_quote_id)
        if staged is None:
            raise LookupError(f"staged quote {staged_quote_id} not found")
        return staged


class _RecordingDeterministicService:
    instances: list["_RecordingDeterministicService"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.import_calls: list[tuple[object, object, object]] = []
        _RecordingDeterministicService.instances.append(self)

    def import_file(self, session, path, context) -> ImportSummary:
        self.import_calls.append((session, path, context))
        return ImportSummary(
            batch_id=9002,
            source_artifact_id=1,
            idempotency_key="stub-key",
            status="partial",
            total_rows=1,
            accepted_rows=0,
            rejected_rows=0,
            review_rows=1,
            staged_quote_count=1,
            review_item_count=1,
        )


def test_prompt_routes_to_semantic_service_with_context(
    api, monkeypatch
) -> None:
    client, ids = api
    _RecordingSemanticService.instances.clear()
    settings = Settings(qwen_enabled=True, qwen_model="qwen3.8-flash-next-iq3_s")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_module, "SemanticExcelImportService", _RecordingSemanticService
    )

    prompt = "厂商 AlphaLabs；供应商 ResellerOne；币种 CNY；每百万 Token"
    response = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={
            "request": json.dumps(
                {
                    "data_source_id": ids["source"],
                    "parser_version": "semantic-api-v1",
                    "prompt_text": prompt,
                    "context_defaults": {
                        "manufacturer": "AlphaLabs",
                        "currency": "CNY",
                    },
                },
                ensure_ascii=False,
            )
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_id"] == 9001
    assert len(_RecordingSemanticService.instances) == 1
    service = _RecordingSemanticService.instances[0]
    assert len(service.import_calls) == 1
    _, _, context = service.import_calls[0]
    assert context.prompt_text == prompt
    assert context.context_defaults == {
        "manufacturer": "AlphaLabs",
        "currency": "CNY",
    }
    assert context.interpretation_version == settings.qwen_extraction_version


def test_empty_prompt_uses_deterministic_service(api, monkeypatch) -> None:
    client, ids = api
    _RecordingSemanticService.instances.clear()
    _RecordingDeterministicService.instances.clear()
    settings = Settings(qwen_enabled=True, qwen_model="qwen3.8-flash-next-iq3_s")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_module, "SemanticExcelImportService", _RecordingSemanticService
    )
    monkeypatch.setattr(
        api_module, "ExcelImportService", _RecordingDeterministicService
    )

    response = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={
            "request": json.dumps(
                {
                    "data_source_id": ids["source"],
                    "provider_id": ids["provider"],
                    "parser_version": "deterministic-api-v1",
                }
            )
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_id"] == 9002
    assert _RecordingSemanticService.instances == []
    assert len(_RecordingDeterministicService.instances) == 1
    _, _, context = _RecordingDeterministicService.instances[0].import_calls[0]
    assert context.prompt_text == ""
    assert context.interpretation_version == "deterministic-v1"


def test_reextract_endpoint_returns_staged_quote(api, monkeypatch) -> None:
    client, ids = api
    _RecordingSemanticService.instances.clear()
    monkeypatch.setattr(
        api_module, "SemanticExcelImportService", _RecordingSemanticService
    )
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: Settings(qwen_enabled=True, qwen_model="qwen3.8-flash-next-iq3_s"),
    )

    ok = client.post(
        f"/staged-quotes/{ids['staged']}/re-extract",
        json={"actor": "reviewer"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == ids["staged"]
    assert _RecordingSemanticService.instances[0].reextract_calls == [
        (ids["staged"], "reviewer")
    ]

    missing = client.post(
        "/staged-quotes/999999/re-extract",
        json={"actor": "reviewer"},
    )
    assert missing.status_code == 404

    blank = client.post(
        f"/staged-quotes/{ids['staged']}/re-extract",
        json={"actor": "   "},
    )
    assert blank.status_code == 422


def test_reextract_disabled_when_qwen_off(api, monkeypatch) -> None:
    client, ids = api
    _RecordingSemanticService.instances.clear()
    monkeypatch.setattr(
        api_module, "SemanticExcelImportService", _RecordingSemanticService
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: Settings())
    disabled = client.post(
        f"/staged-quotes/{ids['staged']}/re-extract",
        json={"actor": "reviewer"},
    )
    assert disabled.status_code == 409
    assert _RecordingSemanticService.instances == []


def test_semantic_import_error_maps_to_503(api, monkeypatch) -> None:
    client, ids = api

    class _FailingSemanticService(_RecordingSemanticService):
        def import_file(self, session, path, context):
            from tokensql.services.semantic_import_service import (
                SemanticImportError,
            )

            raise SemanticImportError("table understanding failed: http_status 502")

    settings = Settings(qwen_enabled=True, qwen_model="qwen3.8-flash-next-iq3_s")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_module, "SemanticExcelImportService", _FailingSemanticService
    )

    response = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={
            "request": json.dumps(
                {
                    "data_source_id": ids["source"],
                    "parser_version": "semantic-api-v1",
                    "prompt_text": "缺省币种 CNY",
                },
                ensure_ascii=False,
            )
        },
    )
    assert response.status_code == 503
    assert "table understanding failed" in response.json()["detail"]
