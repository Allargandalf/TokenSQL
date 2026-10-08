"""Strict Pydantic contracts for Qwen table understanding and row extraction.

All monetary and quantity values are decimal strings; the service layer
converts them to Decimal. Every model forbids unknown fields and is frozen so
validated payloads cannot be mutated downstream.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ColumnRoleLiteral = Literal[
    "model_name",
    "model_version",
    "manufacturer",
    "provider",
    "input_price",
    "output_price",
    "cached_input_price",
    "discount",
    "condition",
    "effective_date",
    "notes",
    "unknown",
]

ProviderTypeLiteral = Literal["official", "reseller", "aggregator", "internal"]

ComponentTypeLiteral = Literal["monetary", "discount"]
DimensionLiteral = Literal[
    "token", "request", "image", "audio", "time", "storage", "other"
]
DirectionLiteral = Literal["input", "output", "cached_input", "other"]


class ColumnRoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column_letter: str
    role: ColumnRoleLiteral
    header_text: str | None = None
    unit: str | None = None
    currency: str | None = None
    metric_code: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    notes: str | None = None


class SheetUnderstandingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet: str
    manufacturer: str | None = None
    provider: str | None = None
    provider_type: ProviderTypeLiteral | None = None
    currency: str | None = None
    unit_basis: str | None = None
    discount_basis: str | None = None
    effective_from: str | None = None
    column_roles: list[ColumnRoleModel] = Field(default_factory=list)
    notes: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class TableUnderstandingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    sheets: list[SheetUnderstandingModel]
    notes: str | None = None


class ComponentExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_type: ComponentTypeLiteral
    metric_code: str | None = None
    dimension: DimensionLiteral | None = None
    direction: DirectionLiteral | None = None
    raw_amount: str | None = None
    currency: str | None = None
    unit_name: str | None = None
    unit_quantity: str | None = None
    discount_rate: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    tier_start: str | None = None
    tier_end: str | None = None
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_component(self) -> "ComponentExtractionModel":
        if self.component_type == "monetary":
            if not (self.raw_amount or "").strip():
                raise ValueError(
                    "monetary component requires a non-empty raw_amount"
                )
            if not (self.unit_quantity or "").strip():
                raise ValueError(
                    "monetary component requires a non-empty unit_quantity"
                )
        else:
            if not (self.discount_rate or "").strip():
                raise ValueError(
                    "discount component requires a non-empty discount_rate"
                )
            for forbidden in ("raw_amount", "currency", "unit_name", "unit_quantity"):
                if getattr(self, forbidden) is not None:
                    raise ValueError(
                        f"discount component must not set {forbidden}"
                    )
        return self


class RowExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(gt=0)
    model_name: str | None = None
    model_version: str | None = None
    aliases: list[str] = Field(default_factory=list)
    manufacturer: str | None = None
    provider: str | None = None
    plan_name: str | None = None
    components: list[ComponentExtractionModel] = Field(default_factory=list)
    effective_from: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    catalog_suggestions: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class RowExtractionBatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    rows: list[RowExtractionModel]
