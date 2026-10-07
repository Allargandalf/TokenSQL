"""Transactional Excel-to-staging ingestion.

Parsing is deterministic. Model matching is exact then fuzzy, with an optional
strictly validated LLM candidate selector for unresolved names. The service never
publishes a :class:`PriceSnapshot`; the LLM sees no keys and cannot execute SQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import utc_now
from tokensql.db.models import (
    DataSource,
    ImportBatch,
    ImportRow,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.importers import ExcelParseResult, ParserProfile, parse_excel
from tokensql.importers.types import CellRecord, ColumnHeader, RowRecord, StagedQuoteRecord
from tokensql.llm import QwenClient
from tokensql.matching import CandidateSelector

from .catalog_service import CatalogService


@dataclass(frozen=True)
class ImportContext:
    """All caller-controlled choices that affect an import.

    ``sheet_names=None`` means all workbook sheets.  ``vendor_context`` is
    audit-only context (for example a supplier account or quotation date); it is
    included in the idempotency key because it can change interpretation.
    """

    data_source_id: int
    parser_version: str
    profile: ParserProfile = field(default_factory=ParserProfile)
    sheet_names: tuple[str, ...] | None = None
    provider_id: int | None = None
    vendor_context: Mapping[str, Any] = field(default_factory=dict)
    initiated_by: str | None = None

    def __post_init__(self) -> None:
        if self.data_source_id <= 0:
            raise ValueError("data_source_id must be positive")
        if not self.parser_version.strip():
            raise ValueError("parser_version must not be blank")
        object.__setattr__(self, "parser_version", self.parser_version.strip())
        if self.provider_id is not None and self.provider_id <= 0:
            raise ValueError("provider_id must be positive when supplied")
        if self.sheet_names is not None:
            names = tuple(str(name).strip() for name in self.sheet_names)
            if any(not name for name in names):
                raise ValueError("sheet_names must not contain blank names")
            if len(set(names)) != len(names):
                raise ValueError("sheet_names must not contain duplicates")
            object.__setattr__(self, "sheet_names", names)


@dataclass(frozen=True)
class ImportSummary:
    batch_id: int
    source_artifact_id: int
    idempotency_key: str
    status: str
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    review_rows: int
    staged_quote_count: int
    review_item_count: int
    reused_existing: bool = False

    @property
    def is_duplicate(self) -> bool:
        return self.reused_existing

    @property
    def deduplicated(self) -> bool:
        return self.reused_existing


@contextmanager
def _transaction(session: Session) -> Iterator[None]:
    """Own a transaction unless the caller already owns one.

    A savepoint avoids committing unrelated caller work while still ensuring
    that a failed import cannot leave a partial batch behind.
    """

    manager = session.begin_nested() if session.in_transaction() else session.begin()
    with manager:
        yield


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile_payload(profile: ParserProfile) -> dict[str, Any]:
    return {
        "header_row": profile.header_row,
        "discount_scale": profile.discount_scale,
        "price_headers": profile.price_headers,
    }


def _context_payload(context: ImportContext) -> dict[str, Any]:
    return {
        "data_source_id": context.data_source_id,
        "parser_version": context.parser_version,
        "profile": _profile_payload(context.profile),
        "sheet_names": sorted(context.sheet_names) if context.sheet_names is not None else None,
        "provider_id": context.provider_id,
        "vendor_context": context.vendor_context,
        "initiated_by": context.initiated_by,
    }


def _idempotency_key(artifact_sha256: str, context: ImportContext) -> str:
    interpretation = {
        "artifact_sha256": artifact_sha256,
        "data_source_id": context.data_source_id,
        "parser_version": context.parser_version,
        "profile": _profile_payload(context.profile),
        "sheet_names": sorted(context.sheet_names) if context.sheet_names is not None else None,
        "provider_id": context.provider_id,
        "vendor_context": context.vendor_context,
    }
    digest = hashlib.sha256(_canonical_json(interpretation).encode("utf-8")).hexdigest()
    return "excel:" + digest


def _header_payload(header: ColumnHeader) -> dict[str, Any]:
    return _json_safe(
        {
            "column_index": header.column_index,
            "column_letter": header.column_letter,
            "coordinate": header.coordinate,
            "raw_value": header.raw_value,
            "normalized_value": header.normalized_value,
            "needs_review": header.needs_review,
            "review_reasons": header.review_reasons,
        }
    )


def _cell_payload(cell: CellRecord, header: ColumnHeader) -> dict[str, Any]:
    return _json_safe(
        {
            "coordinate": cell.coordinate,
            "column_index": cell.column_index,
            "row_index": cell.row_index,
            "header": _header_payload(header),
            "raw_value": cell.raw_value,
            "cached_value": cell.cached_value,
            "formula_text": cell.formula_text,
            "formula_kind": cell.formula_kind,
            "formula_range": cell.formula_range,
            "shared_index": cell.shared_index,
            "merge_range": cell.merge_range,
            "source_coordinate": cell.source_coordinate,
            "needs_review": cell.needs_review,
            "review_reasons": cell.review_reasons,
        }
    )


def _row_payload(row: RowRecord, headers: Sequence[ColumnHeader]) -> dict[str, Any]:
    header_by_column = {header.column_index: header for header in headers}
    return {
        "row_number": row.row_index,
        "needs_review": row.needs_review,
        "review_reasons": list(row.review_reasons),
        "cells": [
            _cell_payload(cell, header_by_column[cell.column_index]) for cell in row.cells
        ],
    }


def _candidate_payload(candidate: StagedQuoteRecord) -> dict[str, Any]:
    parsed = candidate.parsed_price
    return _json_safe(
        {
            "source_coordinate": candidate.source_coordinate,
            "header": _header_payload(candidate.header),
            "parsed_payload": {
                "raw_value": parsed.raw_value,
                "raw_text": parsed.raw_text,
                "kind": parsed.kind,
                "amount": parsed.amount,
                "currency": parsed.currency,
                "unit": parsed.unit,
                "discount_multiplier": parsed.discount_multiplier,
                "condition_text": parsed.condition_text,
            },
            "confidence": "1.0000" if not candidate.needs_review else None,
            "status": "needs_review" if candidate.needs_review else "parsed",
            "review_reasons": candidate.review_reasons,
            "cell_provenance": _cell_payload(candidate.cell, candidate.header),
        }
    )


def _nonempty_data_row(row: RowRecord, header_row: int) -> bool:
    if row.row_index <= header_row:
        return False
    return any(
        cell.raw_value is not None
        or cell.cached_value is not None
        or cell.formula_text is not None
        for cell in row.cells
    )


def _cell_effective_value(cell: CellRecord) -> Any:
    if cell.formula_text is not None and cell.cached_value is not None:
        return cell.cached_value
    return cell.raw_value


def _model_name(row: RowRecord, headers: Sequence[ColumnHeader]) -> str | None:
    accepted = {"模型名称", "模型", "model", "model name", "model_name"}
    for header in headers:
        if header.normalized_value in accepted:
            value = _cell_effective_value(row.cell_at_column(header.column_index))
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _review_type(reason: str) -> str:
    if "currency" in reason:
        return "currency"
    if "unit" in reason:
        return "unit"
    if "model" in reason:
        return "model_match"
    if any(token in reason for token in ("price", "discount", "formula")):
        return "price"
    return "other"


def _summary(session: Session, batch: ImportBatch, *, reused: bool) -> ImportSummary:
    staged_count = session.scalar(
        select(func.count(StagedQuote.id))
        .join(ImportRow, StagedQuote.import_row_id == ImportRow.id)
        .where(ImportRow.import_batch_id == batch.id)
    ) or 0
    review_count = session.scalar(
        select(func.count(ReviewItem.id))
        .join(StagedQuote, ReviewItem.staged_quote_id == StagedQuote.id)
        .join(ImportRow, StagedQuote.import_row_id == ImportRow.id)
        .where(ImportRow.import_batch_id == batch.id)
    ) or 0
    return ImportSummary(
        batch_id=batch.id,
        source_artifact_id=batch.source_artifact_id,
        idempotency_key=batch.idempotency_key,
        status=batch.status,
        total_rows=batch.total_rows,
        accepted_rows=batch.accepted_rows,
        rejected_rows=batch.rejected_rows,
        review_rows=batch.review_rows,
        staged_quote_count=int(staged_count),
        review_item_count=int(review_count),
        reused_existing=reused,
    )


class ExcelImportService:
    """Persist parsed rows and resolve model presentations into staging.

    Matching is deterministic-first. An injected selector is useful for tests
    and alternate local runtimes; otherwise a Qwen client is created lazily only
    when ``TOKENSQL_QWEN_ENABLED`` is true. The selector sees presentation-only
    candidates and can never write SQL or choose a database identifier.
    """

    def __init__(
        self,
        *,
        qwen_client: CandidateSelector | None = None,
        settings: Any | None = None,
        fuzzy_threshold: float = 88.0,
        ambiguity_margin: float = 5.0,
    ) -> None:
        if not 0.0 <= fuzzy_threshold <= 100.0:
            raise ValueError("fuzzy_threshold must be between 0 and 100")
        if ambiguity_margin < 0.0:
            raise ValueError("ambiguity_margin cannot be negative")
        self._qwen_client = qwen_client
        self._settings = settings
        self._fuzzy_threshold = float(fuzzy_threshold)
        self._ambiguity_margin = float(ambiguity_margin)

    @contextmanager
    def _candidate_selector(self) -> Iterator[CandidateSelector | None]:
        if self._qwen_client is not None:
            yield self._qwen_client
            return

        settings = self._settings
        if settings is None:
            from tokensql.config import get_settings

            settings = get_settings()
        if not settings.qwen_enabled:
            yield None
            return

        with QwenClient.from_settings(settings) as client:
            yield client

    def import_file(
        self, session: Session, source_path: str | Path, context: ImportContext
    ) -> ImportSummary:
        path = Path(source_path).expanduser().resolve(strict=True)
        artifact_sha256 = _sha256_file(path)
        key = _idempotency_key(artifact_sha256, context)

        try:
            with _transaction(session):
                self._validate_context_entities(session, context)
                existing = session.scalar(
                    select(ImportBatch).where(ImportBatch.idempotency_key == key)
                )
                if existing is not None:
                    return _summary(session, existing, reused=True)

            parsed = parse_excel(path, profile=context.profile)
            selected = self._selected_sheets(parsed, context.sheet_names)

            with _transaction(session):
                self._validate_context_entities(session, context)
                existing = session.scalar(
                    select(ImportBatch).where(ImportBatch.idempotency_key == key)
                )
                if existing is not None:
                    return _summary(session, existing, reused=True)

                artifact = self._get_or_create_artifact(
                    session, path, artifact_sha256, parsed, context
                )
                batch = ImportBatch(
                    source_artifact=artifact,
                    provider_id=context.provider_id,
                    idempotency_key=key,
                    parser_version=context.parser_version,
                    status="processing",
                    initiated_by=context.initiated_by,
                    import_context=_json_safe(
                        {
                            **_context_payload(context),
                            "effective_sheet_names": [sheet.title for sheet in selected],
                            "workbook_review_reasons": parsed.workbook.review_reasons,
                        }
                    ),
                )
                session.add(batch)
                session.flush()
                with self._candidate_selector() as qwen_client:
                    batch.import_context = {
                        **batch.import_context,
                        "model_matching": {
                            "sequence": ["exact", "fuzzy", "qwen"],
                            "qwen_enabled": qwen_client is not None,
                            "fuzzy_threshold": self._fuzzy_threshold,
                            "ambiguity_margin": self._ambiguity_margin,
                        },
                    }
                    self._persist_rows(
                        session,
                        batch,
                        parsed,
                        selected,
                        context,
                        qwen_client=qwen_client,
                    )
                batch.completed_at = utc_now()
                batch.status = (
                    "partial"
                    if batch.rejected_rows or batch.review_rows
                    else "completed"
                )
                session.flush()
                return _summary(session, batch, reused=False)
        except Exception as error:
            self._record_failure(
                session,
                path=path,
                artifact_sha256=artifact_sha256,
                idempotency_key=key,
                context=context,
                error=error,
            )
            raise

    @staticmethod
    def _validate_context_entities(session: Session, context: ImportContext) -> None:
        source = session.get(DataSource, context.data_source_id)
        if source is None:
            raise ValueError(f"unknown data_source_id: {context.data_source_id}")
        if source.source_type == "official":
            raise ValueError(
                "official DataSource cannot be used for Excel import; "
                "use OfficialPriceService"
            )
        if context.provider_id is None:
            raise ValueError("provider_id is required for Excel import")
        if session.get(Provider, context.provider_id) is None:
            raise ValueError(f"unknown provider_id: {context.provider_id}")

    @staticmethod
    def _selected_sheets(
        parsed: ExcelParseResult, sheet_names: tuple[str, ...] | None
    ) -> tuple[Any, ...]:
        if sheet_names is None:
            return parsed.workbook.sheets
        available = {sheet.title for sheet in parsed.workbook.sheets}
        missing = sorted(set(sheet_names) - available)
        if missing:
            raise ValueError("unknown sheet selection: " + ", ".join(missing))
        requested = set(sheet_names)
        return tuple(sheet for sheet in parsed.workbook.sheets if sheet.title in requested)

    @staticmethod
    def _get_or_create_artifact(
        session: Session,
        path: Path,
        artifact_sha256: str,
        parsed: ExcelParseResult | None,
        context: ImportContext,
    ) -> SourceArtifact:
        artifact = session.scalar(
            select(SourceArtifact).where(
                SourceArtifact.data_source_id == context.data_source_id,
                SourceArtifact.sha256 == artifact_sha256,
            )
        )
        if artifact is not None:
            return artifact
        raw_content = path.read_bytes()
        if hashlib.sha256(raw_content).hexdigest() != artifact_sha256:
            raise ValueError("source workbook changed during import")
        supplied_name = context.vendor_context.get("original_filename")
        original_filename = (
            supplied_name.strip()
            if isinstance(supplied_name, str) and supplied_name.strip()
            else path.name
        )
        artifact = SourceArtifact(
            data_source_id=context.data_source_id,
            uri=f"urn:sha256:{artifact_sha256}",
            storage_uri=f"db://source-artifact/sha256/{artifact_sha256}",
            raw_content=raw_content,
            original_filename=original_filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            sha256=artifact_sha256,
            fetched_at=utc_now(),
            raw_metadata={
                "byte_size": path.stat().st_size,
                "sheet_names": (
                    [sheet.title for sheet in parsed.workbook.sheets] if parsed else []
                ),
            },
        )
        session.add(artifact)
        session.flush()
        return artifact

    def _persist_rows(
        self,
        session: Session,
        batch: ImportBatch,
        parsed: ExcelParseResult,
        selected: Sequence[Any],
        context: ImportContext,
        *,
        qwen_client: CandidateSelector | None,
    ) -> None:
        selected_names = {sheet.title for sheet in selected}
        candidates_by_row: dict[tuple[str, int], list[StagedQuoteRecord]] = {}
        for candidate in parsed.staged_quotes:
            if candidate.sheet_name in selected_names:
                candidates_by_row.setdefault(
                    (candidate.sheet_name, candidate.source_row), []
                ).append(candidate)

        catalog = CatalogService(session)
        total = accepted = rejected = review = 0
        for sheet in selected:
            data_rows = [
                row
                for row in sheet.rows
                if _nonempty_data_row(row, sheet.header_row)
            ]
            for position, row in enumerate(data_rows):
                total += 1
                raw_data = _row_payload(row, sheet.headers)
                row_hash = hashlib.sha256(
                    _canonical_json(raw_data).encode("utf-8")
                ).hexdigest()
                candidates = candidates_by_row.get((sheet.title, row.row_index), [])
                import_row = ImportRow(
                    import_batch=batch,
                    sheet_name=sheet.title,
                    row_number=row.row_index,
                    row_hash=row_hash,
                    raw_data=raw_data,
                    status="staged" if candidates else "rejected",
                    error_code=None if candidates else "no_quote_candidate",
                    error_message=None if candidates else "No configured price column produced a candidate",
                )
                session.add(import_row)

                if not candidates:
                    rejected += 1
                    continue

                reasons = list(
                    dict.fromkeys(
                        reason
                        for candidate in candidates
                        for reason in candidate.review_reasons
                    )
                )
                if context.provider_id is None:
                    reasons.append("provider_missing")
                if position == 0:
                    reasons.extend(sheet.review_reasons)
                    for header in sheet.headers:
                        if header.needs_review:
                            reasons.append("empty_header:" + header.column_letter)
                        if any(
                            cell.formula_text is not None
                            for data_row in data_rows
                            for cell in (data_row.cell_at_column(header.column_index),)
                        ):
                            reasons.append("formula_column:" + header.column_letter)
                reasons = list(dict.fromkeys(reasons))

                extracted_model_name = _model_name(row, sheet.headers)
                matched_model_id = None
                matched_version_id = None
                match_method = None
                match_confidence = None
                model_review_reason = None
                model_review_payload = None
                model_match_audit: dict[str, Any]
                if extracted_model_name is None:
                    model_review_reason = "model_missing"
                    model_review_payload = {
                        "query": None,
                        "review_reason": "unknown",
                        "detail": "Excel row does not contain a model name",
                    }
                    model_match_audit = {
                        **model_review_payload,
                        "stage": "review",
                        "confidence": "0.0",
                        "review_required": True,
                    }
                else:
                    match = catalog.match_model(
                        extracted_model_name,
                        provider_id=context.provider_id,
                        qwen_client=qwen_client,
                        fuzzy_threshold=self._fuzzy_threshold,
                        ambiguity_margin=self._ambiguity_margin,
                    )
                    match_confidence = Decimal(str(match.result.confidence))
                    model_match_audit = {
                        "query": extracted_model_name,
                        "stage": match.result.stage.value,
                        "confidence": str(match.result.confidence),
                        "reason": match.result.reason,
                        "review_required": match.result.review_required,
                        "review_reason": (
                            match.result.review_reason.value
                            if match.result.review_reason is not None
                            else None
                        ),
                    }
                    if match.result.review_required:
                        review_reason = match.result.review_reason
                        reason_code = (
                            review_reason.value
                            if review_reason is not None
                            else "invalid_response"
                        )
                        model_review_reason = f"model_match:{reason_code}"
                        model_review_payload = {
                            "query": extracted_model_name,
                            "review_reason": reason_code,
                            "detail": match.result.reason,
                            "confidence": str(match.result.confidence),
                        }
                    else:
                        matched_model_id = match.standard_model_id
                        matched_version_id = match.model_version_id
                        match_method = match.result.stage.value

                if model_review_reason is not None:
                    reasons.append(model_review_reason)
                    reasons = list(dict.fromkeys(reasons))

                staged = StagedQuote(
                    import_row=import_row,
                    provider_id=context.provider_id,
                    matched_standard_model_id=matched_model_id,
                    matched_model_version_id=matched_version_id,
                    extracted_model_name=extracted_model_name,
                    extracted_plan_name=None,
                    match_method=match_method,
                    match_confidence=match_confidence,
                    review_status="needs_review" if reasons else "pending",
                    extracted_quote={
                        "schema_version": 1,
                        "model_match": model_match_audit,
                        "candidates": [_candidate_payload(item) for item in candidates],
                    },
                    validation_errors=reasons,
                )
                session.add(staged)
                for reason in reasons:
                    staged.review_items.append(
                        ReviewItem(
                            review_type=_review_type(reason),
                            status="open",
                            severity="warning",
                            reason=reason,
                            proposed_resolution=(
                                model_review_payload
                                if reason == model_review_reason
                                else None
                            ),
                        )
                    )
                if reasons:
                    review += 1
                else:
                    accepted += 1

        batch.total_rows = total
        batch.accepted_rows = accepted
        batch.rejected_rows = rejected
        batch.review_rows = review

    def _record_failure(
        self,
        session: Session,
        *,
        path: Path,
        artifact_sha256: str,
        idempotency_key: str,
        context: ImportContext,
        error: Exception,
    ) -> None:
        """Best-effort audit after the atomic import transaction rolls back."""

        try:
            with _transaction(session):
                try:
                    self._validate_context_entities(session, context)
                except ValueError:
                    return
                existing = session.scalar(
                    select(ImportBatch).where(
                        ImportBatch.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return
                artifact = self._get_or_create_artifact(
                    session, path, artifact_sha256, None, context
                )
                valid_provider_id = context.provider_id
                failed_at = utc_now()
                failed = ImportBatch(
                    source_artifact=artifact,
                    provider_id=valid_provider_id,
                    idempotency_key=idempotency_key,
                    parser_version=context.parser_version,
                    status="failed",
                    started_at=failed_at,
                    completed_at=failed_at,
                    initiated_by=context.initiated_by,
                    error_message=f"{error.__class__.__name__}: {error}",
                    import_context=_json_safe(
                        {
                            **_context_payload(context),
                            "failure_phase": "excel_staging",
                        }
                    ),
                )
                session.add(failed)
                session.flush()
        except Exception:
            # Never replace the import exception with a secondary audit failure.
            return
