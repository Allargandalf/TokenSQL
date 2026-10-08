"""Regression tests for the 2026-10-07 semantic import review fixes.

Covers: thousand-token unit scaling, provider/manufacturer context conflicts,
catalog write gating for review rows, publish IntegrityError demotion, CLI
semantic routing, prompt propagation into model requests, the process-wide
shared in-flight semaphore, and lenient chat-completion envelope parsing.

All gateway traffic stays behind ``httpx.MockTransport``; no real model or
network access happens. Fixtures and payload helpers are reused from
``tests.test_semantic_import``.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner
from sqlalchemy.orm import Session

from tokensql.config import Settings, get_settings
from tokensql.db.models import (
    ImportRow,
    Manufacturer,
    Offering,
    PriceComponent,
    PriceNormalization,
    PriceSnapshot,
    StagedQuote,
    StandardModel,
)
from tokensql.llm.client import QwenClientConfig
from tokensql.llm.extraction_schemas import TableUnderstandingModel
from tokensql.llm.semantic import SemanticQwenClient

from tests.test_semantic_import import (
    MODEL,
    MockGateway,
    _context,
    _monetary,
    _row,
    _row_batch,
    _service,
    _sheet,
    _staged_for,
    _understanding,
    _write_workbook,
    catalog,
    session,
)

runner = CliRunner()


# --------------------------------------------------------------------- #
# 1. thousand-token unit scaling on auto-published components
# --------------------------------------------------------------------- #


def test_thousand_token_unit_scaled_to_per_million(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _write_workbook(
        tmp_path / "quote.xlsx",
        {"S1": [["模型名称", "输入价格"], ["Alpha-1", "0.005"]]},
    )
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "0.005",
                            direction="input",
                            unit_name="千token",
                            unit_quantity="1",
                            evidence=["S1!B2"],
                        )
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                )
            )
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.accepted_rows == 1
    component = session.scalar(select(PriceComponent))
    assert component is not None
    assert component.raw_unit_name == "token"
    assert component.raw_unit_quantity == Decimal("1000")
    assert component.raw_amount == Decimal("0.005")

    normalization = session.scalar(select(PriceNormalization))
    assert normalization is not None
    assert normalization.normalized_amount == Decimal("5")
    assert normalization.normalized_unit_quantity == 1_000_000
    assert normalization.normalized_currency_code == "CNY"


# --------------------------------------------------------------------- #
# 2/3. context_defaults vs extraction provider/manufacturer conflicts
# --------------------------------------------------------------------- #


def test_provider_conflict_between_defaults_and_extraction(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    from tests.test_semantic_import import _quote_workbook

    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary("2.00", direction="input", evidence=["S1!B2"]),
                        _monetary("6.00", direction="output", evidence=["S1!C2"]),
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="AlphaReseller",
                )
            )
        ],
    )
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, defaults={"provider_name": "BetaReseller"}),
    )

    assert summary.review_rows == 1
    assert summary.accepted_rows == 0
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert staged.review_status == "needs_review"
    assert "provider_conflict" in staged.validation_errors


def test_manufacturer_conflict_between_defaults_and_extraction(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    from tests.test_semantic_import import _quote_workbook

    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary("2.00", direction="input", evidence=["S1!B2"]),
                        _monetary("6.00", direction="output", evidence=["S1!C2"]),
                    ],
                    model_name="Alpha-1",
                    manufacturer="CopycatMfg",
                    provider="ResellerOne",
                )
            )
        ],
    )
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, defaults={"manufacturer": "AlphaLabs"}),
    )

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert "manufacturer_conflict" in staged.validation_errors


# --------------------------------------------------------------------- #
# 4. review rows must not create catalog records
# --------------------------------------------------------------------- #


def test_low_confidence_row_creates_no_catalog_records(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    from tests.test_semantic_import import _happy_row, _quote_workbook

    def counts() -> dict[str, int]:
        return {
            model.__name__: int(
                session.scalar(select(func.count()).select_from(model))
            )
            for model in (Manufacturer, StandardModel, Offering)
        }

    before = counts()
    suggestions = {
        "standard_models": ["Alpha-1"],
        "providers": [{"name": "ResellerOne", "type": "reseller"}],
    }
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _happy_row(confidence=0.4, catalog_suggestions=suggestions)
            )
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert counts() == before
    staged = _staged_for(session, summary.batch_id)
    assert staged.review_status == "needs_review"
    model_match = staged.extracted_quote["model_match"]
    assert model_match["stage"] == "qwen_semantic"
    assert model_match["catalog_suggestions"] == suggestions


# --------------------------------------------------------------------- #
# 5. duplicate components -> publish IntegrityError demoted to review
# --------------------------------------------------------------------- #


def test_duplicate_component_publish_failure_keeps_batch_alive(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _write_workbook(
        tmp_path / "dup.xlsx",
        {
            "S1": [
                ["模型名称", "输入价格", "输入价格2"],
                ["Alpha-1", "2.00", "3.00"],
                ["Alpha-2", "1.00", ""],
            ]
        },
    )
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary("2.00", direction="input", evidence=["S1!B2"]),
                        _monetary("3.00", direction="input", evidence=["S1!C2"]),
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                ),
                _row(
                    3,
                    [_monetary("1.00", direction="input", evidence=["S1!B3"])],
                    model_name="Alpha-2",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                ),
            )
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.total_rows == 2
    assert summary.accepted_rows == 1
    assert summary.review_rows == 1

    stageds = session.scalars(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == summary.batch_id)
    ).all()
    by_model = {staged.extracted_model_name: staged for staged in stageds}
    duplicate = by_model["Alpha-1"]
    healthy = by_model["Alpha-2"]

    assert duplicate.review_status == "needs_review"
    assert any(
        error.startswith("publish_failed")
        for error in duplicate.validation_errors
    )
    assert healthy.review_status == "approved"

    snapshots = session.scalars(select(PriceSnapshot)).all()
    assert len(snapshots) == 1
    assert len(snapshots[0].components) == 1


# --------------------------------------------------------------------- #
# 6. CLI semantic routing driven by --prompt + settings
# --------------------------------------------------------------------- #


def _summary_stub() -> SimpleNamespace:
    return SimpleNamespace(
        batch_id=11,
        source_artifact_id=3,
        idempotency_key="excel:" + "1" * 64,
        status="completed",
        total_rows=1,
        accepted_rows=1,
        rejected_rows=0,
        review_rows=0,
        staged_quote_count=1,
        review_item_count=0,
        reused_existing=False,
    )


def _recording_stub(calls: list[Any], settings_box: dict[str, Any]) -> Any:
    class _Stub:
        def __init__(self, *, settings: Any = None, **kwargs: Any) -> None:
            settings_box["settings"] = settings

        def import_file(self, session: Any, path: Any, context: Any) -> Any:
            calls.append(context)
            return _summary_stub()

    return _Stub


@pytest.fixture
def qwen_cli_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOKENSQL_QWEN_ENABLED", "1")
    monkeypatch.setenv("TOKENSQL_QWEN_MODEL", "qwen3.8-flash-next-iq3_s")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()


def test_cli_routes_prompt_import_to_semantic_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qwen_cli_env: Settings
) -> None:
    from tokensql.cli import app

    workbook = tmp_path / "quote.xlsx"
    workbook.write_bytes(b"placeholder")
    semantic_calls: list[Any] = []
    deterministic_calls: list[Any] = []
    settings_box: dict[str, Any] = {}
    monkeypatch.setattr(
        "tokensql.services.semantic_import_service.SemanticExcelImportService",
        _recording_stub(semantic_calls, settings_box),
    )
    monkeypatch.setattr(
        "tokensql.services.import_service.ExcelImportService",
        _recording_stub(deterministic_calls, settings_box),
    )

    result = runner.invoke(
        app,
        [
            "import-xlsx",
            str(workbook),
            "--source-id",
            "42",
            "--prompt",
            "缺省币种 CNY",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(semantic_calls) == 1
    assert deterministic_calls == []
    context = semantic_calls[0]
    assert context.prompt_text == "缺省币种 CNY"
    assert context.interpretation_version == qwen_cli_env.qwen_extraction_version
    assert settings_box["settings"].qwen_enabled is True


def test_cli_without_prompt_uses_deterministic_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qwen_cli_env: Settings
) -> None:
    from tokensql.cli import app

    workbook = tmp_path / "quote.xlsx"
    workbook.write_bytes(b"placeholder")
    semantic_calls: list[Any] = []
    deterministic_calls: list[Any] = []
    settings_box: dict[str, Any] = {}
    monkeypatch.setattr(
        "tokensql.services.semantic_import_service.SemanticExcelImportService",
        _recording_stub(semantic_calls, settings_box),
    )
    monkeypatch.setattr(
        "tokensql.services.import_service.ExcelImportService",
        _recording_stub(deterministic_calls, settings_box),
    )

    result = runner.invoke(
        app,
        [
            "import-xlsx",
            str(workbook),
            "--source-id",
            "42",
            "--provider-id",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(deterministic_calls) == 1
    assert semantic_calls == []
    assert deterministic_calls[0].interpretation_version == "deterministic-v1"


# --------------------------------------------------------------------- #
# 7. prompt_text and context_defaults reach every model request
# --------------------------------------------------------------------- #


class _CapturingGateway(MockGateway):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.post_payloads: list[dict[str, Any]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.post_payloads.append(json.loads(request.content))
        return super()._handler(request)


def test_prompt_and_defaults_enter_both_model_requests(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    from tests.test_semantic_import import _happy_row, _quote_workbook

    prompt = "缺省币种 CNY；供应商 ResellerOne"
    defaults = {"currency": "CNY", "provider_name": "ResellerOne"}
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = _CapturingGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[_row_batch(_happy_row())],
    )
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, prompt=prompt, defaults=defaults),
    )
    assert summary.accepted_rows == 1

    by_schema = {
        payload["response_format"]["json_schema"]["name"]: payload
        for payload in gateway.post_payloads
    }
    assert set(by_schema) == {"TableUnderstandingModel", "RowExtractionBatchModel"}
    for name, payload in by_schema.items():
        user_payload = json.loads(payload["messages"][1]["content"])
        context = user_payload["context"]
        assert context["prompt_text"] == prompt, name
        assert context["context_defaults"]["currency"] == "CNY", name
        assert context["context_defaults"]["provider_name"] == "ResellerOne", name


# --------------------------------------------------------------------- #
# 8. one shared in-flight budget per base_url across client instances
# --------------------------------------------------------------------- #

_MIN_TABLE = {
    "schema_version": 1,
    "sheets": [
        {"sheet": "S1", "confidence": 0.9, "column_roles": [], "evidence": []}
    ],
    "notes": None,
}


def _table_completion() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-sem",
            "model": MODEL,
            "choices": [
                {"message": {"content": json.dumps(_MIN_TABLE, ensure_ascii=False)}}
            ],
        },
    )


def test_shared_semaphore_serializes_two_clients_on_same_base_url() -> None:
    base_url = "http://127.0.0.1:9/v1"
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return _table_completion()

    def worker() -> None:
        client = SemanticQwenClient(
            QwenClientConfig(base_url=base_url, model=MODEL, max_in_flight=1),
            transport=httpx.MockTransport(handler),
        )
        try:
            for _ in range(2):
                client.chat_json(
                    system="row-extraction-v1",
                    user="{}",
                    schema=TableUnderstandingModel,
                )
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["max_active"] == 1


# --------------------------------------------------------------------- #
# 9. unknown top-level envelope fields are tolerated
# --------------------------------------------------------------------- #


def test_unknown_envelope_fields_are_tolerated_by_chat_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-ext",
                "object": "chat.completion",
                "created": 1_760_000_000,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_MIN_TABLE, ensure_ascii=False),
                            "tool_calls": None,
                            "annotations": [],
                        },
                        "finish_reason": "stop",
                        "logprobs": None,
                        "token_ids": [1, 2, 3],
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
                "kv_transfer_params": {"num_computed_tokens": 7},
                "system_fingerprint": "fp-future",
                "some_future_top_level": {"nested": [1, 2]},
            },
        )

    client = SemanticQwenClient(
        QwenClientConfig(base_url="http://127.0.0.1:9/v2", model=MODEL),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.chat_json(
            system="table-understanding-v1",
            user="{}",
            schema=TableUnderstandingModel,
        )
    finally:
        client.close()

    assert result.model == MODEL
    assert result.data["sheets"][0]["sheet"] == "S1"
