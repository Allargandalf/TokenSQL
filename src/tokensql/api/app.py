"""FastAPI application exposing TokenSQL's validated service layer."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, TypeVar
import zipfile
from xml.etree import ElementTree

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl.utils.cell import range_boundaries
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload

from tokensql.api.dependencies import get_session
from tokensql.config import get_settings
from tokensql.api.schemas import (
    AliasCreateRequest,
    AliasResponse,
    ComparisonDifferenceResponse,
    ConfirmStagedQuoteRequest,
    FXRateCreateRequest,
    FXRateResponse,
    HealthResponse,
    ImportContextResponse,
    ImportRequest,
    ImportSummaryResponse,
    ModelResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    PriceComponentResponse,
    PriceNormalizationCreateRequest,
    PriceNormalizationResponse,
    PriceSnapshotResponse,
    PublicationResponse,
    PublishOfficialQuoteRequest,
    PublishStagedQuoteRequest,
    PricingPlanResponse,
    QwenHealthResponse,
    ProfileContextRequest,
    ProviderResponse,
    ReExtractRequest,
    ReviewApproveRequest,
    ReviewRejectRequest,
    ReviewResponse,
    SheetProfileResponse,
    HeaderProfileResponse,
    StagedProfileResponse,
    StagedQuoteResponse,
    WorkbookProfileResponse,
)
from tokensql.db.models import (
    AuditEvent,
    DataSource,
    FXRate,
    ImportBatch,
    PriceComponent,
    PriceNormalization,
    PriceSnapshot,
    PricingPlan,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.importers import ParserProfile, parse_excel
from tokensql.services.catalog_service import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogService,
)
from tokensql.services.import_service import (
    ExcelImportService,
    ImportContext,
    ImportSummary,
)
from tokensql.services.semantic_import_service import (
    SemanticExcelImportService,
    SemanticImportError,
)
from tokensql.services.pricing_service import (
    DuplicatePublicationError,
    PricingNotFoundError,
    PricingService,
    PricingValidationError,
)
from tokensql.services.official_service import (
    OfficialPriceService,
    OfficialSourceValidationError,
)
from tokensql.services.staged_quote_service import (
    StagedQuoteConfirmationError,
    StagedQuoteConfirmationService,
    StagedQuoteNotFoundError,
)
from tokensql.services.review_service import (
    AliasRuleConflictError,
    ReviewNotFoundError,
    ReviewService,
    ReviewStateError,
)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ZIP_MEMBERS = 1_024
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_WORKSHEET_ROWS = 200_000
MAX_WORKSHEET_COLUMNS = 2_048
SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _public_detail(error: Exception) -> str:
    message = " ".join(str(error).split())
    if not message or "/" in message or "\\" in message:
        return "request could not be processed"
    return message[:500]


def _parse_json_form(raw: str, schema: type[SchemaT], label: str) -> SchemaT:
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return schema.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        raise HTTPException(
            status_code=422, detail=f"{label} must be a valid JSON object"
        ) from None


def _parser_profile(value: Any) -> ParserProfile:
    headers = value.price_headers
    kwargs: dict[str, Any] = {
        "header_row": value.header_row,
        "discount_scale": value.discount_scale,
    }
    if headers is not None:
        kwargs["price_headers"] = tuple(headers)
    return ParserProfile(**kwargs)


def _validate_xlsx_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ValueError("workbook contains too many ZIP members")

            expanded = 0
            names: set[str] = set()
            for member in members:
                name = member.filename
                posix = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or posix.is_absolute()
                    or ".." in posix.parts
                    or (posix.parts and ":" in posix.parts[0])
                ):
                    raise ValueError("workbook contains an unsafe ZIP member path")
                if member.flag_bits & 0x1:
                    raise ValueError("encrypted workbook members are not supported")
                if member.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise ValueError("workbook ZIP member is too large")
                expanded += member.file_size
                if expanded > MAX_ZIP_EXPANDED_BYTES:
                    raise ValueError("workbook expands beyond the safe limit")
                if member.file_size:
                    if member.compress_size == 0:
                        raise ValueError("workbook ZIP member has an invalid size")
                    ratio = member.file_size / member.compress_size
                    if ratio > MAX_ZIP_COMPRESSION_RATIO:
                        raise ValueError("workbook ZIP compression ratio is unsafe")
                names.add(name)

            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("ZIP archive is not an Excel workbook")

            for name in sorted(names):
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                root = ElementTree.fromstring(archive.read(name))
                dimension = root.find(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}dimension"
                )
                reference = None if dimension is None else dimension.get("ref")
                if not reference:
                    continue
                try:
                    _, _, max_column, max_row = range_boundaries(reference)
                except (TypeError, ValueError):
                    raise ValueError(
                        "worksheet declares an invalid dimension"
                    ) from None
                if (
                    max_row > MAX_WORKSHEET_ROWS
                    or max_column > MAX_WORKSHEET_COLUMNS
                ):
                    raise ValueError(
                        "worksheet declared dimension exceeds the safe limit"
                    )
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ElementTree.ParseError) as error:
        raise ValueError("invalid Excel workbook archive") from error


async def _save_xlsx(upload: UploadFile) -> tuple[Path, str]:
    handle = None
    path: Path | None = None
    completed = False
    try:
        raw_filename = (upload.filename or "").replace("\\", "/")
        filename = raw_filename.rsplit("/", 1)[-1]
        if (
            not filename
            or len(filename) > 255
            or any(ord(character) < 32 for character in filename)
        ):
            raise HTTPException(status_code=422, detail="invalid upload filename")
        if not filename.casefold().endswith(".xlsx"):
            raise HTTPException(
                status_code=415, detail="only .xlsx files are supported"
            )

        handle = tempfile.NamedTemporaryFile(
            mode="wb", prefix="tokensql-upload-", suffix=".xlsx", delete=False
        )
        path = Path(handle.name)
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total = handle.tell() + len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413, detail="upload exceeds the 10 MiB limit"
                )
            handle.write(chunk)
        handle.close()
        if path.stat().st_size == 0 or not zipfile.is_zipfile(path):
            raise HTTPException(status_code=422, detail="invalid Excel workbook")
        try:
            _validate_xlsx_archive(path)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        completed = True
        return path, filename
    except BaseException:
        if handle is not None:
            handle.close()
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        if handle is not None:
            handle.close()
        try:
            await upload.close()
        except BaseException:
            if completed and path is not None:
                path.unlink(missing_ok=True)
            raise


def _model_response(value: Any) -> ModelResponse:
    return ModelResponse(
        id=value.id,
        manufacturer_id=value.manufacturer_id,
        canonical_name=value.canonical_name,
        display_name=value.display_name,
        family=value.family,
        modality=value.modality,
        is_active=value.is_active,
        attributes=value.attributes,
    )


def _provider_response(value: Any) -> ProviderResponse:
    return ProviderResponse(
        id=value.id,
        name=value.name,
        slug=value.slug,
        provider_type=value.provider_type,
        website_url=value.website_url,
        is_active=value.is_active,
        attributes=value.attributes,
    )


def _plan_response(value: Any) -> PricingPlanResponse:
    return PricingPlanResponse(
        id=value.id,
        offering_id=value.offering_id,
        name=value.name,
        billing_mode=value.billing_mode,
        currency_code=value.currency_code,
        effective_from=value.effective_from,
        effective_to=value.effective_to,
        minimum_commitment_amount=value.minimum_commitment_amount,
        terms=value.terms,
    )


def _review_response(value: Any) -> ReviewResponse:
    return ReviewResponse(
        id=value.id,
        staged_quote_id=value.staged_quote_id,
        review_type=value.review_type,
        status=value.status,
        severity=value.severity,
        reason=value.reason,
        proposed_resolution=value.proposed_resolution,
        resolution=value.resolution,
        resolved_by=value.resolved_by,
        resolved_at=value.resolved_at,
    )


def _staged_response(value: StagedQuote) -> StagedQuoteResponse:
    row = value.import_row
    batch = row.import_batch
    artifact = batch.source_artifact
    from tokensql.api.schemas import StagedSourceResponse

    return StagedQuoteResponse(
        id=value.id,
        import_batch_id=batch.id,
        row_number=row.row_number,
        sheet_name=row.sheet_name,
        provider_id=value.provider_id,
        matched_standard_model_id=value.matched_standard_model_id,
        matched_model_version_id=value.matched_model_version_id,
        matched_offering_id=value.matched_offering_id,
        extracted_model_name=value.extracted_model_name,
        extracted_plan_name=value.extracted_plan_name,
        match_method=value.match_method,
        review_status=value.review_status,
        validation_errors=value.validation_errors,
        extracted_quote=value.extracted_quote,
        source=StagedSourceResponse(
            data_source_id=artifact.data_source_id,
            source_artifact_id=artifact.id,
            original_filename=artifact.original_filename,
            sha256=artifact.sha256,
        ),
        reviews=[_review_response(item) for item in value.review_items],
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _alias_response(value: Any) -> AliasResponse:
    return AliasResponse(
        id=value.id,
        alias=value.alias,
        normalized_alias=value.normalized_alias,
        alias_type=value.alias_type,
        provider_id=value.provider_id,
        standard_model_id=value.standard_model_id,
        model_version_id=value.model_version_id,
        confidence=value.confidence,
        is_verified=value.is_verified,
        source_note=value.source_note,
    )


def _component_response(value: Any) -> PriceComponentResponse:
    return PriceComponentResponse(
        id=value.id,
        billing_metric_id=value.billing_metric_id,
        metric_code=value.billing_metric.code,
        component_type=value.component_type,
        tier_start=value.tier_start,
        tier_end=value.tier_end,
        raw_amount=value.raw_amount,
        raw_currency_code=value.raw_currency_code,
        raw_unit_name=value.raw_unit_name,
        raw_unit_quantity=value.raw_unit_quantity,
        discount_rate=value.discount_rate,
        conditions=value.conditions,
    )


def _snapshot_response(value: Any) -> PriceSnapshotResponse:
    return PriceSnapshotResponse(
        id=value.id,
        pricing_plan_id=value.pricing_plan_id,
        data_source_id=value.data_source_id,
        source_artifact_id=value.source_artifact_id,
        supersedes_snapshot_id=value.supersedes_snapshot_id,
        observed_at=value.observed_at,
        effective_from=value.effective_from,
        effective_to=value.effective_to,
        currency_code=value.currency_code,
        verification_status=value.verification_status,
        components=[_component_response(item) for item in value.components],
    )


def _existing_publication(
    session: Session,
    *,
    staged_quote_id: int,
    source_artifact_id: int,
    idempotency_key: str,
    pricing_plan_id: int | None,
) -> PriceSnapshot | None:
    statement = (
        select(PriceSnapshot)
        .options(
            selectinload(PriceSnapshot.components).joinedload(
                PriceComponent.billing_metric
            )
        )
        .where(PriceSnapshot.source_artifact_id == source_artifact_id)
    )
    if pricing_plan_id is not None:
        statement = statement.where(
            PriceSnapshot.pricing_plan_id == pricing_plan_id
        )
    for snapshot in session.scalars(statement):
        publication = (snapshot.raw_payload or {}).get("_publication", {})
        if (
            publication.get("staged_quote_id") == staged_quote_id
            and publication.get("idempotency_key") == idempotency_key
        ):
            return snapshot
    return None


def _fx_rate_response(value: FXRate, *, reused: bool) -> FXRateResponse:
    return FXRateResponse(
        id=value.id,
        base_currency=value.base_currency,
        quote_currency=value.quote_currency,
        rate=value.rate,
        rate_date=value.rate_date,
        data_source_id=value.data_source_id,
        observed_at=value.observed_at,
        verification_status=value.verification_status,
        reused_existing=reused,
    )


def _normalization_response(
    value: PriceNormalization, *, reused: bool
) -> PriceNormalizationResponse:
    return PriceNormalizationResponse(
        id=value.id,
        price_component_id=value.price_component_id,
        fx_rate_id=value.fx_rate_id,
        algorithm_version=value.algorithm_version,
        comparison_key=value.comparison_key,
        verification_status=value.verification_status,
        normalized_amount=value.normalized_amount,
        normalized_currency_code=value.normalized_currency_code,
        normalized_unit_name=value.normalized_unit_name,
        normalized_unit_quantity=value.normalized_unit_quantity,
        formula=value.formula,
        created_at=value.created_at,
        reused_existing=reused,
    )


def _summary_response(value: ImportSummary) -> ImportSummaryResponse:
    return ImportSummaryResponse(**value.__dict__)


def _profile_response(result: Any) -> WorkbookProfileResponse:
    workbook = result.workbook
    sheets = [
        SheetProfileResponse(
            title=sheet.title,
            sheet_index=sheet.sheet_index,
            max_row=sheet.max_row,
            max_column=sheet.max_column,
            header_row=sheet.header_row,
            data_row_count=sheet.data_row_count,
            merge_ranges=sheet.merge_ranges,
            headers=[
                HeaderProfileResponse(
                    column_index=header.column_index,
                    column_letter=header.column_letter,
                    coordinate=header.coordinate,
                    raw_value=header.raw_value,
                    normalized_value=header.normalized_value,
                    needs_review=header.needs_review,
                    review_reasons=header.review_reasons,
                )
                for header in sheet.headers
            ],
            needs_review=sheet.needs_review,
            review_reasons=sheet.review_reasons,
        )
        for sheet in workbook.sheets
    ]
    staged = [
        StagedProfileResponse(
            sheet_name=item.sheet_name,
            source_row=item.source_row,
            source_coordinate=item.source_coordinate,
            price_kind=item.parsed_price.kind.value,
            amount=item.parsed_price.amount,
            currency=item.parsed_price.currency,
            unit=item.parsed_price.unit,
            discount_multiplier=item.parsed_price.discount_multiplier,
            condition_text=item.parsed_price.condition_text,
            needs_review=item.needs_review,
            review_reasons=item.review_reasons,
        )
        for item in result.staged_quotes
    ]
    return WorkbookProfileResponse(
        source_name=workbook.source_name,
        sha256=workbook.sha256,
        byte_size=workbook.byte_size,
        sheets=sheets,
        needs_review=workbook.needs_review,
        review_reasons=workbook.review_reasons,
        staged_quotes=staged,
    )




def create_app() -> FastAPI:
    api = FastAPI(title="TokenSQL API", version="0.1.0")

    @api.exception_handler(StagedQuoteNotFoundError)
    @api.exception_handler(CatalogNotFoundError)
    @api.exception_handler(PricingNotFoundError)
    @api.exception_handler(ReviewNotFoundError)
    async def not_found_handler(request: Any, error: Exception):
        del request
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": _public_detail(error)})

    @api.exception_handler(CatalogConflictError)
    @api.exception_handler(AliasRuleConflictError)
    @api.exception_handler(DuplicatePublicationError)
    @api.exception_handler(ReviewStateError)
    @api.exception_handler(IntegrityError)
    async def conflict_handler(request: Any, error: Exception):
        del request
        from fastapi.responses import JSONResponse
        detail = (
            "database conflict"
            if isinstance(error, IntegrityError)
            else _public_detail(error)
        )
        return JSONResponse(status_code=409, content={"detail": detail})
    @api.exception_handler(StagedQuoteConfirmationError)

    @api.exception_handler(OfficialSourceValidationError)
    @api.exception_handler(PricingValidationError)
    @api.exception_handler(ValueError)
    async def business_validation_handler(request: Any, error: Exception):
        del request
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": _public_detail(error)})

    @api.exception_handler(SemanticImportError)
    async def semantic_unavailable_handler(request: Any, error: Exception):
        del request
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": _public_detail(error)})

    @api.get("/health", response_model=HealthResponse)
    def health(session: Session = Depends(get_session)) -> HealthResponse:
        try:
            session.execute(select(1))
        except SQLAlchemyError:
            raise HTTPException(
                status_code=503, detail="database unavailable"
            ) from None
        settings = get_settings()
        return HealthResponse(
            status="ok",
            database="available",
            qwen=QwenHealthResponse(
                enabled=settings.qwen_enabled,
                configured=settings.qwen_configured,
            ),
        )

    @api.get("/models", response_model=list[ModelResponse])
    def models(session: Session = Depends(get_session)) -> list[ModelResponse]:
        return [_model_response(value) for value in CatalogService(session).list_models()]

    @api.get("/providers", response_model=list[ProviderResponse])
    def providers(session: Session = Depends(get_session)) -> list[ProviderResponse]:
        return [
            _provider_response(value)
            for value in CatalogService(session).list_providers()
        ]

    @api.get("/pricing-plans", response_model=list[PricingPlanResponse])
    def pricing_plans(
        offering_id: int | None = Query(default=None, gt=0),
        session: Session = Depends(get_session),
    ) -> list[PricingPlanResponse]:
        if offering_id is None:
            values = session.scalars(
                select(PricingPlan).order_by(
                    PricingPlan.offering_id, PricingPlan.effective_from, PricingPlan.id
                )
            )
            return [_plan_response(value) for value in values]
        return [
            _plan_response(value)
            for value in CatalogService(session).list_plans(offering_id)
        ]

    @api.post("/imports/profile", response_model=WorkbookProfileResponse)
    async def import_profile(
        file: UploadFile = File(...),
        context: str = Form(default="{}"),
    ) -> WorkbookProfileResponse:
        parsed_context = _parse_json_form(context, ProfileContextRequest, "context")
        path, filename = await _save_xlsx(file)
        try:
            try:
                result = parse_excel(path, profile=_parser_profile(parsed_context.profile))
            except Exception:
                raise HTTPException(
                    status_code=422, detail="invalid Excel workbook"
                ) from None
            response = _profile_response(result)
            return response.model_copy(update={"source_name": filename})
        finally:
            path.unlink(missing_ok=True)

    @api.post("/imports", response_model=ImportSummaryResponse)
    async def import_workbook(
        file: UploadFile = File(...),
        request: str = Form(...),
        session: Session = Depends(get_session),
    ) -> ImportSummaryResponse:
        body = _parse_json_form(request, ImportRequest, "request")
        path, filename = await _save_xlsx(file)
        try:
            settings = get_settings()
            context = ImportContext(
                data_source_id=body.data_source_id,
                parser_version=body.parser_version,
                profile=_parser_profile(body.profile),
                sheet_names=body.sheet_names,
                provider_id=body.provider_id,
                vendor_context={
                    **body.vendor_context,
                    "original_filename": filename,
                },
                initiated_by=body.initiated_by,
                prompt_text=body.prompt_text,
                context_defaults=body.context_defaults,
                interpretation_version=(
                    settings.qwen_extraction_version
                    if body.prompt_text and settings.qwen_enabled
                    else "deterministic-v1"
                ),
            )
            if body.prompt_text and settings.qwen_enabled:
                service: Any = SemanticExcelImportService(settings=settings)
            else:
                service = ExcelImportService()
            try:
                result = service.import_file(session, path, context)
            except SemanticImportError:
                raise
            except ValueError as error:
                raise HTTPException(
                    status_code=422, detail=_public_detail(error)
                ) from None
            except Exception as error:
                if isinstance(error, HTTPException):
                    raise
                raise HTTPException(
                    status_code=422, detail="workbook import failed validation"
                ) from None
            return _summary_response(result)
        finally:
            path.unlink(missing_ok=True)

    @api.get(
        "/imports/{batch_id}/context", response_model=ImportContextResponse
    )
    def import_context(
        batch_id: int,
        session: Session = Depends(get_session),
    ) -> ImportContextResponse:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404, detail=f"import batch {batch_id} not found"
            )
        return ImportContextResponse(
            batch_id=batch.id, import_context=dict(batch.import_context or {})
        )

    @api.get("/staged-quotes", response_model=list[StagedQuoteResponse])
    def staged_quotes(
        review_status: str | None = Query(
            default=None,
            pattern="^(pending|approved|rejected|needs_review)$",
        ),
        import_batch_id: int | None = Query(default=None, gt=0),
        limit: int = Query(default=100, ge=1, le=1000),
        session: Session = Depends(get_session),
    ) -> list[StagedQuoteResponse]:
        values = StagedQuoteConfirmationService(session).list(
            review_status=review_status,
            import_batch_id=import_batch_id,
            limit=limit,
        )
        return [_staged_response(value) for value in values]

    @api.get(
        "/staged-quotes/{staged_quote_id}", response_model=StagedQuoteResponse
    )
    def staged_quote(
        staged_quote_id: int,
        session: Session = Depends(get_session),
    ) -> StagedQuoteResponse:
        return _staged_response(
            StagedQuoteConfirmationService(session).get(staged_quote_id)
        )

    @api.post(
        "/staged-quotes/{staged_quote_id}/confirm",
        response_model=StagedQuoteResponse,
    )
    def confirm_staged_quote(
        staged_quote_id: int,
        body: ConfirmStagedQuoteRequest,
        session: Session = Depends(get_session),
    ) -> StagedQuoteResponse:
        value = StagedQuoteConfirmationService(session).confirm(
            staged_quote_id,
            actor=body.actor,
            source_artifact_id=body.source_artifact_id,
            provider_id=body.provider_id,
            standard_model_id=body.standard_model_id,
            model_version_id=body.model_version_id,
            offering_id=body.offering_id,
            pricing_plan_id=body.pricing_plan_id,
            currency_code=body.currency_code,
            observed_at=body.observed_at,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            components=[item.model_dump() for item in body.components],
            note=body.note,
        )
        return _staged_response(value)

    @api.post("/staged-quotes/{staged_quote_id}/re-extract",
              response_model=StagedQuoteResponse)
    def reextract_staged_quote(
        staged_quote_id: int,
        body: ReExtractRequest,
        session: Session = Depends(get_session),
    ) -> StagedQuoteResponse:
        if not get_settings().qwen_enabled:
            raise HTTPException(
                status_code=409, detail="Qwen semantic pipeline is disabled"
            )
        service = SemanticExcelImportService()
        try:
            value = service.reextract_staged_quote(
                session, staged_quote_id, actor=body.actor
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=_public_detail(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=_public_detail(error)) from None
        return _staged_response(value)

    @api.get("/reviews", response_model=list[ReviewResponse])
    def reviews(
        status: str = Query(default="open", pattern="^(open|resolved|dismissed)$"),
        severity: str | None = None,
        review_type: str | None = None,
        limit: int | None = Query(default=None, ge=1, le=1000),
        session: Session = Depends(get_session),
    ) -> list[ReviewResponse]:
        if status == "open":
            values = ReviewService(session).list_pending(
                severity=severity, review_type=review_type, limit=limit
            )
        else:
            statement = select(ReviewItem).where(ReviewItem.status == status)
            if severity is not None:
                statement = statement.where(ReviewItem.severity == severity)
            if review_type is not None:
                statement = statement.where(ReviewItem.review_type == review_type)
            statement = statement.order_by(ReviewItem.created_at, ReviewItem.id)
            if limit is not None:
                statement = statement.limit(limit)
            values = list(session.scalars(statement))
        return [_review_response(value) for value in values]

    @api.post("/reviews/{review_id}/approve", response_model=ReviewResponse)
    def approve_review(
        review_id: int,
        body: ReviewApproveRequest,
        session: Session = Depends(get_session),
    ) -> ReviewResponse:
        value = ReviewService(session).approve(
            review_id,
            reviewer=body.reviewer,
            resolution=body.resolution,
            create_alias_rule=body.create_alias_rule,
        )
        return _review_response(value)

    @api.post("/reviews/{review_id}/reject", response_model=ReviewResponse)
    def reject_review(
        review_id: int,
        body: ReviewRejectRequest,
        session: Session = Depends(get_session),
    ) -> ReviewResponse:
        value = ReviewService(session).reject(
            review_id, reviewer=body.reviewer, resolution=body.resolution
        )
        return _review_response(value)


    @api.post("/aliases", response_model=AliasResponse)
    def create_alias(
        body: AliasCreateRequest,
        session: Session = Depends(get_session),
    ) -> AliasResponse:
        value = CatalogService(session).create_alias(
            **body.model_dump(),
            verified=False,
            human_confirmed=False,
        )
        return _alias_response(value)

    @api.post("/prices/publish", response_model=PublicationResponse)
    def publish_reviewed_quote(
        body: PublishStagedQuoteRequest,
        session: Session = Depends(get_session),
    ) -> PublicationResponse:
        staged = session.get(StagedQuote, body.staged_quote_id)
        if staged is None:
            raise PricingNotFoundError(
                f"staged quote {body.staged_quote_id} not found"
            )
        artifact = session.get(SourceArtifact, body.source_artifact_id)
        if artifact is None:
            raise PricingNotFoundError(
                f"source artifact {body.source_artifact_id} not found"
            )
        if not artifact.data_source.is_active:
            raise PricingValidationError("data source is inactive")
        if artifact.data_source.source_type == "official":
            raise PricingValidationError(
                "official staged quotes require the official publication endpoint"
            )
        staged_source_id = staged.import_row.import_batch.source_artifact_id
        if staged_source_id != body.source_artifact_id:
            raise PricingValidationError(
                "source artifact does not belong to the staged quote"
            )
        if staged.review_status != "approved" or staged.validation_errors:
            raise PricingValidationError(
                "only validated, approved staged quotes may be published"
            )
        statuses = {item.status for item in staged.review_items}
        if "dismissed" in statuses:
            raise PricingValidationError("rejected review items cannot be published")
        if "open" in statuses:
            raise PricingValidationError("all review items must be resolved")
        existing = _existing_publication(
            session,
            staged_quote_id=staged.id,
            source_artifact_id=artifact.id,
            idempotency_key=body.idempotency_key,
            pricing_plan_id=body.pricing_plan_id,
        )
        if existing is not None:
            return PublicationResponse(
                snapshot=_snapshot_response(existing), reused_existing=True
            )
        try:
            snapshot = PricingService(session).publish(
                staged.id,
                pricing_plan_id=body.pricing_plan_id,
                idempotency_key=body.idempotency_key,
            )
        except DuplicatePublicationError:
            existing = _existing_publication(
                session,
                staged_quote_id=staged.id,
                source_artifact_id=artifact.id,
                idempotency_key=body.idempotency_key,
                pricing_plan_id=body.pricing_plan_id,
            )
            if existing is None:
                raise
            return PublicationResponse(
                snapshot=_snapshot_response(existing), reused_existing=True
            )
        session.add(
            AuditEvent(
                actor=body.actor,
                action="publish_reviewed_price",
                entity_type="price_snapshot",
                entity_id=str(snapshot.id),
                correlation_id=body.idempotency_key,
                after_data={
                    "staged_quote_id": staged.id,
                    "source_artifact_id": artifact.id,
                    "verification_status": snapshot.verification_status,
                },
            )
        )
        session.flush()
        return PublicationResponse(
            snapshot=_snapshot_response(snapshot), reused_existing=False
        )

    @api.post("/official-prices/publish", response_model=PublicationResponse)
    def publish_official_quote(
        body: PublishOfficialQuoteRequest,
        session: Session = Depends(get_session),
    ) -> PublicationResponse:
        staged = session.get(StagedQuote, body.staged_quote_id)
        if staged is None:
            raise PricingNotFoundError(
                f"staged quote {body.staged_quote_id} not found"
            )
        artifact = session.get(SourceArtifact, body.source_artifact_id)
        if artifact is None:
            raise PricingNotFoundError(
                f"source artifact {body.source_artifact_id} not found"
            )
        staged_source_id = staged.import_row.import_batch.source_artifact_id
        if staged_source_id != artifact.id:
            raise OfficialSourceValidationError(
                "source artifact does not belong to the staged official quote"
            )
        if artifact.data_source_id != body.data_source_id:
            raise OfficialSourceValidationError(
                "data source does not own the source artifact"
            )
        existing = _existing_publication(
            session,
            staged_quote_id=staged.id,
            source_artifact_id=artifact.id,
            idempotency_key=body.idempotency_key,
            pricing_plan_id=body.pricing_plan_id,
        )
        if existing is not None:
            return PublicationResponse(
                snapshot=_snapshot_response(existing), reused_existing=True
            )
        service = OfficialPriceService(
            session,
            data_source_id=body.data_source_id,
            client=None,
            adapters=(),
        )
        snapshot = service.publish_verified(
            staged.id,
            verified=True,
            verified_by=body.actor,
            pricing_plan_id=body.pricing_plan_id,
            idempotency_key=body.idempotency_key,
        )
        return PublicationResponse(
            snapshot=_snapshot_response(snapshot), reused_existing=False
        )

    @api.post("/fx-rates", response_model=FXRateResponse)
    def create_fx_rate(
        body: FXRateCreateRequest,
        session: Session = Depends(get_session),
    ) -> FXRateResponse:
        source = session.get(DataSource, body.data_source_id)
        if source is None:
            raise PricingNotFoundError(
                f"data source {body.data_source_id} not found"
            )
        if not source.is_active:
            raise PricingValidationError("FX data source is inactive")
        base = body.base_currency.upper()
        quote = body.quote_currency.upper()
        existing = session.scalar(
            select(FXRate).where(
                FXRate.base_currency == base,
                FXRate.quote_currency == quote,
                FXRate.rate == body.rate,
                FXRate.rate_date == body.rate_date,
                FXRate.data_source_id == source.id,
                FXRate.verification_status == "verified",
            )
        )
        value = PricingService(session).create_fx_rate(
            base_currency=base,
            quote_currency=quote,
            rate=body.rate,
            rate_date=body.rate_date,
            data_source_id=source.id,
            verification_status="verified",
            observed_at=body.observed_at,
        )
        reused = existing is not None
        if not reused:
            session.add(
                AuditEvent(
                    actor=body.actor,
                    action="verify_fx_rate",
                    entity_type="fx_rate",
                    entity_id=str(value.id),
                    after_data={
                        "data_source_id": source.id,
                        "base_currency": value.base_currency,
                        "quote_currency": value.quote_currency,
                        "rate_date": value.rate_date.isoformat(),
                        "verification_status": value.verification_status,
                    },
                )
            )
            session.flush()
        return _fx_rate_response(value, reused=reused)

    @api.post(
        "/price-normalizations", response_model=PriceNormalizationResponse
    )
    def create_price_normalization(
        body: PriceNormalizationCreateRequest,
        session: Session = Depends(get_session),
    ) -> PriceNormalizationResponse:
        component = session.get(PriceComponent, body.price_component_id)
        if component is None:
            raise PricingNotFoundError(
                f"price component {body.price_component_id} not found"
            )
        snapshot_source_id = component.snapshot.source_artifact_id
        if snapshot_source_id != body.source_artifact_id:
            raise PricingValidationError(
                "source artifact does not belong to the price component"
            )
        statement = select(PriceNormalization).where(
            PriceNormalization.price_component_id == component.id,
            PriceNormalization.algorithm_version == body.algorithm_version,
        )
        statement = statement.where(
            PriceNormalization.fx_rate_id.is_(None)
            if body.fx_rate_id is None
            else PriceNormalization.fx_rate_id == body.fx_rate_id
        )
        existing = session.scalar(statement)
        value = PricingService(session).store_normalization(
            component,
            fx_rate_id=body.fx_rate_id,
            algorithm_version=body.algorithm_version,
        )
        if value.verification_status != "verified":
            raise PricingValidationError(
                "normalization requires a verified published price"
            )
        reused = existing is not None
        if not reused:
            session.add(
                AuditEvent(
                    actor=body.actor,
                    action="persist_price_normalization",
                    entity_type="price_normalization",
                    entity_id=str(value.id),
                    after_data={
                        "price_component_id": component.id,
                        "source_artifact_id": body.source_artifact_id,
                        "fx_rate_id": value.fx_rate_id,
                        "algorithm_version": value.algorithm_version,
                        "verification_status": value.verification_status,
                    },
                )
            )
            session.flush()
        return _normalization_response(value, reused=reused)

    @api.get("/prices/history", response_model=list[PriceSnapshotResponse])
    def price_history(
        pricing_plan_id: int = Query(gt=0),
        session: Session = Depends(get_session),
    ) -> list[PriceSnapshotResponse]:
        if session.get(PricingPlan, pricing_plan_id) is None:
            raise PricingNotFoundError(f"pricing plan {pricing_plan_id} not found")
        return [
            _snapshot_response(value)
            for value in PricingService(session).history(pricing_plan_id)
        ]

    @api.post("/prices/compare", response_model=PriceCompareResponse)
    def compare_prices(
        body: PriceCompareRequest,
        session: Session = Depends(get_session),
    ) -> PriceCompareResponse:
        result = PricingService(session).compare(
            body.left_component_id,
            body.right_component_id,
            left_normalization_id=body.left_normalization_id,
            right_normalization_id=body.right_normalization_id,
            algorithm_version=body.algorithm_version,
        )
        return PriceCompareResponse(
            comparable=result.comparable,
            left_amount=result.left_amount,
            right_amount=result.right_amount,
            delta=result.delta,
            ratio=result.ratio,
            cheaper=result.cheaper,
            reasons=result.reasons,
            differences=[
                ComparisonDifferenceResponse(
                    field=item.field, left=item.left, right=item.right
                )
                for item in result.differences
            ],
        )

    @api.get("/prices/export.csv")
    def export_prices(
        pricing_plan_id: int | None = Query(default=None, gt=0),
        session: Session = Depends(get_session),
    ) -> StreamingResponse:
        statement = select(PriceSnapshot).options(
            selectinload(PriceSnapshot.components).joinedload(
                PriceComponent.billing_metric
            )
        )
        if pricing_plan_id is not None:
            if session.get(PricingPlan, pricing_plan_id) is None:
                raise PricingNotFoundError(
                    f"pricing plan {pricing_plan_id} not found"
                )
            statement = statement.where(
                PriceSnapshot.pricing_plan_id == pricing_plan_id
            )
        snapshots = list(
            session.scalars(
                statement.order_by(
                    PriceSnapshot.pricing_plan_id,
                    PriceSnapshot.effective_from,
                    PriceSnapshot.id,
                )
            )
        )
        return StreamingResponse(
            PricingService.stream_csv(snapshots),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="token-prices.csv"'
            },
        )

    return api


app = create_app()
