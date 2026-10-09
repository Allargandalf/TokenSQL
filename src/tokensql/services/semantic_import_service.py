"""Qwen-driven semantic Excel ingestion with rule-checked auto-publication.

Pipeline: deterministic structure parse -> Qwen whole-table understanding ->
batched per-row semantic extraction -> strict rule validation and
standardisation -> high-confidence, rule-passing rows are published in one
transaction; only low-confidence or rule-failing rows enter review.

The model never sees database identifiers and never writes SQL. Catalog
creation and price publication stay inside validated service-layer
transactions with explicit audit provenance.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tokensql.db.base import utc_now
from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    FXRate,
    ImportBatch,
    ImportRow,
    ReviewItem,
    StagedQuote,
)
from tokensql.importers import parse_excel
from tokensql.importers.types import (
    ExcelParseResult,
    RowRecord,
    SheetProfile,
)
from tokensql.llm.extraction_schemas import (
    ComponentExtractionModel,
    RowExtractionModel,
    TableUnderstandingModel,
)
from tokensql.llm.semantic import (
    ROW_PROMPT_VERSION,
    TABLE_PROMPT_VERSION,
    ChatResult,
    SemanticQwenClient,
    SemanticQwenError,
)
from tokensql.matching import normalize_model_name

from .catalog_service import CatalogConflictError, CatalogService
from .import_service import (
    ExcelImportService,
    ImportContext,
    ImportSummary,
    _candidate_payload,
    _context_payload,
    _idempotency_key,
    _json_safe,
    _model_name,
    _nonempty_data_row,
    _revive_failed_batch,
    _sha256_file,
    _summary,
    _transaction,
)
from .pricing_service import (
    KNOWN_CONDITION_KEYS,
    KNOWN_CURRENCIES,
    PricingService,
    PricingValidationError,
)

AUTO_ACTOR_PREFIX = "qwen-auto"
MAX_SUMMARY_CHARS = 14_000
DEFAULT_ROW_BATCH_SIZE = 10

_UNIT_ALIASES: dict[str, str] = {
    "token": "token", "tokens": "token", "tok": "token",
    "百万token": "token", "百万tokens": "token", "million tokens": "token",
    "million_tokens": "token", "per million tokens": "token",
    "千token": "token", "thousand tokens": "token",
    "秒": "second", "seconds": "second", "second": "second", "sec": "second",
    "s": "second",
    "次": "request", "请求": "request", "request": "request",
    "requests": "request", "call": "request",
    "张": "image", "图片": "image", "图": "image", "image": "image",
    "images": "image", "picture": "image",
    "字符": "character", "character": "character", "characters": "character",
    "char": "character",
    "音色": "voice", "voice": "voice", "voices": "voice",
}

_MILLION_TOKEN_MARKERS = ("百万", "million", "1e6", "1_000_000")
_THOUSAND_TOKEN_MARKERS = ("千", "thousand tokens", "thousand_tokens")


class SemanticImportError(RuntimeError):
    """A semantic import could not proceed (model unavailable/failed)."""


@dataclass
class _RowOutcome:
    status: str  # accepted | review | rejected
    reasons: list[str] = field(default_factory=list)
    staged: StagedQuote | None = None


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{label} must not be a float")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                result = datetime.combine(
                    date.fromisoformat(text[:10]), datetime.min.time()
                )
            except ValueError:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _cell_ref(coordinate: str) -> str:
    text = str(coordinate).strip()
    if "!" in text:
        text = text.rsplit("!", 1)[-1]
    return text.upper()


def _normalize_unit(unit_name: str | None) -> tuple[str | None, str]:
    """Return (canonical unit, scale marker); marker in {"", "million", "thousand"}."""

    raw = str(unit_name or "").strip().casefold()
    if not raw:
        return None, ""
    if any(marker in raw for marker in _MILLION_TOKEN_MARKERS):
        scale = "million"
    elif any(marker in raw for marker in _THOUSAND_TOKEN_MARKERS):
        scale = "thousand"
    else:
        scale = ""
    canonical = _UNIT_ALIASES.get(raw)
    return canonical, scale


class SemanticExcelImportService(ExcelImportService):
    """Excel + user Prompt/Context -> Qwen extraction -> validated auto-publish."""

    def __init__(
        self,
        *,
        semantic_client: SemanticQwenClient | None = None,
        settings: Any | None = None,
        row_batch_size: int = DEFAULT_ROW_BATCH_SIZE,
        min_confidence: float | None = None,
    ) -> None:
        super().__init__(settings=settings)
        if row_batch_size < 1 or row_batch_size > 50:
            raise ValueError("row_batch_size must be between 1 and 50")
        self._semantic_client = semantic_client
        if min_confidence is None:
            from tokensql.config import get_settings

            min_confidence = (settings or get_settings()).qwen_auto_publish_min_confidence
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self._min_confidence = float(min_confidence)
        self._row_batch_size = row_batch_size

    @contextmanager
    def _client(self) -> Iterator[SemanticQwenClient]:
        if self._semantic_client is not None:
            yield self._semantic_client
            return
        client = SemanticQwenClient(settings=self._settings)
        try:
            yield client
        finally:
            client.close()

    # ------------------------------------------------------------------ #
    # workbook summary for whole-table understanding
    # ------------------------------------------------------------------ #

    @staticmethod
    def _effective_value(cell: Any) -> Any:
        if cell.formula_text is not None and cell.cached_value is not None:
            return cell.cached_value
        return cell.raw_value

    def _row_payload(
        self, sheet: SheetProfile, row: RowRecord
    ) -> dict[str, Any]:
        cells = []
        for cell in row.cells:
            value = self._effective_value(cell)
            if value is None and cell.formula_text is None:
                continue
            header = sheet.header_at_column(cell.column_index)
            entry: dict[str, Any] = {
                "coordinate": f"{sheet.title}!{cell.coordinate}",
                "header": header.normalized_value or header.raw_value,
                "value": str(value) if value is not None else None,
            }
            if cell.formula_text is not None:
                entry["formula"] = cell.formula_text
            if cell.merge_range:
                entry["merge_range"] = cell.merge_range
            cells.append(entry)
        return {"row_number": row.row_index, "cells": cells}

    def _workbook_summary(
        self, parsed: ExcelParseResult, selected: Sequence[SheetProfile]
    ) -> dict[str, Any]:
        candidate_rows: dict[str, set[int]] = {}
        for candidate in parsed.staged_quotes:
            candidate_rows.setdefault(candidate.sheet_name, set()).add(
                candidate.source_row
            )
        sheets = []
        for sheet in selected:
            data_rows = [
                row for row in sheet.rows if _nonempty_data_row(row, sheet.header_row)
            ]
            picked: dict[int, RowRecord] = {}
            for row in data_rows[:3]:
                picked[row.row_index] = row
            for row_number in sorted(candidate_rows.get(sheet.title, set()))[:8]:
                for row in data_rows:
                    if row.row_index == row_number:
                        picked[row_number] = row
                        break
            if data_rows:
                picked[data_rows[-1].row_index] = data_rows[-1]
            sheets.append(
                {
                    "sheet": sheet.title,
                    "header_row": sheet.header_row,
                    "headers": [
                        {
                            "column_letter": header.column_letter,
                            "coordinate": f"{sheet.title}!{header.coordinate}",
                            "raw_value": header.raw_value,
                            "normalized_value": header.normalized_value,
                        }
                        for header in sheet.headers
                    ],
                    "merge_ranges": list(sheet.merge_ranges)[:40],
                    "data_row_count": len(data_rows),
                    "review_reasons": list(sheet.review_reasons),
                    "representative_rows": [
                        self._row_payload(sheet, picked[key])
                        for key in sorted(picked)
                    ],
                }
            )
        summary = {
            "workbook": parsed.workbook.source_name,
            "sheets": sheets,
            "workbook_review_reasons": list(parsed.workbook.review_reasons),
        }
        if len(json.dumps(summary, ensure_ascii=False)) > MAX_SUMMARY_CHARS:
            for sheet in sheets:
                sheet["representative_rows"] = sheet["representative_rows"][:2]
                sheet["truncated"] = True
        return summary

    @staticmethod
    def _user_context(context: ImportContext) -> dict[str, Any]:
        return {
            "prompt_text": context.prompt_text,
            "context_defaults": dict(context.context_defaults),
            "original_filename": context.vendor_context.get("original_filename"),
        }

    # ------------------------------------------------------------------ #
    # per-row validation and catalog resolution
    # ------------------------------------------------------------------ #

    def _sheet_understanding(
        self, understanding: dict[str, Any], sheet_title: str
    ) -> dict[str, Any]:
        for sheet in understanding.get("sheets", []):
            if sheet.get("sheet") == sheet_title:
                return sheet
        return {}

    def _resolve_metric(
        self, session: Session, component: ComponentExtractionModel
    ) -> BillingMetric | None:
        if component.metric_code:
            metric = session.scalar(
                select(BillingMetric).where(
                    BillingMetric.code == component.metric_code.strip()
                )
            )
            if metric is not None:
                return metric
        if component.dimension is None:
            return None
        statement = select(BillingMetric).where(
            BillingMetric.dimension == component.dimension
        )
        if component.direction in ("input", "output", "cached_input"):
            statement = statement.where(BillingMetric.direction == component.direction)
        metrics = list(session.scalars(statement))
        if len(metrics) == 1:
            return metrics[0]
        return None

    def _validate_component(
        self,
        session: Session,
        component: ComponentExtractionModel,
        *,
        sheet_title: str,
        row_cell_refs: set[str],
        context: ImportContext,
        understanding_sheet: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        reasons: list[str] = []
        for evidence in component.evidence:
            text = evidence.strip()
            if text.lower() == "user_context":
                continue
            if "!" in text and text.rsplit("!", 1)[0].strip() != sheet_title:
                reasons.append(f"evidence_invalid:{evidence}")
                continue
            if _cell_ref(text) not in row_cell_refs:
                reasons.append(f"evidence_invalid:{evidence}")
        unknown_conditions = sorted(set(component.conditions) - KNOWN_CONDITION_KEYS)
        if unknown_conditions:
            reasons.append("condition_unknown:" + ",".join(unknown_conditions))

        if component.component_type == "discount":
            try:
                rate = _decimal(component.discount_rate, "discount_rate")
            except ValueError as exc:
                return None, [f"discount_invalid:{exc}"]
            if not 0 <= rate <= 1:
                return None, ["discount_invalid:rate_out_of_range"]
            metric = self._resolve_metric(session, component)
            if metric is None:
                reasons.append("metric_unresolved")
            payload = {
                "component_type": "discount",
                "discount_rate": str(rate),
                "conditions": dict(component.conditions),
                "condition_key": "discount",
                "tier_start": str(_decimal(component.tier_start or 0, "tier_start")),
                "tier_end": (
                    None
                    if component.tier_end is None
                    else str(_decimal(component.tier_end, "tier_end"))
                ),
            }
            if metric is not None:
                payload["billing_metric_id"] = metric.id
            return payload, reasons

        try:
            amount = _decimal(component.raw_amount, "raw_amount")
            quantity = _decimal(component.unit_quantity, "unit_quantity")
        except ValueError as exc:
            return None, [f"amount_invalid:{exc}"]
        if amount < 0 or quantity <= 0:
            return None, ["amount_invalid:negative_or_zero_quantity"]

        currency = str(component.currency or "").strip().upper()
        currency_source = "cell"
        if not currency:
            currency = str(
                understanding_sheet.get("currency")
                or context.context_defaults.get("currency")
                or ""
            ).strip().upper()
            currency_source = "context"
        if currency not in KNOWN_CURRENCIES:
            reasons.append("currency_missing")
            currency = ""

        unit, unit_scale = _normalize_unit(component.unit_name)
        if unit is None:
            unit_basis = str(
                understanding_sheet.get("unit_basis")
                or context.context_defaults.get("unit_basis")
                or ""
            )
            unit, basis_scale = _normalize_unit(unit_basis)
            unit_scale = unit_scale or basis_scale
        if unit is None:
            reasons.append("unit_unresolved")

        if unit == "token" and quantity == 1:
            if unit_scale == "million":
                quantity = Decimal(1_000_000)
            elif unit_scale == "thousand":
                quantity = Decimal(1_000)

        metric = self._resolve_metric(session, component)
        if metric is None:
            reasons.append("metric_unresolved")

        payload: dict[str, Any] = {
            "component_type": "monetary",
            "raw_amount": str(amount),
            "raw_unit_name": unit or "",
            "raw_unit_quantity": str(quantity),
            "conditions": dict(component.conditions),
            "tier_start": str(_decimal(component.tier_start or 0, "tier_start")),
            "tier_end": (
                None
                if component.tier_end is None
                else str(_decimal(component.tier_end, "tier_end"))
            ),
            "_currency_source": currency_source,
        }
        if currency:
            payload["raw_currency_code"] = currency
        if metric is not None:
            payload["billing_metric_id"] = metric.id
        return payload, reasons

    def _resolve_catalog(
        self,
        session: Session,
        context: ImportContext,
        extraction: RowExtractionModel,
        understanding_sheet: dict[str, Any],
        *,
        model_id: str,
        audit_rows: list[tuple[str, str, str, dict[str, Any]]],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        reasons: list[str] = []
        catalog = CatalogService(session)

        manufacturer_name = (
            extraction.manufacturer
            or understanding_sheet.get("manufacturer")
            or context.context_defaults.get("manufacturer")
        )
        provider_name = (
            extraction.provider
            or understanding_sheet.get("provider")
            or context.context_defaults.get("provider_name")
        )
        model_name = extraction.model_name
        if not model_name:
            return None, ["model_missing"]
        if not manufacturer_name:
            reasons.append("manufacturer_missing")
        if not provider_name:
            reasons.append("provider_missing")
        default_manufacturer = str(context.context_defaults.get("manufacturer") or "").strip()
        default_provider = str(context.context_defaults.get("provider_name") or "").strip()
        if (
            default_manufacturer
            and extraction.manufacturer
            and extraction.manufacturer.strip().casefold() != default_manufacturer.casefold()
        ):
            reasons.append("manufacturer_conflict")
        if (
            default_provider
            and extraction.provider
            and extraction.provider.strip().casefold() != default_provider.casefold()
        ):
            reasons.append("provider_conflict")
        if reasons:
            return None, reasons

        provider_type = str(
            context.context_defaults.get("provider_type")
            or understanding_sheet.get("provider_type")
            or "reseller"
        ).strip().casefold()
        if provider_type not in {"official", "reseller", "aggregator", "internal"}:
            provider_type = "reseller"

        try:
            manufacturer = catalog.create_manufacturer(str(manufacturer_name).strip())
            audit_rows.append(
                (
                    "manufacturer",
                    str(manufacturer.id),
                    f"created_or_reused manufacturer {manufacturer.name}",
                    {"name": manufacturer.name, "model_id": model_id},
                )
            )
            provider = catalog.create_provider(
                str(provider_name).strip(), provider_type=provider_type
            )
            audit_rows.append(
                (
                    "provider",
                    str(provider.id),
                    f"created_or_reused provider {provider.name}",
                    {"name": provider.name, "provider_type": provider_type,
                     "model_id": model_id},
                )
            )
            model = catalog.get_or_create_model(
                manufacturer.id, model_name.strip(), display_name=model_name.strip()
            )
            audit_rows.append(
                (
                    "standard_model",
                    str(model.id),
                    f"created_or_reused model {model.canonical_name}",
                    {"canonical_name": model.canonical_name,
                     "manufacturer": manufacturer.name, "model_id": model_id},
                )
            )
            version = None
            if extraction.model_version:
                version = catalog.get_or_create_version(
                    model.id, extraction.model_version.strip()
                )
            for alias in extraction.aliases:
                cleaned = alias.strip()
                if cleaned and normalize_model_name(cleaned) != model.canonical_name:
                    catalog.create_alias(
                        cleaned,
                        model.id,
                        provider_id=provider.id,
                        model_version_id=None if version is None else version.id,
                        alias_type="imported",
                        verified=False,
                        source_note=f"auto-extracted by {model_id}",
                    )
            offering = catalog.get_or_create_offering(
                provider.id,
                model.id,
                model_name.strip(),
                model_version_id=None if version is None else version.id,
            )
        except CatalogConflictError as exc:
            return None, [f"catalog_conflict:{exc}"]

        effective_from = (
            _parse_datetime(extraction.effective_from)
            or _parse_datetime(understanding_sheet.get("effective_from"))
            or _parse_datetime(context.context_defaults.get("effective_from"))
        )
        return (
            {
                "manufacturer": manufacturer,
                "provider": provider,
                "model": model,
                "version": version,
                "offering": offering,
                "effective_from": effective_from,
            },
            reasons,
        )

    # ------------------------------------------------------------------ #
    # row processing
    # ------------------------------------------------------------------ #

    def _process_row(
        self,
        session: Session,
        batch: ImportBatch,
        import_row: ImportRow,
        sheet: SheetProfile,
        row: RowRecord,
        extraction: RowExtractionModel | None,
        *,
        context: ImportContext,
        understanding: dict[str, Any],
        understanding_request_id: str,
        chat: ChatResult | None,
        candidates: Sequence[Any],
        audit_rows: list[tuple[str, str, str, dict[str, Any]]],
    ) -> _RowOutcome:
        if extraction is None:
            return _RowOutcome("rejected", ["no_extraction"])
        row_cell_refs = {cell.coordinate.upper() for cell in row.cells}
        reasons, evaluation = self._evaluate_extraction(
            session,
            batch,
            context,
            understanding,
            sheet.title,
            extraction,
            chat=chat,
            row_cell_refs=row_cell_refs,
            fallback_model_name=_model_name(row, sheet.headers),
            audit_rows=audit_rows,
        )
        component_payloads = evaluation["component_payloads"]
        catalog_info = evaluation["catalog_info"]
        snapshot_currency = evaluation["snapshot_currency"]
        fx_rate = evaluation["fx_rate"]
        observed_at = evaluation["observed_at"]
        effective_from = evaluation["effective_from"]
        model_name = evaluation["model_name"]

        staged = StagedQuote(
            import_row=import_row,
            provider_id=None if catalog_info is None else catalog_info["provider"].id,
            matched_standard_model_id=(
                None if catalog_info is None else catalog_info["model"].id
            ),
            matched_model_version_id=(
                None
                if catalog_info is None or catalog_info["version"] is None
                else catalog_info["version"].id
            ),
            matched_offering_id=(
                None if catalog_info is None else catalog_info["offering"].id
            ),
            extracted_model_name=model_name,
            extracted_plan_name=(
                (extraction.plan_name or "").strip() or None
            ),
            match_method="qwen",
            match_confidence=Decimal(str(round(extraction.confidence, 4))),
            review_status="needs_review" if reasons else "pending",
            extracted_quote={
                "schema_version": 3,
                "origin": "third_party",
                "model_match": {
                    "stage": "qwen_semantic",
                    "confidence": str(extraction.confidence),
                    "field_confidence": extraction.field_confidence,
                    "missing": extraction.missing,
                    "conflicts": extraction.conflicts,
                    "catalog_suggestions": extraction.catalog_suggestions,
                    "model": chat.model if chat else None,
                    "request_id": chat.request_id if chat else None,
                    "extraction_version": ROW_PROMPT_VERSION,
                    "understanding_request_id": understanding_request_id,
                },
                "candidates": [_candidate_payload(item) for item in candidates],
                "_auto_extraction": {
                    "validation_method": "auto_rules",
                    "actor": f"{AUTO_ACTOR_PREFIX}:{chat.model if chat else 'unknown'}",
                    "prompt_sha256": batch.import_context.get("prompt_sha256"),
                    "components": component_payloads,
                    "effective_from": effective_from.isoformat(),
                    "currency_code": snapshot_currency,
                    "observed_at": observed_at.isoformat(),
                },
            },
            validation_errors=reasons,
        )
        session.add(staged)
        session.flush()

        if reasons:
            for reason in reasons:
                staged.review_items.append(
                    ReviewItem(
                        review_type=_review_type(reason),
                        status="open",
                        severity="warning",
                        reason=reason,
                    )
                )
            session.flush()
            return _RowOutcome("review", reasons, staged)

        # ---- auto publish path ----
        return self._publish_approved(
            session,
            batch,
            staged,
            import_row,
            sheet_title=sheet.title,
            row_number=row.row_index,
            extraction=extraction,
            component_payloads=component_payloads,
            snapshot_currency=snapshot_currency,
            observed_at=observed_at,
            effective_from=effective_from,
            fx_rate=fx_rate,
            chat=chat,
        )

    def _evaluate_extraction(
        self,
        session: Session,
        batch: ImportBatch,
        context: ImportContext,
        understanding: dict[str, Any],
        sheet_title: str,
        extraction: RowExtractionModel,
        *,
        chat: ChatResult | None,
        row_cell_refs: set[str],
        fallback_model_name: str | None,
        audit_rows: list[tuple[str, str, str, dict[str, Any]]],
    ) -> tuple[list[str], dict[str, Any]]:
        reasons: list[str] = []
        understanding_sheet = self._sheet_understanding(understanding, sheet_title)

        if extraction.conflicts:
            reasons.append("conflicts:" + ";".join(extraction.conflicts[:4]))
        if extraction.confidence < self._min_confidence:
            reasons.append(f"low_confidence:{extraction.confidence:.2f}")
        for key in ("model_name", "components"):
            value = extraction.field_confidence.get(key, extraction.confidence)
            if value < self._min_confidence:
                reasons.append(f"low_field_confidence:{key}:{value:.2f}")

        component_payloads: list[dict[str, Any]] = []
        if not extraction.components:
            reasons.append("no_components")
        for component in extraction.components:
            payload, component_reasons = self._validate_component(
                session,
                component,
                sheet_title=sheet_title,
                row_cell_refs=row_cell_refs,
                context=context,
                understanding_sheet=understanding_sheet,
            )
            reasons.extend(component_reasons)
            if payload is not None:
                component_payloads.append(payload)

        model_name = extraction.model_name or fallback_model_name
        if not model_name:
            reasons.append("model_missing")

        observed_at = utc_now()

        snapshot_currency = ""
        if component_payloads:
            monetary = [
                payload
                for payload in component_payloads
                if payload.get("component_type") == "monetary"
            ]
            if monetary:
                currencies = {
                    payload.get("raw_currency_code")
                    for payload in monetary
                    if payload.get("raw_currency_code")
                }
                if len(currencies) == 1:
                    snapshot_currency = currencies.pop()
                else:
                    reasons.append("currency_ambiguous")
            else:
                default_currency = str(
                    self._sheet_understanding(understanding, sheet_title).get("currency")
                    or context.context_defaults.get("currency")
                    or ""
                ).strip().upper()
                if default_currency in KNOWN_CURRENCIES:
                    snapshot_currency = default_currency
                else:
                    reasons.append("currency_missing")

        fx_rate: FXRate | None = None
        if snapshot_currency and snapshot_currency != "CNY":
            fx_rate = session.scalar(
                select(FXRate).where(
                    FXRate.verification_status == "verified",
                    (
                        (FXRate.base_currency == snapshot_currency)
                        & (FXRate.quote_currency == "CNY")
                    )
                    | (
                        (FXRate.base_currency == "CNY")
                        & (FXRate.quote_currency == snapshot_currency)
                    ),
                ).order_by(FXRate.rate_date.desc(), FXRate.id.desc()).limit(1)
            )
            if fx_rate is None:
                reasons.append("fx_missing")
            else:
                # Keep the snapshot observation date aligned with the FX
                # evidence date so persisted normalisation stays valid.
                observed_at = datetime.combine(
                    fx_rate.rate_date, observed_at.timetz()
                )

        # Catalog writes only happen for rows that already passed every
        # confidence and rule check; failing rows keep their suggestions in
        # the staged payload without polluting the catalog.
        catalog_info = None
        if model_name and not reasons:
            effective_extraction = extraction
            if not effective_extraction.model_name:
                effective_extraction = extraction.model_copy(
                    update={"model_name": model_name}
                )
            catalog_info, catalog_reasons = self._resolve_catalog(
                session,
                context,
                effective_extraction,
                understanding_sheet,
                model_id=chat.model if chat else "unknown",
                audit_rows=audit_rows,
            )
            reasons.extend(catalog_reasons)

        effective_from = (
            _parse_datetime(extraction.effective_from)
            or _parse_datetime(understanding_sheet.get("effective_from"))
            or _parse_datetime(context.context_defaults.get("effective_from"))
            or observed_at
        )
        return reasons, {
            "component_payloads": component_payloads,
            "catalog_info": catalog_info,
            "snapshot_currency": snapshot_currency,
            "fx_rate": fx_rate,
            "observed_at": observed_at,
            "effective_from": effective_from,
            "model_name": model_name,
        }

    def _publish_approved(
        self,
        session: Session,
        batch: ImportBatch,
        staged: StagedQuote,
        import_row: ImportRow,
        *,
        sheet_title: str,
        row_number: int,
        extraction: RowExtractionModel,
        component_payloads: list[dict[str, Any]],
        snapshot_currency: str,
        observed_at: datetime,
        effective_from: datetime,
        fx_rate: FXRate | None,
        chat: ChatResult | None,
    ) -> _RowOutcome:
        plan_name = (extraction.plan_name or "").strip() or "standard"
        catalog = CatalogService(session)
        try:
            plan = catalog.get_or_create_plan(
                staged.matched_offering_id,
                plan_name,
                billing_mode="pay_as_you_go",
                currency_code=snapshot_currency,
                effective_from=effective_from,
            )
        except CatalogConflictError as exc:
            staged.review_status = "needs_review"
            staged.validation_errors = ["plan_conflict:" + str(exc)]
            staged.review_items.append(
                ReviewItem(
                    review_type="price",
                    status="open",
                    severity="warning",
                    reason="plan_conflict:" + str(exc),
                )
            )
            session.flush()
            return _RowOutcome("review", ["plan_conflict"], staged)

        components_payload = []
        for payload in component_payloads:
            entry = {key: value for key, value in payload.items() if not key.startswith("_")}
            entry["pricing_plan_id"] = plan.id
            components_payload.append(entry)
        staged.extracted_quote = {
            **staged.extracted_quote,
            "pricing_plan_id": plan.id,
            "plan_name": plan.name,
            "currency_code": snapshot_currency,
            "observed_at": observed_at.isoformat(),
            "effective_from": effective_from.isoformat(),
            "effective_to": None,
            "components": components_payload,
        }
        staged.review_status = "approved"
        staged.validation_errors = []
        session.flush()

        publication_key = hashlib.sha256(
            (
                f"{batch.idempotency_key}:{sheet_title}:{row_number}"
                f":{plan.id}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            snapshot = PricingService(session).publish(
                staged.id,
                pricing_plan_id=plan.id,
                idempotency_key="auto:" + publication_key,
            )
        except (PricingValidationError, IntegrityError) as exc:
            staged.review_status = "needs_review"
            staged.validation_errors = [f"publish_failed:{exc}"]
            staged.review_items.append(
                ReviewItem(
                    review_type="price",
                    status="open",
                    severity="error",
                    reason=f"publish_failed:{exc}",
                )
            )
            session.flush()
            return _RowOutcome("review", ["publish_failed"], staged)

        normalization_ids: list[int] = []
        normalization_failures: list[str] = []
        for component in snapshot.components:
            if component.component_type != "monetary":
                continue
            try:
                with session.begin_nested():
                    record = PricingService(session).store_normalization(
                        component,
                        fx_rate=None if fx_rate is None else fx_rate.id,
                    )
            except (PricingValidationError, IntegrityError) as exc:
                normalization_failures.append(
                    f"component {component.id}: {type(exc).__name__}"
                )
                continue
            normalization_ids.append(record.id)

        session.add(
            AuditEvent(
                actor=f"{AUTO_ACTOR_PREFIX}:{chat.model if chat else 'unknown'}",
                action="auto_publish_price",
                entity_type="price_snapshot",
                entity_id=str(snapshot.id),
                correlation_id=batch.idempotency_key,
                after_data={
                    "staged_quote_id": staged.id,
                    "validation_method": "auto_rules",
                    "model": chat.model if chat else None,
                    "request_id": chat.request_id if chat else None,
                    "extraction_version": ROW_PROMPT_VERSION,
                    "confidence": str(extraction.confidence),
                    "normalization_ids": normalization_ids,
                    "normalization_failures": normalization_failures,
                },
            )
        )
        session.flush()
        import_row.status = "accepted"
        session.flush()
        return _RowOutcome("accepted", [], staged)

    # ------------------------------------------------------------------ #
    # re-extraction of a single review row
    # ------------------------------------------------------------------ #

    def reextract_staged_quote(
        self, session: Session, staged_quote_id: int, *, actor: str
    ) -> StagedQuote:
        """Re-run semantic extraction for one staged row; publish if it now passes."""

        staged = session.get(StagedQuote, staged_quote_id)
        if staged is None:
            raise LookupError(f"staged quote {staged_quote_id} not found")
        if staged.review_status == "approved":
            raise ValueError("approved staged quotes are already published; not re-extracting")
        import_row = staged.import_row
        batch = import_row.import_batch
        semantic = batch.import_context.get("semantic")
        if not semantic or "table_understanding" not in semantic:
            raise ValueError(
                "batch has no stored table understanding; run a fresh semantic import"
            )
        context = ImportContext(
            data_source_id=batch.source_artifact.data_source_id,
            parser_version=batch.parser_version,
            provider_id=staged.provider_id or batch.provider_id,
            prompt_text=str(batch.import_context.get("prompt_text") or ""),
            context_defaults=dict(batch.import_context.get("context_defaults") or {}),
            interpretation_version=str(
                batch.import_context.get("interpretation_version")
                or "qwen-extract-v1"
            ),
        )
        cells = list(import_row.raw_data.get("cells") or [])
        payload = {"row_number": import_row.row_number, "cells": cells}
        with self._client() as client:
            try:
                chat = client.extract_rows(
                    user_context=self._user_context(context),
                    table_understanding=semantic["table_understanding"],
                    rows=[payload],
                )
            except SemanticQwenError as exc:
                staged.validation_errors = [
                    *staged.validation_errors,
                    f"model_{exc.kind}",
                ]
                staged.review_items.append(
                    ReviewItem(
                        review_type="other",
                        status="open",
                        severity="warning",
                        reason=f"model_{exc.kind}",
                    )
                )
                session.flush()
                return staged
        extraction: RowExtractionModel | None = None
        for item in chat.data.get("rows", []):
            try:
                candidate = RowExtractionModel.model_validate(item)
            except Exception:
                continue
            if candidate.row_number == import_row.row_number:
                extraction = candidate
                break
        if extraction is None:
            staged.validation_errors = [*staged.validation_errors, "no_extraction"]
            staged.review_items.append(
                ReviewItem(
                    review_type="other",
                    status="open",
                    severity="warning",
                    reason="no_extraction",
                )
            )
            session.flush()
            return staged

        row_cell_refs = {
            _cell_ref(str(cell.get("coordinate") or "")) for cell in cells
        }
        audit_rows: list[tuple[str, str, str, dict[str, Any]]] = []
        reasons, evaluation = self._evaluate_extraction(
            session,
            batch,
            context,
            semantic["table_understanding"],
            import_row.sheet_name,
            extraction,
            chat=chat,
            row_cell_refs=row_cell_refs,
            fallback_model_name=staged.extracted_model_name,
            audit_rows=audit_rows,
        )
        self._flush_catalog_audits(session, batch, chat, audit_rows, set())

        for item in staged.review_items:
            if item.status == "open":
                item.status = "resolved"
                item.resolved_by = actor
                item.resolved_at = utc_now()
                item.resolution = {"action": "reextracted"}
        staged.validation_errors = reasons
        staged.review_status = "needs_review" if reasons else "pending"
        staged.match_method = "qwen"
        staged.match_confidence = Decimal(str(round(extraction.confidence, 4)))
        quote = dict(staged.extracted_quote)
        quote["model_match"] = {
            "stage": "qwen_semantic",
            "confidence": str(extraction.confidence),
            "field_confidence": extraction.field_confidence,
            "missing": extraction.missing,
            "conflicts": extraction.conflicts,
            "catalog_suggestions": extraction.catalog_suggestions,
            "model": chat.model,
            "request_id": chat.request_id,
            "extraction_version": ROW_PROMPT_VERSION,
            "understanding_request_id": semantic.get(
                "table_understanding_request_id"
            ),
        }
        staged.extracted_quote = quote
        catalog_info = evaluation["catalog_info"]
        if catalog_info is not None:
            staged.provider_id = catalog_info["provider"].id
            staged.matched_standard_model_id = catalog_info["model"].id
            staged.matched_model_version_id = (
                None
                if catalog_info["version"] is None
                else catalog_info["version"].id
            )
            staged.matched_offering_id = catalog_info["offering"].id
        staged.extracted_model_name = evaluation["model_name"]
        session.flush()

        if reasons:
            for reason in reasons:
                staged.review_items.append(
                    ReviewItem(
                        review_type=_review_type(reason),
                        status="open",
                        severity="warning",
                        reason=reason,
                    )
                )
            session.flush()
            return staged
        outcome = self._publish_approved(
            session,
            batch,
            staged,
            import_row,
            sheet_title=import_row.sheet_name,
            row_number=import_row.row_number,
            extraction=extraction,
            component_payloads=evaluation["component_payloads"],
            snapshot_currency=evaluation["snapshot_currency"],
            observed_at=evaluation["observed_at"],
            effective_from=evaluation["effective_from"],
            fx_rate=evaluation["fx_rate"],
            chat=chat,
        )
        return outcome.staged or staged

    # ------------------------------------------------------------------ #
    # main flow: all model traffic happens outside the write transaction
    # ------------------------------------------------------------------ #

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
                if existing is not None and existing.status != "failed":
                    return _summary(session, existing, reused=True)

            parsed = parse_excel(path, profile=context.profile)
            selected = self._selected_sheets(parsed, context.sheet_names)
            model_plan = self._run_model_phase(parsed, selected, context)

            with _transaction(session):
                self._validate_context_entities(session, context)
                existing = session.scalar(
                    select(ImportBatch).where(ImportBatch.idempotency_key == key)
                )
                if existing is not None and existing.status != "failed":
                    return _summary(session, existing, reused=True)
                artifact = self._get_or_create_artifact(
                    session, path, artifact_sha256, parsed, context
                )
                if existing is not None:
                    _revive_failed_batch(existing)
                    batch = existing
                else:
                    batch = ImportBatch(
                        source_artifact=artifact,
                        provider_id=context.provider_id,
                        idempotency_key=key,
                        parser_version=context.parser_version,
                        status="processing",
                        initiated_by=context.initiated_by,
                    )
                    session.add(batch)
                batch.import_context = _json_safe(
                    {
                        **_context_payload(context),
                        "context_scope": "batch",
                        "context_version": 1,
                        "effective_sheet_names": [
                            sheet.title for sheet in selected
                        ],
                        "workbook_review_reasons": (
                            parsed.workbook.review_reasons
                        ),
                    }
                )
                session.flush()
                self._persist_model_results(
                    session, batch, parsed, selected, context, model_plan
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

    def _run_model_phase(
        self,
        parsed: ExcelParseResult,
        selected: Sequence[Any],
        context: ImportContext,
    ) -> dict[str, Any]:
        with self._client() as client:
            try:
                served_model = client.verify_model()
            except SemanticQwenError as exc:
                raise SemanticImportError(f"model preflight failed: {exc}") from exc
            user_context = self._user_context(context)
            summary = self._workbook_summary(parsed, selected)
            try:
                understanding_chat = client.understand_table(
                    user_context=user_context, workbook_summary=summary
                )
            except SemanticQwenError as exc:
                raise SemanticImportError(
                    f"table understanding failed: {exc}"
                ) from exc
            try:
                TableUnderstandingModel.model_validate(understanding_chat.data)
            except Exception as exc:
                raise SemanticImportError(
                    f"table understanding schema invalid: {exc}"
                ) from exc
            understanding = understanding_chat.data
            chunks: list[dict[str, Any]] = []
            for sheet in selected:
                data_rows = [
                    row
                    for row in sheet.rows
                    if _nonempty_data_row(row, sheet.header_row)
                ]
                for start in range(0, len(data_rows), self._row_batch_size):
                    chunk = data_rows[start : start + self._row_batch_size]
                    payloads = [self._row_payload(sheet, row) for row in chunk]
                    extraction_by_row: dict[int, RowExtractionModel] = {}
                    chat: ChatResult | None = None
                    chunk_error: str | None = None
                    try:
                        chat = client.extract_rows(
                            user_context=user_context,
                            table_understanding=understanding,
                            rows=payloads,
                        )
                        for item in chat.data.get("rows", []):
                            try:
                                extraction_by_row[int(item["row_number"])] = (
                                    RowExtractionModel.model_validate(item)
                                )
                            except Exception:
                                continue
                    except SemanticQwenError as exc:
                        chunk_error = f"model_{exc.kind}"
                    chunks.append(
                        {
                            "sheet": sheet,
                            "rows": list(zip(chunk, payloads)),
                            "extraction_by_row": extraction_by_row,
                            "chat": chat,
                            "chunk_error": chunk_error,
                        }
                    )
            return {
                "served_model": served_model,
                "understanding_chat": understanding_chat,
                "understanding": understanding,
                "chunks": chunks,
            }

    def _persist_model_results(
        self,
        session: Session,
        batch: ImportBatch,
        parsed: ExcelParseResult,
        selected: Sequence[Any],
        context: ImportContext,
        model_plan: dict[str, Any],
    ) -> None:
        understanding_chat = model_plan["understanding_chat"]
        understanding = model_plan["understanding"]
        batch.import_context = {
            **batch.import_context,
            "semantic": {
                "model": understanding_chat.model,
                "served_model": model_plan["served_model"],
                "table_prompt_version": TABLE_PROMPT_VERSION,
                "row_prompt_version": ROW_PROMPT_VERSION,
                "table_understanding_request_id": understanding_chat.request_id,
                "table_understanding": understanding,
                "table_understanding_usage": understanding_chat.usage,
                "min_confidence": self._min_confidence,
                "row_batch_size": self._row_batch_size,
            },
        }
        session.flush()

        candidates_by_row: dict[tuple[str, int], list[Any]] = {}
        for candidate in parsed.staged_quotes:
            candidates_by_row.setdefault(
                (candidate.sheet_name, candidate.source_row), []
            ).append(candidate)

        total = accepted = rejected = review = 0
        audit_rows: list[tuple[str, str, str, dict[str, Any]]] = []
        audited: set[tuple[str, str]] = set()
        for chunk in model_plan["chunks"]:
            sheet = chunk["sheet"]
            chat = chunk["chat"]
            chunk_error = chunk["chunk_error"]
            extraction_by_row = chunk["extraction_by_row"]
            for row, payload in chunk["rows"]:
                total += 1
                raw_data = {
                    "row_number": row.row_index,
                    "cells": payload["cells"],
                    "needs_review": row.needs_review,
                    "review_reasons": list(row.review_reasons),
                }
                row_hash = hashlib.sha256(
                    json.dumps(
                        raw_data, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                candidates = candidates_by_row.get((sheet.title, row.row_index), [])
                import_row = ImportRow(
                    import_batch=batch,
                    sheet_name=sheet.title,
                    row_number=row.row_index,
                    row_hash=row_hash,
                    raw_data=_json_safe(raw_data),
                    status="staged",
                )
                session.add(import_row)
                session.flush()

                if chunk_error is not None:
                    review += 1
                    staged = StagedQuote(
                        import_row=import_row,
                        provider_id=context.provider_id,
                        extracted_model_name=_model_name(row, sheet.headers),
                        match_method=None,
                        review_status="needs_review",
                        extracted_quote={
                            "schema_version": 3,
                            "model_match": {"stage": "qwen_semantic",
                                            "error": chunk_error},
                            "candidates": [
                                _candidate_payload(item) for item in candidates
                            ],
                        },
                        validation_errors=[chunk_error],
                    )
                    session.add(staged)
                    staged.review_items.append(
                        ReviewItem(
                            review_type="other",
                            status="open",
                            severity="warning",
                            reason=chunk_error,
                        )
                    )
                    session.flush()
                    continue

                outcome = self._process_row(
                    session,
                    batch,
                    import_row,
                    sheet,
                    row,
                    extraction_by_row.get(row.row_index),
                    context=context,
                    understanding=understanding,
                    understanding_request_id=understanding_chat.request_id,
                    chat=chat,
                    candidates=candidates,
                    audit_rows=audit_rows,
                )
                self._flush_catalog_audits(
                    session, batch, chat, audit_rows, audited
                )
                if outcome.status == "accepted":
                    accepted += 1
                elif outcome.status == "review":
                    review += 1
                else:
                    rejected += 1
                    import_row.status = "rejected"
                    import_row.error_code = ",".join(outcome.reasons) or "no_extraction"
                    session.flush()

        batch.total_rows = total
        batch.accepted_rows = accepted
        batch.rejected_rows = rejected
        batch.review_rows = review

    @staticmethod
    def _flush_catalog_audits(
        session: Session,
        batch: ImportBatch,
        chat: ChatResult | None,
        audit_rows: list[tuple[str, str, str, dict[str, Any]]],
        audited: set[tuple[str, str]],
    ) -> None:
        """Record one audit event per distinct catalog record per batch."""

        model_id = chat.model if chat is not None else "unknown"
        for entity_type, entity_id, reason, after in audit_rows:
            key = (entity_type, entity_id)
            if key in audited:
                continue
            audited.add(key)
            session.add(
                AuditEvent(
                    actor=f"{AUTO_ACTOR_PREFIX}:{model_id}",
                    action="auto_resolve_catalog_record",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    correlation_id=batch.idempotency_key,
                    after_data={**after, "reason": reason},
                )
            )
        audit_rows.clear()
        session.flush()


def _review_type(reason: str) -> str:
    lowered = reason.lower()
    if "currency" in lowered or "fx" in lowered:
        return "currency"
    if "unit" in lowered or "metric" in lowered:
        return "unit"
    if "model" in lowered or "catalog" in lowered:
        return "model_match"
    if any(token in lowered for token in ("price", "discount", "publish", "plan", "amount")):
        return "price"
    return "other"
