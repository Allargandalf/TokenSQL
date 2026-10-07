"""Strict request and response contracts for the TokenSQL HTTP API."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UTCModel(APIModel):
    @field_validator("*", mode="before")
    @classmethod
    def require_aware_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("datetime must include a timezone")
            return value.astimezone(timezone.utc)
        return value

    @field_serializer("*", when_used="json")
    def serialize_values(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Decimal):
            return str(value)
        return value


class ParserProfileRequest(APIModel):
    header_row: int = Field(default=1, ge=1)
    discount_scale: Decimal | None = None
    price_headers: tuple[str, ...] | None = None


class ProfileContextRequest(APIModel):
    profile: ParserProfileRequest = Field(default_factory=ParserProfileRequest)


class ImportRequest(APIModel):
    data_source_id: int = Field(gt=0)
    parser_version: str = Field(min_length=1, max_length=80)
    profile: ParserProfileRequest = Field(default_factory=ParserProfileRequest)
    sheet_names: tuple[str, ...] | None = None
    provider_id: int | None = Field(default=None, gt=0)
    vendor_context: dict[str, Any] = Field(default_factory=dict)
    initiated_by: str | None = Field(default=None, max_length=255)


class ReviewApproveRequest(APIModel):
    reviewer: str = Field(min_length=1, max_length=255)
    resolution: dict[str, Any] = Field(default_factory=dict)
    create_alias_rule: bool | None = None


class ReviewRejectRequest(APIModel):
    reviewer: str = Field(min_length=1, max_length=255)
    resolution: dict[str, Any] | None = None


class AliasCreateRequest(APIModel):
    alias: str = Field(min_length=1, max_length=255)
    standard_model_id: int = Field(gt=0)
    provider_id: int | None = Field(default=None, gt=0)
    model_version_id: int | None = Field(default=None, gt=0)
    alias_type: str = Field(default="imported", max_length=32)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    source_note: str | None = None


class PriceCompareRequest(APIModel):
    left_component_id: int = Field(gt=0)
    right_component_id: int = Field(gt=0)
    left_normalization_id: int | None = Field(default=None, gt=0)
    right_normalization_id: int | None = Field(default=None, gt=0)
    algorithm_version: str | None = Field(default=None, min_length=1, max_length=80)


class VerifiedActionRequest(APIModel):
    """Common trust boundary for state-changing verified operations."""

    actor: str = Field(min_length=1, max_length=255)
    verified: Literal[True]

    @field_validator("actor")
    @classmethod
    def actor_must_not_be_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("actor cannot be blank")
        return cleaned


class PublishStagedQuoteRequest(VerifiedActionRequest):
    staged_quote_id: int = Field(gt=0)
    pricing_plan_id: int | None = Field(default=None, gt=0)
    source_artifact_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=255)


    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("idempotency_key cannot be blank")
        return cleaned


class PublishOfficialQuoteRequest(PublishStagedQuoteRequest):
    data_source_id: int = Field(gt=0)


class FXRateCreateRequest(VerifiedActionRequest, UTCModel):
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(min_length=3, max_length=3)
    rate: Decimal = Field(gt=0)
    rate_date: date
    data_source_id: int = Field(gt=0)
    observed_at: datetime | None = None


class PriceNormalizationCreateRequest(VerifiedActionRequest):
    price_component_id: int = Field(gt=0)
    source_artifact_id: int = Field(gt=0)
    fx_rate_id: int | None = Field(default=None, gt=0)
    algorithm_version: Literal["tokensql-normalization-v1"] = (
        "tokensql-normalization-v1"
    )


class ConfirmedComponentRequest(APIModel):
    candidate_index: int = Field(ge=0)
    billing_metric_id: int = Field(gt=0)
    component_type: Literal["monetary", "discount"]
    raw_amount: Decimal | None = Field(default=None, ge=0)
    raw_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    raw_unit_name: str | None = Field(default=None, min_length=1, max_length=64)
    raw_unit_quantity: Decimal | None = Field(default=None, gt=0)
    discount_rate: Decimal | None = Field(default=None, ge=0, le=1)
    tier_start: Decimal = Field(default=Decimal("0"), ge=0)
    tier_end: Decimal | None = Field(default=None, gt=0)
    conditions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_component_shape(self) -> "ConfirmedComponentRequest":
        monetary = (
            self.raw_amount,
            self.raw_currency_code,
            self.raw_unit_name,
            self.raw_unit_quantity,
        )
        if self.tier_end is not None and self.tier_end <= self.tier_start:
            raise ValueError("tier_end must be greater than tier_start")
        if self.component_type == "monetary":
            if any(value is None for value in monetary) or self.discount_rate is not None:
                raise ValueError(
                    "monetary component requires amount/currency/unit and no discount"
                )
        elif any(value is not None for value in monetary) or self.discount_rate is None:
            raise ValueError(
                "discount component requires discount_rate and no monetary fields"
            )
        return self


class ConfirmStagedQuoteRequest(UTCModel):
    actor: str = Field(min_length=1, max_length=255)
    source_artifact_id: int = Field(gt=0)
    provider_id: int = Field(gt=0)
    standard_model_id: int = Field(gt=0)
    model_version_id: int | None = Field(default=None, gt=0)
    offering_id: int = Field(gt=0)
    pricing_plan_id: int = Field(gt=0)
    currency_code: str = Field(min_length=3, max_length=3)
    observed_at: datetime
    effective_from: datetime
    effective_to: datetime | None = None
    components: tuple[ConfirmedComponentRequest, ...] = Field(min_length=1)
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("actor", "note")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

class QwenHealthResponse(APIModel):
    enabled: bool
    configured: bool


class HealthResponse(APIModel):
    status: str
    database: str
    qwen: QwenHealthResponse


class ModelResponse(APIModel):
    id: int
    manufacturer_id: int
    canonical_name: str
    display_name: str
    family: str | None
    modality: str
    is_active: bool
    attributes: dict[str, Any]


class ProviderResponse(APIModel):
    id: int
    name: str
    slug: str
    provider_type: str
    website_url: str | None
    is_active: bool
    attributes: dict[str, Any]


class PricingPlanResponse(UTCModel):
    id: int
    offering_id: int
    name: str
    billing_mode: str
    currency_code: str
    effective_from: datetime
    effective_to: datetime | None
    minimum_commitment_amount: Decimal | None
    terms: dict[str, Any]


class HeaderProfileResponse(APIModel):
    column_index: int
    column_letter: str
    coordinate: str
    raw_value: Any
    normalized_value: str
    needs_review: bool
    review_reasons: tuple[str, ...]


class SheetProfileResponse(APIModel):
    title: str
    sheet_index: int
    max_row: int
    max_column: int
    header_row: int
    data_row_count: int
    merge_ranges: tuple[str, ...]
    headers: list[HeaderProfileResponse]
    needs_review: bool
    review_reasons: tuple[str, ...]


class StagedProfileResponse(APIModel):
    sheet_name: str
    source_row: int
    source_coordinate: str
    price_kind: str
    amount: Decimal | None
    currency: str | None
    unit: str | None
    discount_multiplier: Decimal | None
    condition_text: str | None
    needs_review: bool
    review_reasons: tuple[str, ...]


class WorkbookProfileResponse(APIModel):
    source_name: str
    sha256: str
    byte_size: int
    sheets: list[SheetProfileResponse]
    needs_review: bool
    review_reasons: tuple[str, ...]
    staged_quotes: list[StagedProfileResponse]


class ImportSummaryResponse(APIModel):
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
    reused_existing: bool


class ReviewResponse(UTCModel):
    id: int
    staged_quote_id: int
    review_type: str
    status: str
    severity: str
    reason: str
    proposed_resolution: dict[str, Any] | None
    resolution: dict[str, Any] | None
    resolved_by: str | None
    resolved_at: datetime | None


class StagedSourceResponse(APIModel):
    data_source_id: int
    source_artifact_id: int
    original_filename: str | None
    sha256: str


class StagedQuoteResponse(UTCModel):
    id: int
    import_batch_id: int
    row_number: int
    sheet_name: str
    provider_id: int | None
    matched_standard_model_id: int | None
    matched_model_version_id: int | None
    matched_offering_id: int | None
    extracted_model_name: str | None
    extracted_plan_name: str | None
    match_method: str | None
    review_status: str
    validation_errors: list[Any]
    extracted_quote: dict[str, Any]
    source: StagedSourceResponse
    reviews: list[ReviewResponse]
    created_at: datetime
    updated_at: datetime


class AliasResponse(APIModel):
    id: int
    alias: str
    normalized_alias: str
    alias_type: str
    provider_id: int | None
    standard_model_id: int
    model_version_id: int | None
    confidence: Decimal | None
    is_verified: bool
    source_note: str | None


class PriceComponentResponse(APIModel):
    id: int
    billing_metric_id: int
    metric_code: str
    component_type: str
    tier_start: Decimal
    tier_end: Decimal | None
    raw_amount: Decimal | None
    raw_currency_code: str | None
    raw_unit_name: str | None
    raw_unit_quantity: Decimal | None
    discount_rate: Decimal | None
    conditions: dict[str, Any]


class PriceSnapshotResponse(UTCModel):
    id: int
    pricing_plan_id: int
    data_source_id: int
    source_artifact_id: int | None
    supersedes_snapshot_id: int | None
    observed_at: datetime
    effective_from: datetime
    effective_to: datetime | None
    currency_code: str
    verification_status: str
    components: list[PriceComponentResponse]


class PublicationResponse(APIModel):
    snapshot: PriceSnapshotResponse
    reused_existing: bool


class FXRateResponse(UTCModel):
    id: int
    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_date: date
    data_source_id: int
    observed_at: datetime
    verification_status: str
    reused_existing: bool


class PriceNormalizationResponse(UTCModel):
    id: int
    price_component_id: int
    fx_rate_id: int | None
    algorithm_version: str
    comparison_key: str
    verification_status: str
    normalized_amount: Decimal
    normalized_currency_code: str
    normalized_unit_name: str
    normalized_unit_quantity: int
    formula: str
    created_at: datetime
    reused_existing: bool


class ComparisonDifferenceResponse(APIModel):
    field: str
    left: Any
    right: Any


class PriceCompareResponse(APIModel):
    comparable: bool
    left_amount: Decimal | None
    right_amount: Decimal | None
    delta: Decimal | None
    ratio: Decimal | None
    cheaper: str | None
    reasons: tuple[str, ...]
    differences: list[ComparisonDifferenceResponse]
