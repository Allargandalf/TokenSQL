"""Validated public contracts for the lightweight quote workspace."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ProviderType = Literal["official", "reseller", "aggregator", "internal", "unknown"]
ComponentType = Literal["monetary", "discount"]
QuoteStatus = Literal["active", "archived"]


def _required_text(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


class QuoteFields(BaseModel):
    """Editable flat quote fields accepted from the UI or an agent."""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    model_version: str | None = None
    manufacturer: str | None = None
    provider: str = "未指定"
    provider_type: ProviderType = "unknown"
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    unit_quantity: Decimal = Decimal("1")
    metric: str | None = None
    dimension: str | None = None
    direction: str | None = None
    component_type: ComponentType = "monetary"
    discount_rate: Decimal | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    effective_date: date | None = None
    source: str | None = None
    source_ref: str | None = None
    note: str | None = None

    @field_validator("model_name", "provider")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator(
        "model_version",
        "manufacturer",
        "unit",
        "metric",
        "dimension",
        "direction",
        "source",
        "source_ref",
        "note",
        mode="before",
    )
    @classmethod
    def blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        return normalized

    @field_validator("unit_quantity")
    @classmethod
    def validate_unit_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("unit_quantity must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_component(self) -> "QuoteFields":
        if self.component_type == "monetary":
            if self.amount is None or not self.amount.is_finite() or self.amount < 0:
                raise ValueError("monetary quotes require a non-negative amount")
            if self.currency is None:
                raise ValueError("monetary quotes require currency")
            if self.unit is None:
                raise ValueError("monetary quotes require unit")
            if self.discount_rate is not None:
                raise ValueError("monetary quotes cannot include discount_rate")
        else:
            if self.discount_rate is None or not self.discount_rate.is_finite():
                raise ValueError("discount quotes require discount_rate")
            if self.discount_rate < 0 or self.discount_rate > 1:
                raise ValueError("discount_rate must be between zero and one")
            if self.amount is not None:
                raise ValueError("discount quotes cannot include amount")
        return self


class QuoteCreate(QuoteFields):
    pass


class QuotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    actor: str = "workspace-ui"
    model_name: str | None = None
    model_version: str | None = None
    manufacturer: str | None = None
    provider: str | None = None
    provider_type: ProviderType | None = None
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    unit_quantity: Decimal | None = None
    metric: str | None = None
    dimension: str | None = None
    direction: str | None = None
    component_type: ComponentType | None = None
    discount_rate: Decimal | None = None
    conditions: dict[str, Any] | None = None
    effective_date: date | None = None
    source: str | None = None
    source_ref: str | None = None
    note: str | None = None


class BulkQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[QuoteCreate] = Field(min_length=1, max_length=10_000)
    actor: str = "workspace-ui"
    idempotency_key: str | None = Field(default=None, max_length=255)


class QuoteResponse(QuoteFields):
    id: int
    status: QuoteStatus
    revision: int
    created_at: datetime
    updated_at: datetime


class FilterOptions(BaseModel):
    model_names: list[str]
    manufacturers: list[str]
    providers: list[str]
    currencies: list[str]
    metrics: list[str]
    dimensions: list[str]

class QuoteListResponse(BaseModel):
    items: list[QuoteResponse]
    total: int
    filter_options: FilterOptions

class BulkWriteResponse(BaseModel):
    items: list[QuoteResponse]
    created: int
    reused: bool = False
