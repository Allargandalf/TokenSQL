"""Transactional Excel staging service acceptance tests."""

from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    DataSource,
    ImportBatch,
    ImportRow,
    PriceSnapshot,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.importers import ParserProfile
from tokensql.llm import QwenResult, QwenSelection, ReviewReason
from tokensql.services import ExcelImportService, ImportContext
from tokensql.services.catalog_service import CatalogService
from tokensql.services.import_service import normalize_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "test"


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture
def source_and_provider(session: Session) -> tuple[int, int]:
    source = DataSource(
        name="Excel staging tests",
        source_type="provider_excel",
        authority_level="secondary",
    )
    provider = Provider(
        name="Fixture provider",
        slug="fixture-provider",
        provider_type="reseller",
    )
    session.add_all([source, provider])
    session.flush()
    ids = (source.id, provider.id)
    session.commit()
    return ids


def _context(
    ids: tuple[int, int],
    *,
    version: str = "parser-v1",
    profile: ParserProfile | None = None,
    provider: bool = True,
) -> ImportContext:
    source_id, provider_id = ids
    return ImportContext(
        data_source_id=source_id,
        provider_id=provider_id if provider else None,
        parser_version=version,
        profile=profile or ParserProfile(),
        vendor_context={"supplier_account": "fixture"},
    )


def _reasons(session: Session, batch_id: int) -> Counter[str]:
    reasons = session.scalars(
        select(ReviewItem.reason)
        .join(StagedQuote, ReviewItem.staged_quote_id == StagedQuote.id)
        .join(ImportRow, StagedQuote.import_row_id == ImportRow.id)
        .where(ImportRow.import_batch_id == batch_id)
    ).all()
    return Counter(reasons)


