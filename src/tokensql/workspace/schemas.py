"""Validated normalized pricing contracts preserving original currency and units."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .discount import normalize_discount_rate
from .pricing_conditions import DiscountItem, OfficialMatch, PricingConditions
from .units import PricingUnitId, expand_price_payload

ProviderType = Literal["official", "reseller", "aggregator", "internal", "unknown"]
ComponentType = Literal["monetary", "discount"]
QuoteStatus = Literal["active", "archived"]
CurrencyType = Literal["CNY", "USD", "HKD"]


def _required_text(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("must not be blank")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("discount_rate", "raw_discount_rate", mode="before", check_fields=False)
    @classmethod
    def fixed_discount(cls, value: Any) -> Decimal | None:
        return normalize_discount_rate(value)

    @field_validator("*", mode="before")
    @classmethod
    def exact_numbers(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None and info.field_name in {"aliases", "tags", "models", "official_prices", "create", "update", "delete", "discount_items"}:
            return []
        if value is None and info.field_name in {"conditions", "pricing_conditions"}:
            return {}
        nullable_business = {
            "vendor", "model_name", "billing_item", "billing_metric", "supplier", "supplier_id", "source_scope", "source_entry_id", "source_sheet", "supplier_model_id",
            "provider_type", "amount", "discount_rate", "pricing_basis", "pricing_unit_id", "currency",
            "unit", "unit_quantity", "component_type", "conversion", "dimension", "direction",
            "effective_date", "source", "source_ref", "note", "explicit_official",
            "original_currency", "original_amount", "exchange_rate", "rate_source",
        }
        if isinstance(value, str) and not value.strip() and info.field_name in nullable_business:
            return None
        if isinstance(value, float):
            raise ValueError("use Decimal strings, not binary floating point numbers")
        return value


class ConversionEvidence(Contract):
    original_currency: str | None = None
    original_amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    exchange_rate: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    rate_source: str | None = None

    @field_validator("original_currency")
    @classmethod
    def foreign_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if len(value) != 3 or not value.isalpha() :
            raise ValueError("original_currency must be a foreign three-letter currency")
        return value

    @field_validator("rate_source")
    @classmethod
    def evidence_required(cls, value: str | None) -> str | None:
        return _required_text(value) if value is not None else None


class ModelCreate(Contract):
    model_id: str
    vendor: str | None = None
    model_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    expected_revision: int | None = Field(default=None, ge=1)
    source: str | None = None
    source_ref: str | None = None
    note: str | None = None

    @field_validator("model_id")
    @classmethod
    def required(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_required_text(v) for v in values))


class OfficialPriceCreate(Contract):
    model_id: str
    official_price_id: str | None = Field(default=None, min_length=1, max_length=128)
    billing_metric: str | None = None
    pricing_conditions: PricingConditions = Field(default_factory=PricingConditions)
    conditions: dict[str, Any] = Field(default_factory=dict)
    billing_item: str | None = None
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    pricing_unit_id: PricingUnitId | None = None
    currency: CurrencyType | None = "CNY"
    unit: str | None = None
    unit_quantity: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    explicit_official: Literal[True] | None = None
    source: str | None = None
    source_ref: str | None = None
    conversion: ConversionEvidence | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    effective_date: date | None = None
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_pricing_unit(cls, value: Any) -> Any:
        return expand_price_payload(value, default_currency="CNY") if isinstance(value, dict) else value

    @field_validator("model_id")
    @classmethod
    def required(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("unit")
    @classmethod
    def lower(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None


class QuoteFields(Contract):
    model_id: str
    billing_metric: str | None = None
    pricing_conditions: PricingConditions = Field(default_factory=PricingConditions)
    discount_scope: Literal["unknown", "model", "items"] = "unknown"
    discount_items: list[DiscountItem] = Field(default_factory=list)
    billing_item: str | None = None
    supplier: str | None = None
    supplier_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_scope: str | None = Field(default=None, min_length=1, max_length=255)
    source_entry_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_sheet: str | None = Field(default=None, min_length=1, max_length=255)
    supplier_model_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    provider_type: ProviderType | None = None
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    discount_rate: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    pricing_basis: Literal["amount", "discount_rate"] | None = None
    pricing_unit_id: PricingUnitId | None = None
    currency: CurrencyType | None = None
    unit: str | None = None
    unit_quantity: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    component_type: ComponentType | None = None
    conversion: ConversionEvidence | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    dimension: str | None = None
    direction: str | None = None
    effective_date: date | None = None
    source: str | None = None
    source_ref: str | None = None
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_pricing_unit(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, dict):
            return value
        return expand_price_payload(value, allow_legacy=bool(info.context and info.context.get("allow_legacy_unit")),
                                    default_currency="CNY" if cls.__name__ == "QuoteCreate" else None)

    @field_validator("model_id")
    @classmethod
    def required(cls, value: str | None) -> str | None:
        return _required_text(value) if value is not None else None

    @field_validator("unit")
    @classmethod
    def lower(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_required_text(v) for v in values))

    @field_validator("supplier_model_id")
    @classmethod
    def clean_channel(cls, value: str | None) -> str | None:
        return _required_text(value) if value is not None else None



class QuoteCreate(QuoteFields):
    currency: CurrencyType | None = "CNY"


class QuotePatch(Contract):
    expected_revision: int = Field(ge=1)
    billing_metric: str | None = None
    pricing_conditions: PricingConditions | None = None
    discount_scope: Literal["unknown", "model", "items"] | None = None
    discount_items: list[DiscountItem] | None = None
    actor: str = "workspace-ui"
    model_id: str | None = None
    billing_item: str | None = None
    supplier: str | None = None
    supplier_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_scope: str | None = Field(default=None, min_length=1, max_length=255)
    source_entry_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_sheet: str | None = Field(default=None, min_length=1, max_length=255)
    supplier_model_id: str | None = None
    tags: list[str] | None = None
    provider_type: ProviderType | None = None
    amount: Decimal | None = None
    discount_rate: Decimal | None = None
    pricing_basis: Literal["amount", "discount_rate"] | None = None
    pricing_unit_id: PricingUnitId | None = None
    currency: CurrencyType | None = None
    unit: str | None = None
    unit_quantity: Decimal | None = None
    component_type: ComponentType | None = None
    conversion: ConversionEvidence | None = None
    conditions: dict[str, Any] | None = None
    dimension: str | None = None
    direction: str | None = None
    effective_date: date | None = None
    source: str | None = None
    source_ref: str | None = None
    note: str | None = None


class QuoteUpdate(Contract):
    id: int = Field(gt=0)
    expected_revision: int = Field(ge=1)
    changes: dict[str, Any]

    @field_validator("changes")
    @classmethod
    def normalize_discount_change(cls, changes: dict[str, Any]) -> dict[str, Any]:
        if "discount_rate" in changes:
            changes = {**changes, "discount_rate": normalize_discount_rate(changes["discount_rate"])}
        return changes


class QuoteDelete(Contract):
    id: int = Field(gt=0)
    expected_revision: int = Field(ge=1)


class PricingBundle(Contract):
    models: list[ModelCreate] = Field(default_factory=list, max_length=10000)
    official_prices: list[OfficialPriceCreate] = Field(default_factory=list, max_length=20000)
    create: list[QuoteCreate] = Field(default_factory=list, max_length=10000)
    update: list[QuoteUpdate] = Field(default_factory=list, max_length=10000)
    delete: list[QuoteDelete] = Field(default_factory=list, max_length=10000)


class SupplierWrite(Contract):
    """Explicit supplier creation, rename or alias confirmation."""
    supplier_id: str | None = None
    name: str
    aliases: list[str] = Field(default_factory=list)
    provider_type: ProviderType | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def required_name(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def required_aliases(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_required_text(value) for value in values))


class ReconciliationDecision(Contract):
    entry_key: str
    action: Literal["update", "create", "ignore"]
    quote_id: int | None = Field(default=None, gt=0)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def update_target(self) -> "ReconciliationDecision":
        if self.action == "update" and (self.quote_id is None or self.expected_revision is None):
            raise ValueError("an explicit update mapping requires quote_id and expected_revision")
        if self.action != "update" and (self.quote_id is not None or self.expected_revision is not None):
            raise ValueError("only update mappings accept a quote target")
        return self


class SupplierReconciliationRequest(Contract):
    """A confirmed supplier/source boundary plus model-produced candidate entries."""
    supplier_id: str
    source_scope: str = Field(min_length=1, max_length=255)
    mode: Literal["incremental", "full"] = "incremental"
    scope_confirmed: bool = False
    complete_read: bool = False
    expected_sheets: list[str] = Field(default_factory=list)
    covered_sheets: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    supplier_model_ids: list[str] = Field(default_factory=list)
    source_version: str | None = None
    source_effective_date: date | None = None
    file_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    latest_confirmed: bool = False
    allow_rollback: bool = False
    unresolved_items: int = Field(default=0, ge=0)
    models: list[ModelCreate] = Field(default_factory=list, max_length=10000)
    official_prices: list[OfficialPriceCreate] = Field(default_factory=list, max_length=20000)
    entries: list[QuoteCreate] = Field(default_factory=list, max_length=10000)
    decisions: list[ReconciliationDecision] = Field(default_factory=list, max_length=10000)
    explicit_delete_ids: list[QuoteDelete] = Field(default_factory=list, max_length=10000)

    @field_validator("supplier_id", "source_scope")
    @classmethod
    def required_scope(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("expected_sheets", "covered_sheets", "model_ids", "supplier_model_ids")
    @classmethod
    def unique_scope_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_required_text(value) for value in values))

    @field_validator("file_sha256")
    @classmethod
    def lower_hash(cls, value: str) -> str:
        return value.lower()


ReconciliationRequest = SupplierReconciliationRequest


# Stable alternate name for draft producers.
DataBundle = PricingBundle


class BulkQuoteRequest(Contract):
    items: list[QuoteCreate] = Field(min_length=1, max_length=10000)
    actor: str = "workspace-ui"
    idempotency_key: str | None = Field(default=None, max_length=255)


class BulkQuoteDeleteRequest(Contract):
    items: list[QuoteDelete] = Field(min_length=1, max_length=10000)
    actor: str = "workspace-ui"

    @field_validator("items")
    @classmethod
    def unique_ids(cls, values: list[QuoteDelete]) -> list[QuoteDelete]:
        if len({value.id for value in values}) != len(values):
            raise ValueError("duplicate quote IDs are not allowed")
        return values


class BulkQuoteDeleteResponse(Contract):
    deleted: int
    deleted_ids: list[int]


class ViewExportColumn(Contract):
    key: str
    label: str

    @field_validator("key", "label")
    @classmethod
    def required(cls, value: str) -> str:
        return _required_text(value)


class ViewExportRequest(Contract):
    format: Literal["csv", "xlsx"]
    filename: str = Field(min_length=1, max_length=200)
    columns: list[ViewExportColumn] = Field(min_length=1, max_length=200)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        value = _required_text(value)
        if any(character in value for character in ("/", "\\", "\r", "\n", "\0")):
            raise ValueError("filename must be a plain file name without paths or control characters")
        return value

    @field_validator("rows")
    @classmethod
    def exact_row_numbers(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def check(value: Any) -> None:
            if isinstance(value, float):
                raise ValueError("view export rows must use Decimal strings, not binary floats")
            if isinstance(value, dict):
                for nested in value.values():
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)
        check(rows)
        return rows

    @model_validator(mode="after")
    def unique_columns(self) -> "ViewExportRequest":
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("view export column keys must be unique")
        return self


class QuoteResponse(QuoteFields):
    id: int
    supplier_id: str | None = None
    channel_id: int | None = None
    vendor: str | None = None
    model_name: str | None = None
    manufacturer: str | None = None
    provider: str | None = None
    metric: str | None = None
    model_version: str | None = None
    status: QuoteStatus
    revision: int
    created_at: datetime
    updated_at: datetime
    official_price: dict[str, Any] | None = None
    official_prices: list[dict[str, Any]] = Field(default_factory=list)
    effective_discount_rules: list[dict[str, Any]] = Field(default_factory=list)
    official_match: OfficialMatch = Field(default_factory=lambda: OfficialMatch(status="missing", reason="no_official_prices"))
    pricing_issue: str | None = None
    legacy_unit: dict[str, Any] | None = None
    raw_discount_rate: Decimal | None = None
    derived_amount: Decimal | None = None
    converted_supplier_amount: Decimal | None = None
    converted_supplier_pricing_unit_id: PricingUnitId | None = None
    conversion_status: Literal["exact", "estimated", "unavailable"] = "unavailable"
    conversion_evidence: dict[str, Any] = Field(default_factory=dict)
    discount_status: Literal["exact", "estimated", "unavailable"] = "unavailable"
    discount_evidence: dict[str, Any] = Field(default_factory=dict)
    normalized: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    display_issues: list[dict[str, Any]] = Field(default_factory=list)


class FilterOptions(Contract):
    model_names: list[str] = Field(default_factory=list)
    manufacturers: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    currencies: list[str] = Field(default_factory=list)
    pricing_unit_ids: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    vendors: list[str] = Field(default_factory=list)
    suppliers: list[str] = Field(default_factory=list)
    billing_items: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class QuoteListResponse(Contract):
    items: list[QuoteResponse]
    total: int
    filter_options: FilterOptions


class BulkWriteResponse(Contract):
    items: list[QuoteResponse]
    created: int
    reused: bool = False


class ExchangeRate(Contract):
    currency: CurrencyType
    cny_per_unit: Decimal = Field(gt=0, allow_inf_nan=False)
    source: str
    source_url: str | None = None
    observed_at: datetime
    fetched_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("source")
    @classmethod
    def required(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("observed_at", "fetched_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("rate timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def cny_identity(self) -> "ExchangeRate":
        if self.currency == "CNY" and self.cny_per_unit != 1:
            raise ValueError("CNY identity rate is always 1")
        if self.expires_at and self.expires_at < self.observed_at:
            raise ValueError("expires_at must not precede observed_at")
        return self


class TokenEquivalence(Contract):
    model_id: str
    billing_item: str
    unit: str
    tokens_per_unit: Decimal = Field(gt=0, allow_inf_nan=False)
    source: str
    source_ref: str
    observed_at: datetime
    assumptions: str | None = None

    @field_validator("model_id", "billing_item", "unit", "source", "source_ref")
    @classmethod
    def required(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("factor observed_at must include a timezone")
        return value


class PricingContextUpdate(Contract):
    expected_revision: int = Field(ge=1)
    fx_rates: list[ExchangeRate] = Field(default_factory=list)
    token_equivalences: list[TokenEquivalence] = Field(default_factory=list)
    actor: str = "workspace-ui"


class SupplierModelMapping(Contract):
    id: str | None = Field(default=None, max_length=255)
    raw_name: str = Field(min_length=1, max_length=1000)
    model_id: str = Field(min_length=1, max_length=1000)
    supplier_model_id: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw_name", "model_id", "aliases", mode="after")
    @classmethod
    def nonblank_names(cls, value: Any) -> Any:
        if isinstance(value, list):
            return list(dict.fromkeys(_required_text(item) for item in value))
        return _required_text(value)

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 16000:
            raise ValueError("profile evidence must be lightweight (at most 16000 characters)")
        return value


class SupplierBusinessRule(Contract):
    id: str | None = Field(default=None, max_length=255)
    kind: str = Field(default="interpretation", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=16000)
    enabled: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", "text")
    @classmethod
    def nonblank_rule(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("profile rule must not be blank")
        return value.strip()

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        return SupplierModelMapping.bounded_evidence(value)


class SupplierSourceProfile(Contract):
    default_prompt: str | None = Field(default=None, max_length=16000)
    model_mappings: list[SupplierModelMapping] = Field(default_factory=list, max_length=3000)
    rules: list[SupplierBusinessRule] = Field(default_factory=list, max_length=500)


class SupplierProfileContent(SupplierSourceProfile):
    sources: dict[str, SupplierSourceProfile] = Field(default_factory=dict, max_length=100)

    @field_validator("sources")
    @classmethod
    def source_names(cls, value: dict[str, SupplierSourceProfile]) -> dict[str, SupplierSourceProfile]:
        result = {}
        for scope, profile in value.items():
            scope = _required_text(scope)
            if len(scope) > 255 or scope in result:
                raise ValueError("profile source_scope must be unique bounded text")
            result[scope] = profile
        return result


class SupplierProfileWrite(Contract):
    expected_revision: int = Field(ge=0)
    default_prompt: str | None = Field(default=None, max_length=16000)
    model_mappings: list[SupplierModelMapping] = Field(default_factory=list, max_length=3000)
    rules: list[SupplierBusinessRule] = Field(default_factory=list, max_length=500)
    sources: dict[str, SupplierSourceProfile] = Field(default_factory=dict, max_length=100)
    actor: str | None = Field(default=None, max_length=255)

    @field_validator("sources")
    @classmethod
    def source_names(cls, value: dict[str, SupplierSourceProfile]) -> dict[str, SupplierSourceProfile]:
        return SupplierProfileContent.source_names(value)
