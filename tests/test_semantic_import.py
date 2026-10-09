"""Mock-model acceptance tests for the Qwen semantic import pipeline.

Every gateway interaction runs through ``httpx.MockTransport``; no real model
request is ever issued. The catalog baseline comes from
``examples/bootstrap-catalog.json`` and workbooks are tiny openpyxl fixtures.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    FXRate,
    ImportBatch,
    ImportRow,
    PriceComponent,
    PriceNormalization,
    PriceSnapshot,
    ReviewItem,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.llm.client import QwenClientConfig
from tokensql.llm.semantic import SemanticQwenClient
from tokensql.services.bootstrap_service import BootstrapService
from tokensql.services.import_service import ImportContext
from tokensql.services.pricing_service import PricingService
from tokensql.services.semantic_import_service import (
    SemanticExcelImportService,
    SemanticImportError,
)

MODEL = "qwen3.8-flash-next-iq3_s"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_DOCUMENT = PROJECT_ROOT / "examples" / "bootstrap-catalog.json"

DEFAULT_PROMPT = "厂商 AlphaLabs；供应商 ResellerOne；币种 CNY；每百万 Token"


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture
def catalog(session: Session) -> dict[str, dict[str, int]]:
    result = BootstrapService(session).apply_file(BOOTSTRAP_DOCUMENT)
    return result.ids


class MockGateway:
    """Scripted OpenAI-compatible gateway behind httpx.MockTransport."""

    def __init__(
        self,
        *,
        understanding: Any,
        row_batches: list[Any] | None = None,
    ) -> None:
        self.understanding = understanding
        self.row_batches = list(row_batches or [])
        self.counts = {"models": 0, "table": 0, "rows": 0}

    @property
    def chat_calls(self) -> int:
        return self.counts["table"] + self.counts["rows"]

    def client(self) -> SemanticQwenClient:
        return SemanticQwenClient(
            QwenClientConfig(model=MODEL),
            transport=httpx.MockTransport(self._handler),
        )

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            self.counts["models"] += 1
            return httpx.Response(
                200,
                json={"data": [{"id": MODEL, "status": "loaded"}]},
            )
        payload = json.loads(request.content)
        name = payload["response_format"]["json_schema"]["name"]
        if name == "TableUnderstandingModel":
            self.counts["table"] += 1
            return self._respond(self.understanding)
        assert name == "RowExtractionBatchModel"
        self.counts["rows"] += 1
        if not self.row_batches:
            raise AssertionError("unexpected row extraction request")
        return self._respond(self.row_batches.pop(0))

    @staticmethod
    def _respond(item: Any) -> httpx.Response:
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, Exception):
            raise item
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "model": MODEL,
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(item, ensure_ascii=False)
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 240,
                    "total_tokens": 360,
                    "prompt_tokens_details": {"cached_tokens": 96},
                },
            },
        )


def _sheet(title: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sheet": title,
        "confidence": 0.9,
        "column_roles": [],
        "evidence": [],
    }
    data.update(overrides)
    return data


def _understanding(*sheets: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "sheets": list(sheets), "notes": None}


def _row_batch(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "rows": list(rows)}


def _row(row_number: int, components: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "row_number": row_number,
        "components": components,
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


def _monetary(
    amount: str,
    *,
    evidence: list[str],
    direction: str,
    currency: str | None = "CNY",
    unit_name: str | None = "百万token",
    unit_quantity: str = "1000000",
    dimension: str = "token",
    **overrides: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "component_type": "monetary",
        "dimension": dimension,
        "direction": direction,
        "raw_amount": amount,
        "unit_name": unit_name,
        "unit_quantity": unit_quantity,
        "evidence": evidence,
    }
    if currency is not None:
        data["currency"] = currency
    data.update(overrides)
    return data


def _discount(rate: str, *, evidence: list[str], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "component_type": "discount",
        "discount_rate": rate,
        "evidence": evidence,
    }
    data.update(overrides)
    return data


def _write_workbook(
    path: Path, sheets: dict[str, list[list[Any]]]
) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    first = True
    for title, rows in sheets.items():
        sheet = workbook.active if first else workbook.create_sheet()
        sheet.title = title
        for row in rows:
            sheet.append(row)
        first = False
    workbook.save(path)
    return path


def _quote_workbook(path: Path) -> Path:
    return _write_workbook(
        path,
        {
            "S1": [
                ["模型名称", "输入价格", "输出价格"],
                ["Alpha-1", "2.00", "6.00"],
            ]
        },
    )


def _context(
    catalog: dict[str, dict[str, int]],
    *,
    prompt: str = DEFAULT_PROMPT,
    defaults: dict[str, Any] | None = None,
) -> ImportContext:
    return ImportContext(
        data_source_id=catalog["data_sources"]["supplier-xlsx"],
        parser_version="semantic-v1",
        prompt_text=prompt,
        context_defaults=dict(defaults or {}),
    )


def _service(gateway: MockGateway) -> SemanticExcelImportService:
    return SemanticExcelImportService(
        semantic_client=gateway.client(), min_confidence=0.85
    )


def _staged_for(session: Session, batch_id: int) -> StagedQuote:
    return session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == batch_id)
    )


def _reasons(session: Session, staged_id: int) -> list[str]:
    return list(
        session.scalars(
            select(ReviewItem.reason).where(ReviewItem.staged_quote_id == staged_id)
        )
    )


def _happy_row(**overrides: Any) -> dict[str, Any]:
    return _row(
        2,
        [
            _monetary("2.00", direction="input", evidence=["S1!B2"]),
            _monetary("6.00", direction="output", evidence=["S1!C2"]),
        ],
        model_name="Alpha-1",
        manufacturer="AlphaLabs",
        provider="ResellerOne",
        **overrides,
    )


def test_happy_path_publishes_verified_snapshot(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(
            _sheet("S1", currency="CNY", unit_basis="百万token")
        ),
        row_batches=[_row_batch(_happy_row())],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.total_rows == 1
    assert summary.accepted_rows == 1
    assert summary.review_rows == 0
    assert summary.status == "completed"
    assert gateway.counts["models"] == 1

    staged = _staged_for(session, summary.batch_id)
    assert staged.review_status == "approved"
    assert staged.match_method == "qwen"
    assert staged.validation_errors == []

    snapshot = session.scalar(select(PriceSnapshot))
    assert snapshot is not None
    assert snapshot.verification_status == "verified"
    assert snapshot.currency_code == "CNY"
    by_metric = {
        component.billing_metric.code: component
        for component in snapshot.components
    }
    assert set(by_metric) == {"input_tokens", "output_tokens"}
    assert by_metric["input_tokens"].raw_amount == Decimal("2.00")
    assert by_metric["input_tokens"].raw_unit_quantity == Decimal("1000000")
    assert by_metric["output_tokens"].raw_amount == Decimal("6.00")
    assert by_metric["output_tokens"].raw_unit_quantity == Decimal("1000000")
    assert all(
        component.raw_currency_code == "CNY" for component in snapshot.components
    )

    normalizations = session.scalars(select(PriceNormalization)).all()
    assert len(normalizations) == 2
    assert all(item.verification_status == "verified" for item in normalizations)
    assert all(item.fx_rate_id is None for item in normalizations)

    audit = session.scalar(
        select(AuditEvent).where(AuditEvent.action == "auto_publish_price")
    )
    assert audit is not None
    assert audit.actor.startswith("qwen-auto:")
    assert audit.entity_type == "price_snapshot"

    batch = session.get(ImportBatch, summary.batch_id)
    semantic = batch.import_context["semantic"]
    assert semantic["table_understanding"]["sheets"][0]["sheet"] == "S1"
    assert semantic["table_understanding_request_id"]
    assert semantic["table_understanding_usage"]["cached_tokens"] == 96


def test_low_confidence_row_goes_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[_row_batch(_happy_row(confidence=0.5))],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert summary.accepted_rows == 0
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert staged.review_status == "needs_review"
    assert any("low_confidence" in reason for reason in _reasons(session, staged.id))


def test_low_component_field_confidence_forces_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _happy_row(
                    confidence=0.95,
                    field_confidence={"model_name": 0.95, "components": 0.5},
                )
            )
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    reasons = _reasons(session, staged.id)
    assert any(
        reason.startswith("low_field_confidence:components") for reason in reasons
    )


def test_missing_currency_filled_from_context_defaults(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "2.00",
                            direction="input",
                            currency=None,
                            evidence=["S1!B2", "user_context"],
                        )
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                )
            )
        ],
    )
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, defaults={"currency": "CNY"}),
    )

    assert summary.accepted_rows == 1
    snapshot = session.scalar(select(PriceSnapshot))
    assert snapshot is not None
    assert snapshot.currency_code == "CNY"
    assert snapshot.components[0].raw_currency_code == "CNY"


def test_cell_currency_not_overridden_and_missing_fx_goes_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "2.00", direction="input", currency="USD",
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
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, defaults={"currency": "CNY"}),
    )

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert staged.extracted_quote["_auto_extraction"]["currency_code"] == "USD"
    assert "fx_missing" in _reasons(session, staged.id)


def test_verified_fx_rate_enables_auto_publish_with_fx_link(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    fx = PricingService(session).create_fx_rate(
        base_currency="USD",
        quote_currency="CNY",
        rate="7.0",
        rate_date=date.today(),
        data_source_id=catalog["data_sources"]["supplier-xlsx"],
        verification_status="verified",
    )
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "2.00", direction="input", currency="USD",
                            evidence=["S1!B2"],
                        ),
                        _monetary(
                            "6.00", direction="output", currency="USD",
                            evidence=["S1!C2"],
                        ),
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                )
            )
        ],
    )
    summary = _service(gateway).import_file(
        session,
        path,
        _context(catalog, defaults={"currency": "CNY"}),
    )

    assert summary.accepted_rows == 1
    snapshot = session.scalar(select(PriceSnapshot))
    assert snapshot is not None
    assert snapshot.currency_code == "USD"
    normalizations = session.scalars(select(PriceNormalization)).all()
    assert len(normalizations) == 2
    assert all(item.fx_rate_id == fx.id for item in normalizations)
    assert all(item.verification_status == "verified" for item in normalizations)


def test_evidence_outside_row_goes_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "2.00", direction="input", evidence=["S1!Z99"]
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

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert any(
        reason.startswith("evidence_invalid") for reason in _reasons(session, staged.id)
    )


def test_conflicting_extraction_goes_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _happy_row(conflicts=["B2 表头是输入价但数值疑似输出价"])
            )
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert any(
        reason.startswith("conflicts:") for reason in _reasons(session, staged.id)
    )


def test_row_extraction_timeout_sends_rows_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[httpx.ConnectTimeout("connect timed out")],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert gateway.counts["models"] == 1
    assert gateway.counts["table"] == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert staged.review_status == "needs_review"
    assert "model_timeout" in _reasons(session, staged.id)


def test_table_understanding_failure_records_failed_batch(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=httpx.Response(502, json={"error": "upstream"}),
        row_batches=[],
    )
    with pytest.raises(SemanticImportError, match="table understanding failed"):
        _service(gateway).import_file(session, path, _context(catalog))

    failed = session.scalar(
        select(ImportBatch).where(ImportBatch.status == "failed")
    )
    assert failed is not None
    assert failed.error_message
    assert "table understanding failed" in failed.error_message
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_idempotency_key_covers_prompt_and_skips_model_on_replay(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[_row_batch(_happy_row()), _row_batch(_happy_row())],
    )
    service = _service(gateway)
    context = _context(catalog)

    first = service.import_file(session, path, context)
    calls_after_first = gateway.chat_calls
    assert calls_after_first == 2

    replay = service.import_file(session, path, context)
    assert replay.reused_existing
    assert replay.batch_id == first.batch_id
    assert gateway.chat_calls == calls_after_first

    changed = service.import_file(
        session, path, _context(catalog, prompt=DEFAULT_PROMPT + "（复核）")
    )
    assert not changed.reused_existing
    assert changed.batch_id != first.batch_id
    assert gateway.chat_calls == calls_after_first + 2


def test_schema_violating_extraction_goes_to_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    broken = _row_batch(
        _row(
            2,
            [
                {
                    "component_type": "monetary",
                    "dimension": "token",
                    "direction": "input",
                    "raw_amount": "2.00",
                    "currency": "CNY",
                    "unit_name": "百万token",
                    "unit_quantity": "1000000",
                    "evidence": [],
                }
            ],
            model_name="Alpha-1",
            manufacturer="AlphaLabs",
            provider="ResellerOne",
        )
    )
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[broken],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0
    staged = _staged_for(session, summary.batch_id)
    assert "model_schema_invalid" in _reasons(session, staged.id)


def test_reextract_publishes_and_resolves_open_review(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(_happy_row(confidence=0.5)),
            _row_batch(_happy_row(confidence=0.97)),
        ],
    )
    service = _service(gateway)
    summary = service.import_file(session, path, _context(catalog))
    assert summary.review_rows == 1
    staged = _staged_for(session, summary.batch_id)
    open_reviews = session.scalars(
        select(ReviewItem).where(
            ReviewItem.staged_quote_id == staged.id, ReviewItem.status == "open"
        )
    ).all()
    assert open_reviews

    result = service.reextract_staged_quote(session, staged.id, actor="reviewer")

    assert result.id == staged.id
    assert result.review_status == "approved"
    snapshot = session.scalar(select(PriceSnapshot))
    assert snapshot is not None
    assert snapshot.verification_status == "verified"
    for item in open_reviews:
        assert item.status == "resolved"
        assert item.resolution == {"action": "reextracted"}
        assert item.resolved_by == "reviewer"


def test_multi_sheet_understanding_publishes_both_rows(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _write_workbook(
        tmp_path / "multi.xlsx",
        {
            "S1": [["模型名称", "输入价格"], ["Alpha-1", "2.00"]],
            "S2": [["模型名称", "输入价格"], ["Alpha-2", "0.005"]],
        },
    )
    gateway = MockGateway(
        understanding=_understanding(
            _sheet("S1", currency="CNY", unit_basis="百万token"),
            _sheet("S2", currency="CNY", unit_basis="千token"),
        ),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "2.00", direction="input", unit_name=None,
                            unit_quantity="1", evidence=["S1!B2"],
                        )
                    ],
                    model_name="Alpha-1",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                )
            ),
            _row_batch(
                _row(
                    2,
                    [
                        _monetary(
                            "0.005", direction="input", unit_name=None,
                            unit_quantity="1000", evidence=["S2!B2"],
                        )
                    ],
                    model_name="Alpha-2",
                    manufacturer="AlphaLabs",
                    provider="ResellerOne",
                )
            ),
        ],
    )
    summary = _service(gateway).import_file(session, path, _context(catalog))

    assert summary.total_rows == 2
    assert summary.accepted_rows == 2
    assert summary.review_rows == 0
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 2
    quantities = sorted(
        component.raw_unit_quantity
        for component in session.scalars(select(PriceComponent))
    )
    assert quantities == [Decimal("1000"), Decimal("1000000")]


def test_discount_component_publishes_with_discount_rate(
    session: Session, catalog: dict[str, dict[str, int]], tmp_path: Path
) -> None:
    path = _quote_workbook(tmp_path / "quote.xlsx")
    gateway = MockGateway(
        understanding=_understanding(_sheet("S1", currency="CNY")),
        row_batches=[
            _row_batch(
                _row(
                    2,
                    [
                        _monetary("2.00", direction="input", evidence=["S1!B2"]),
                        _discount(
                            "0.3",
                            evidence=["S1!C2"],
                            dimension="token",
                            direction="input",
                        ),
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
    snapshot = session.scalar(select(PriceSnapshot))
    assert snapshot is not None
    discounts = [
        component
        for component in snapshot.components
        if component.component_type == "discount"
    ]
    assert len(discounts) == 1
    assert discounts[0].discount_rate == Decimal("0.3")