def test_real_discount_workbooks_stage_every_row_and_candidate(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    service = ExcelImportService()
    context = _context(
        source_and_provider,
        profile=ParserProfile(discount_scale=10),
    )

    for filename in ("token折扣.xlsx", "token折扣(2).xlsx"):
        summary = service.import_file(session, FIXTURES / filename, context)
        assert summary.total_rows == 26
        assert summary.staged_quote_count == 26
        quotes = session.scalars(
            select(StagedQuote)
            .join(ImportRow)
            .where(ImportRow.import_batch_id == summary.batch_id)
        ).all()
        assert sum(len(item.extracted_quote["candidates"]) for item in quotes) == 52
        assert all(
            candidate["parsed_payload"]["kind"] == "discount"
            for item in quotes
            for candidate in item.extracted_quote["candidates"]
        )

    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_real_quote_reviews_and_full_cell_provenance(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    summary = ExcelImportService().import_file(
        session,
        FIXTURES / "token报价 (1).xlsx",
        _context(source_and_provider),
    )

    assert summary.total_rows == 121
    assert summary.staged_quote_count == 121
    assert summary.review_rows == 121
    assert summary.status == "partial"
    reasons = _reasons(session, summary.batch_id)
    assert reasons["currency_missing"] == 9
    assert reasons["conditional_price_requires_review"] == 1
    assert reasons["empty_header:E"] == 1
    assert reasons["formula_column:E"] == 1

    merged_row = session.scalar(
        select(ImportRow).where(
            ImportRow.import_batch_id == summary.batch_id,
            ImportRow.row_number == 3,
        )
    )
    cells = {cell["coordinate"]: cell for cell in merged_row.raw_data["cells"]}
    assert cells["B3"]["merge_range"] == "B2:B10"
    assert cells["B3"]["source_coordinate"] == "B2"
    assert cells["D3"]["formula_text"]
    assert cells["D3"]["cached_value"] is not None
    assert cells["E3"]["header"]["normalized_value"] == ""
    assert len(merged_row.row_hash) == 64
    artifact = session.get(SourceArtifact, summary.source_artifact_id)
    assert artifact.raw_content == (FIXTURES / "token报价 (1).xlsx").read_bytes()
    assert artifact.uri == f"urn:sha256:{artifact.sha256}"
    assert artifact.storage_uri == (
        f"db://source-artifact/sha256/{artifact.sha256}"
    )
    assert artifact.original_filename == "token报价 (1).xlsx"
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_same_idempotency_key_returns_original_batch_without_new_rows(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    service = ExcelImportService()
    context = _context(
        source_and_provider,
        profile=ParserProfile(discount_scale=10),
    )
    path = FIXTURES / "token折扣(2).xlsx"

    first = service.import_file(session, path, context)
    second = service.import_file(session, path, context)

    assert second.batch_id == first.batch_id
    assert second.source_artifact_id == first.source_artifact_id
    assert second.is_duplicate
    assert session.scalar(select(func.count(ImportBatch.id))) == 1
    assert session.scalar(select(func.count(SourceArtifact.id))) == 1
    assert session.scalar(select(func.count(ImportRow.id))) == 26
    assert session.scalar(select(func.count(StagedQuote.id))) == 26


def test_profile_and_parser_version_are_part_of_idempotency(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    service = ExcelImportService()
    path = FIXTURES / "token折扣(2).xlsx"
    plain = service.import_file(session, path, _context(source_and_provider))
    scaled = service.import_file(
        session,
        path,
        _context(source_and_provider, profile=ParserProfile(discount_scale=10)),
    )
    new_parser = service.import_file(
        session,
        path,
        _context(
            source_and_provider,
            version="parser-v2",
            profile=ParserProfile(discount_scale=10),
        ),
    )

    assert len({plain.batch_id, scaled.batch_id, new_parser.batch_id}) == 3
    assert session.scalar(select(func.count(SourceArtifact.id))) == 1
    assert _reasons(session, plain.batch_id)[
        "bare_number_requires_discount_scale_10"
    ] == 26
    scaled_quotes = session.scalars(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == scaled.batch_id)
    ).all()
    assert all(
        candidate["parsed_payload"]["kind"] == "discount"
        for quote in scaled_quotes
        for candidate in quote.extracted_quote["candidates"]
    )


def test_changed_file_hash_creates_new_artifact_and_batch(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "changing.xlsx"

    def write_price(value: str) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["模型名称", "报价或折扣（最低）"])
        sheet.append(["demo-model", value])
        workbook.save(path)
        workbook.close()

    service = ExcelImportService()
    context = _context(source_and_provider)
    write_price("1元/秒")
    first = service.import_file(session, path, context)
    write_price("2元/秒")
    second = service.import_file(session, path, context)

    assert first.batch_id != second.batch_id
    assert first.source_artifact_id != second.source_artifact_id
    assert session.scalar(select(func.count(SourceArtifact.id))) == 2
    assert session.scalar(select(func.count(ImportBatch.id))) == 2


def test_missing_provider_context_is_rejected_before_artifact_creation(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    with pytest.raises(ValueError, match="provider_id is required"):
        ExcelImportService().import_file(
            session,
            FIXTURES / "token折扣(2).xlsx",
            _context(
                source_and_provider,
                profile=ParserProfile(discount_scale=10),
                provider=False,
            ),
        )
    assert session.scalar(select(func.count(ImportBatch.id))) == 0
    assert session.scalar(select(func.count(SourceArtifact.id))) == 0



def test_unknown_provider_context_is_rejected_before_parsing(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    source_id, _ = source_and_provider
    context = ImportContext(
        data_source_id=source_id,
        provider_id=999_999,
        parser_version="parser-v1",
        profile=ParserProfile(discount_scale=10),
    )
    with pytest.raises(ValueError, match="unknown provider_id"):
        ExcelImportService().import_file(
            session, FIXTURES / "token折扣(2).xlsx", context
        )
    assert session.scalar(select(func.count(ImportBatch.id))) == 0
    assert session.scalar(select(func.count(SourceArtifact.id))) == 0

def test_official_source_is_rejected_for_excel_import(
    session: Session,
) -> None:
    source = DataSource(
        name="Official-only source",
        source_type="official",
        authority_level="primary",
    )
    provider = Provider(
        name="Official provider",
        slug="official-provider",
        provider_type="official",
    )
    session.add_all([source, provider])
    session.flush()
    context = ImportContext(
        data_source_id=source.id,
        provider_id=provider.id,
        parser_version="parser-v1",
        profile=ParserProfile(discount_scale=10),
    )

    with pytest.raises(ValueError, match="OfficialPriceService"):
        ExcelImportService().import_file(
            session, FIXTURES / "token折扣(2).xlsx", context
        )

    assert session.scalar(select(func.count(ImportBatch.id))) == 0
    assert session.scalar(select(func.count(SourceArtifact.id))) == 0


def test_parse_failure_rolls_back_partial_work_and_is_audited(
    session: Session,
    source_and_provider: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parse(*args, **kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr("tokensql.services.import_service.parse_excel", fail_parse)
    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        ExcelImportService().import_file(
            session,
            FIXTURES / "token折扣(2).xlsx",
            _context(
                source_and_provider,
                profile=ParserProfile(discount_scale=10),
            ),
        )

    batch = session.scalar(select(ImportBatch))
    assert batch.status == "failed"
    assert "synthetic parser failure" in batch.error_message
    assert batch.total_rows == 0
    assert session.scalar(select(func.count(ImportRow.id))) == 0
    assert session.scalar(select(func.count(StagedQuote.id))) == 0
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0

def _write_one_quote(path: Path, model_name: str) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["模型名称", "报价或折扣（最低）"])
    sheet.append([model_name, "1元/秒"])
    workbook.save(path)
    workbook.close()


def _duplicate_catalog(session: Session) -> tuple[int, int]:
    catalog = CatalogService(session)
    first_manufacturer = catalog.create_manufacturer("First AI")
    second_manufacturer = catalog.create_manufacturer("Second AI")
    first = catalog.create_model(first_manufacturer.id, "shared-model")
    second = catalog.create_model(second_manufacturer.id, "shared-model")
    return first.id, second.id


class _RecordingSelector:
    def __init__(self, result: QwenResult) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def select_candidate(self, query, candidates):
        self.calls.append((query, tuple(candidates)))
        return self.result


def _qwen_result(
    index: int,
    *,
    confidence: float = 0.95,
    review_reason: ReviewReason | None = None,
) -> QwenResult:
    selection = QwenSelection(
        candidate_index=index,
        confidence=confidence,
        reason="test semantic selection",
        abstain=False,
    )
    if review_reason is not None:
        return QwenResult(
            selection=selection,
            review_required=True,
            review_reason=review_reason,
            reason="test selector requires review",
        )
    return QwenResult(
        selection=selection,
        review_required=False,
        reason=selection.reason,
    )


def test_real_workbook_runs_catalog_exact_matching_without_publication(
    session: Session, source_and_provider: tuple[int, int]
) -> None:
    catalog = CatalogService(session)
    manufacturer = catalog.create_manufacturer("Anthropic")
    model = catalog.create_model(manufacturer.id, "claude-opus-4-8")

    summary = ExcelImportService().import_file(
        session,
        FIXTURES / "token折扣.xlsx",
        _context(
            source_and_provider,
            profile=ParserProfile(discount_scale=10),
        ),
    )

    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(
            ImportRow.import_batch_id == summary.batch_id,
            StagedQuote.extracted_model_name == "claude-opus-4-8",
        )
    )
    assert staged is not None
    assert staged.matched_standard_model_id == model.id
    assert staged.match_method == "exact"
    assert staged.match_confidence == 1
    assert not any(
        reason.startswith("model_match:")
        for reason in staged.validation_errors
    )
    batch = session.get(ImportBatch, summary.batch_id)
    assert batch.import_context["model_matching"] == {
        "sequence": ["exact", "fuzzy", "qwen"],
        "qwen_enabled": False,
        "fuzzy_threshold": 88.0,
        "ambiguity_margin": 5.0,
    }
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_import_uses_fuzzy_match_before_qwen(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    catalog = CatalogService(session)
    manufacturer = catalog.create_manufacturer("Example AI")
    model = catalog.create_model(manufacturer.id, "claude-3.5-sonnet")
    path = tmp_path / "fuzzy.xlsx"
    _write_one_quote(path, "claude-3.5-sonet")

    summary = ExcelImportService(fuzzy_threshold=80).import_file(
        session, path, _context(source_and_provider)
    )
    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == summary.batch_id)
    )

    assert staged.matched_standard_model_id == model.id
    assert staged.match_method == "fuzzy"
    assert not any(
        reason.startswith("model_match:")
        for reason in staged.validation_errors
    )
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_import_qwen_maps_validated_candidate_index_server_side(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    _, second_model_id = _duplicate_catalog(session)
    selector = _RecordingSelector(_qwen_result(1))
    path = tmp_path / "qwen.xlsx"
    _write_one_quote(path, "shared-model")

    summary = ExcelImportService(qwen_client=selector).import_file(
        session, path, _context(source_and_provider)
    )
    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == summary.batch_id)
    )

    assert len(selector.calls) == 1
    query, candidates = selector.calls[0]
    assert query == "shared-model"
    assert len(candidates) == 2
    assert all(not hasattr(candidate, "id") for candidate in candidates)
    assert staged.matched_standard_model_id == second_model_id
    assert staged.match_method == "qwen"
    assert staged.extracted_quote["model_match"]["reason"] == "test semantic selection"
    assert not {
        "database_id", "primary_key", "standard_model_id", "model_version_id"
    }.intersection(staged.extracted_quote["model_match"])
    assert not any(
        reason.startswith("model_match:")
        for reason in staged.validation_errors
    )
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_import_qwen_low_confidence_creates_review_without_guessing(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    _duplicate_catalog(session)
    selector = _RecordingSelector(
        _qwen_result(
            0,
            confidence=0.4,
            review_reason=ReviewReason.LOW_CONFIDENCE,
        )
    )
    path = tmp_path / "low-confidence.xlsx"
    _write_one_quote(path, "shared-model")

    summary = ExcelImportService(qwen_client=selector).import_file(
        session, path, _context(source_and_provider)
    )
    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == summary.batch_id)
    )
    review = session.scalar(
        select(ReviewItem).where(
            ReviewItem.staged_quote_id == staged.id,
            ReviewItem.review_type == "model_match",
        )
    )

    assert staged.matched_standard_model_id is None
    assert staged.matched_model_version_id is None
    assert staged.match_method is None
    assert staged.review_status == "needs_review"
    assert "model_match:low_confidence" in staged.validation_errors
    assert review.reason == "model_match:low_confidence"
    assert review.proposed_resolution["query"] == "shared-model"
    assert review.proposed_resolution["review_reason"] == "low_confidence"
    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_normalize_prompt_canonicalizes_whitespace_and_unicode() -> None:
    assert normalize_prompt("  报价　供应商 \tX\n\n  币种 CNY  \n") == (
        "报价 供应商 X\n币种 CNY"
    )
    assert normalize_prompt("") == ""


def test_import_context_requires_mapping_context_defaults() -> None:
    with pytest.raises(ValueError, match="context_defaults"):
        ImportContext(
            data_source_id=1,
            parser_version="parser-v1",
            context_defaults=["not-a-mapping"],
        )


def test_prompt_text_participates_in_idempotency_and_is_persisted(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    service = ExcelImportService()
    path = tmp_path / "prompt.xlsx"
    _write_one_quote(path, "demo-model")
    source_id, provider_id = source_and_provider

    def context(prompt: str) -> ImportContext:
        return ImportContext(
            data_source_id=source_id,
            provider_id=provider_id,
            parser_version="parser-v1",
            profile=ParserProfile(),
            prompt_text=prompt,
            context_defaults={"currency": "CNY", "unit_basis": "1M tokens"},
        )

    first = service.import_file(session, path, context("厂商为 Example AI"))
    same = service.import_file(session, path, context("厂商为 Example AI"))
    assert same.batch_id == first.batch_id
    assert same.is_duplicate

    other = service.import_file(session, path, context("厂商为 Other AI"))
    assert other.batch_id != first.batch_id
    assert session.scalar(select(func.count(ImportBatch.id))) == 2

    batch = session.get(ImportBatch, first.batch_id)
    assert batch.import_context["prompt_text"] == "厂商为 Example AI"
    assert batch.import_context["prompt_sha256"] == hashlib.sha256(
        normalize_prompt("厂商为 Example AI").encode("utf-8")
    ).hexdigest()
    assert batch.import_context["context_defaults"] == {
        "currency": "CNY",
        "unit_basis": "1M tokens",
    }
    assert batch.import_context["interpretation_version"] == "deterministic-v1"
    assert batch.import_context["context_scope"] == "batch"
    assert batch.import_context["context_version"] == 1


def test_whitespace_equivalent_prompts_share_prompt_sha_but_not_batch(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    service = ExcelImportService()
    path = tmp_path / "prompt-ws.xlsx"
    _write_one_quote(path, "demo-model")
    source_id, provider_id = source_and_provider

    def context(prompt: str) -> ImportContext:
        return ImportContext(
            data_source_id=source_id,
            provider_id=provider_id,
            parser_version="parser-v1",
            profile=ParserProfile(),
            prompt_text=prompt,
        )

    first = service.import_file(session, path, context("厂商  X\n\n币种\tCNY "))
    second = service.import_file(session, path, context(" 厂商 X\n 币种 CNY"))
    assert second.batch_id != first.batch_id
    first_context = session.get(ImportBatch, first.batch_id).import_context
    second_context = session.get(ImportBatch, second.batch_id).import_context
    assert first_context["prompt_sha256"] == second_context["prompt_sha256"]
    assert first_context["prompt_text"] != second_context["prompt_text"]


def test_prompt_allows_missing_provider_id_in_deterministic_path(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
) -> None:
    path = tmp_path / "no-provider.xlsx"
    _write_one_quote(path, "demo-model")
    source_id, _ = source_and_provider
    context = ImportContext(
        data_source_id=source_id,
        provider_id=None,
        parser_version="parser-v1",
        profile=ParserProfile(),
        prompt_text="报价供应商为 Fixture provider；缺省币种 CNY。",
    )

    summary = ExcelImportService().import_file(session, path, context)

    assert summary.total_rows == 1
    assert summary.staged_quote_count == 1
    batch = session.get(ImportBatch, summary.batch_id)
    assert batch.provider_id is None
    assert batch.status == "partial"
    assert batch.import_context["prompt_text"] == (
        "报价供应商为 Fixture provider；缺省币种 CNY。"
    )
    assert _reasons(session, summary.batch_id)["provider_missing"] == 1


def test_disabled_qwen_never_constructs_client_and_ambiguous_name_is_reviewed(
    session: Session,
    source_and_provider: tuple[int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _duplicate_catalog(session)
    path = tmp_path / "disabled.xlsx"
    _write_one_quote(path, "shared-model")

    class DisabledSettings:
        qwen_enabled = False

    def unexpected_client(*args, **kwargs):
        raise AssertionError("disabled Qwen must not construct an HTTP client")

    monkeypatch.setattr(
        "tokensql.services.import_service.QwenClient.from_settings",
        unexpected_client,
    )
    summary = ExcelImportService(settings=DisabledSettings()).import_file(
        session, path, _context(source_and_provider)
    )
    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == summary.batch_id)
    )

    assert staged.matched_standard_model_id is None
    assert staged.match_method is None
    assert "model_match:ambiguous" in staged.validation_errors
    assert staged.review_status == "needs_review"
    assert summary.review_rows == 1
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0

