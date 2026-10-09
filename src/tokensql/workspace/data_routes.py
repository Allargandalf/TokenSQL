"""FastAPI routes for the independent quote workspace."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import ValidationError

from tokensql.workspace.schemas import (
    BulkQuoteRequest,
    BulkWriteResponse,
    FilterOptions,
    QuoteListResponse,
    QuotePatch,
    QuoteResponse,
)
from tokensql.workspace.store import (
    IdempotencyConflictError,
    QuoteNotFoundError,
    RevisionConflictError,
    WorkspaceStore,
)

EXPORT_COLUMNS = (
    "id", "model_name", "model_version", "manufacturer", "provider",
    "provider_type", "amount", "currency", "unit", "unit_quantity", "metric",
    "dimension", "direction", "component_type", "discount_rate", "conditions",
    "effective_date", "source", "source_ref", "note", "status", "revision",
    "created_at", "updated_at",
)


def _detail(error: Exception) -> str:
    return " ".join(str(error).split())[:500] or "request could not be processed"


def _ids(value: str | None) -> list[int] | None:
    if value is None:
        return None
    try:
        result = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers")
    if not result or any(item <= 0 for item in result):
        raise HTTPException(status_code=422, detail="ids must contain positive integers")
    return list(dict.fromkeys(result))


def _export_values(item: QuoteResponse) -> list[object]:
    value = item.model_dump(mode="json")
    value["conditions"] = json.dumps(
        value["conditions"], ensure_ascii=False, sort_keys=True
    )
    return ["" if value.get(column) is None else value.get(column) for column in EXPORT_COLUMNS]


def _csv_response(items: list[QuoteResponse]) -> Response:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS)
    for item in items:
        writer.writerow(_export_values(item))
    data = output.getvalue().encode("utf-8-sig")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tokensql-quotes.csv"'},
    )


def _xlsx_response(items: list[QuoteResponse]) -> StreamingResponse:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Quotes")
    sheet.append(EXPORT_COLUMNS)
    for item in items:
        sheet.append(_export_values(item))
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": 'attachment; filename="tokensql-quotes.xlsx"'},
    )


def create_data_router(store: WorkspaceStore) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["workspace-data"])

    @router.get("/quotes", response_model=QuoteListResponse)
    def list_quotes(
        q: str | None = None,
        model_name: str | None = None,
        manufacturer: str | None = None,
        provider: str | None = None,
        currency: str | None = None,
        metric: str | None = None,
        dimension: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> QuoteListResponse:
        items, total = store.list_quotes(
            q=q, model_name=model_name, manufacturer=manufacturer,
            provider=provider, currency=currency, metric=metric,
            dimension=dimension, limit=limit, offset=offset,
        )
        return QuoteListResponse(
            items=items, total=total,
            filter_options=FilterOptions.model_validate(store.filter_options()),
        )

    @router.get("/quotes/{quote_id}", response_model=QuoteResponse)
    def get_quote(quote_id: int) -> QuoteResponse:
        try:
            return store.get_quote(quote_id)
        except QuoteNotFoundError as error:
            raise HTTPException(status_code=404, detail=_detail(error)) from None

    @router.post("/quotes", response_model=BulkWriteResponse, status_code=201)
    def create_quotes(request: BulkQuoteRequest) -> BulkWriteResponse:
        try:
            items, reused = store.create_many(
                request.items, actor=request.actor,
                idempotency_key=request.idempotency_key,
            )
        except IdempotencyConflictError as error:
            raise HTTPException(status_code=409, detail=_detail(error)) from None
        return BulkWriteResponse(items=items, created=0 if reused else len(items), reused=reused)



    @router.patch("/quotes/{quote_id}", response_model=QuoteResponse)
    def update_quote(quote_id: int, request: QuotePatch) -> QuoteResponse:
        changes = request.model_dump(
            mode="python", exclude_unset=True,
            exclude={"expected_revision", "actor"},
        )
        try:
            return store.update_quote(
                quote_id, changes, expected_revision=request.expected_revision,
                actor=request.actor,
            )
        except QuoteNotFoundError as error:
            raise HTTPException(status_code=404, detail=_detail(error)) from None
        except RevisionConflictError as error:
            raise HTTPException(status_code=409, detail=_detail(error)) from None
        except (ValueError, ValidationError) as error:
            raise HTTPException(status_code=422, detail=_detail(error)) from None

    @router.delete("/quotes/{quote_id}", response_model=QuoteResponse)
    def archive_quote(
        quote_id: int,
        expected_revision: int = Query(ge=1),
        actor: str = "workspace-ui",
        archive: bool = True,
    ) -> QuoteResponse:
        if not archive:
            raise HTTPException(status_code=422, detail="only archival deletion is supported")
        try:
            return store.archive_quote(
                quote_id, expected_revision=expected_revision, actor=actor
            )
        except QuoteNotFoundError as error:
            raise HTTPException(status_code=404, detail=_detail(error)) from None
        except RevisionConflictError as error:
            raise HTTPException(status_code=409, detail=_detail(error)) from None

    @router.get("/exports")
    def export_quotes(
        format: Literal["csv", "xlsx"] = "csv",
        ids: str | None = None,
        q: str | None = None,
        model_name: str | None = None,
        manufacturer: str | None = None,
        provider: str | None = None,
        currency: str | None = None,
        metric: str | None = None,
        dimension: str | None = None,
    ) -> Response:
        parsed_ids = _ids(ids)
        items, _ = store.list_quotes(
            q=q, model_name=model_name, manufacturer=manufacturer,
            provider=provider, currency=currency, metric=metric,
            dimension=dimension, ids=parsed_ids, limit=100_000, offset=0,
        )
        return _csv_response(items) if format == "csv" else _xlsx_response(items)

    return router
