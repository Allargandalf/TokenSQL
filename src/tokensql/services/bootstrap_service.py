"""Idempotent, strict catalog bootstrap for newly migrated databases."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tokensql.db.models import (
    BillingMetric,
    DataSource,
    Manufacturer,
    ModelVersion,
    Offering,
    PricingPlan,
    Provider,
    StandardModel,
)


class BootstrapValidationError(ValueError):
    """The document is invalid or conflicts with stored catalog data."""


REF = r"^[a-z0-9][a-z0-9_-]{0,127}$"


class StrictBootstrapModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataSourceSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    name: str = Field(min_length=1, max_length=255)
    source_type: Literal["official", "provider_excel", "provider_api", "web", "manual"]
    authority_level: Literal["primary", "secondary", "internal"]
    base_url: str | None = Field(default=None, max_length=2048)
    is_active: bool = True
    attributes: dict[str, Any] = Field(default_factory=dict)


class ManufacturerSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    website_url: str | None = Field(default=None, max_length=1024)


class ModelSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    manufacturer_slug: str = Field(pattern=REF)
    canonical_name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    family: str | None = Field(default=None, max_length=160)
    modality: str = Field(default="text", min_length=1, max_length=32)
    is_active: bool = True
    attributes: dict[str, Any] = Field(default_factory=dict)


class ModelVersionSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    model_slug: str = Field(pattern=REF)
    version_name: str = Field(min_length=1, max_length=255)
    release_date: date | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    deprecated_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("deprecated_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("deprecated_at must include a timezone")
        return None if value is None else value.astimezone(timezone.utc)


class ProviderSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    provider_type: Literal["official", "reseller", "aggregator", "internal"]
    website_url: str | None = Field(default=None, max_length=1024)
    is_active: bool = True
    attributes: dict[str, Any] = Field(default_factory=dict)


class OfferingSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    provider_slug: str = Field(pattern=REF)
    model_slug: str = Field(pattern=REF)
    version_slug: str | None = Field(default=None, pattern=REF)
    provider_model_name: str = Field(min_length=1, max_length=255)
    api_model_id: str | None = Field(default=None, max_length=255)
    region: str = Field(default="global", min_length=1, max_length=64)
    is_active: bool = True
    attributes: dict[str, Any] = Field(default_factory=dict)


class PricingPlanSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    offering_slug: str = Field(pattern=REF)
    name: str = Field(min_length=1, max_length=255)
    billing_mode: Literal["pay_as_you_go", "subscription", "committed", "negotiated"]
    currency_code: str = Field(pattern=r"^[A-Za-z]{3}$")
    effective_from: datetime
    effective_to: datetime | None = None
    minimum_commitment_amount: Decimal | None = Field(default=None, ge=0)
    terms: dict[str, Any] = Field(default_factory=dict)

    @field_validator("effective_from", "effective_to")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("pricing plan datetimes must include a timezone")
        return None if value is None else value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def range_is_valid(self) -> "PricingPlanSpec":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class BillingMetricSpec(StrictBootstrapModel):
    slug: str = Field(pattern=REF)
    code: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    dimension: Literal["token", "request", "image", "audio", "time", "storage", "other"]
    direction: str | None = Field(default=None, max_length=32)
    unit_name: str = Field(min_length=1, max_length=64)
    unit_quantity: int = Field(gt=0)
    comparable_group: str = Field(min_length=1, max_length=120)
    attributes: dict[str, Any] = Field(default_factory=dict)


class BootstrapDocument(StrictBootstrapModel):
    schema_version: Literal[1]
    data_sources: tuple[DataSourceSpec, ...] = ()
    manufacturers: tuple[ManufacturerSpec, ...] = ()
    models: tuple[ModelSpec, ...] = ()
    model_versions: tuple[ModelVersionSpec, ...] = ()
    providers: tuple[ProviderSpec, ...] = ()
    offerings: tuple[OfferingSpec, ...] = ()
    pricing_plans: tuple[PricingPlanSpec, ...] = ()
    billing_metrics: tuple[BillingMetricSpec, ...] = ()

    @model_validator(mode="after")
    def unique_slugs(self) -> "BootstrapDocument":
        for group in GROUPS:
            slugs = [item.slug for item in getattr(self, group)]
            if len(slugs) != len(set(slugs)):
                raise ValueError(f"{group} contains duplicate slugs")
        return self


GROUPS = (
    "data_sources",
    "manufacturers",
    "models",
    "model_versions",
    "providers",
    "offerings",
    "pricing_plans",
    "billing_metrics",
)


@dataclass(frozen=True)
class BootstrapResult:
    created: dict[str, int]
    reused: dict[str, int]
    ids: dict[str, dict[str, int]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_bootstrap_document(path: str | Path) -> BootstrapDocument:
    candidate = Path(path).expanduser().resolve(strict=True)
    if candidate.suffix.casefold() != ".json":
        raise BootstrapValidationError("bootstrap file must use the .json extension")
    if candidate.stat().st_size > 1_048_576:
        raise BootstrapValidationError("bootstrap file exceeds the 1 MiB limit")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        return BootstrapDocument.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BootstrapValidationError(f"invalid bootstrap document: {exc}") from exc


def _clean(value: str) -> str:
    return " ".join(value.split())


def _same(value: Any, expected: dict[str, Any]) -> bool:
    return all(getattr(value, key) == item for key, item in expected.items())


class BootstrapService:
    """Create a catalog atomically while resolving only document-local slugs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def apply_file(self, path: str | Path) -> BootstrapResult:
        return self.apply(load_bootstrap_document(path))

    def apply(self, document: BootstrapDocument | dict[str, Any]) -> BootstrapResult:
        try:
            parsed = document if isinstance(document, BootstrapDocument) else BootstrapDocument.model_validate(document)
        except ValueError as exc:
            raise BootstrapValidationError(f"invalid bootstrap document: {exc}") from exc
        created = {group: 0 for group in GROUPS}
        reused = {group: 0 for group in GROUPS}
        ids: dict[str, dict[str, int]] = {group: {} for group in GROUPS}
        manager = self.session.begin() if not self.session.in_transaction() else nullcontext()
        try:
            with manager:
                self._apply(parsed, created, reused, ids)
                self.session.flush()
        except BootstrapValidationError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raise BootstrapValidationError(
                "bootstrap conflicts with an existing catalog identity"
            ) from exc
        return BootstrapResult(created, reused, ids)

    def _record(
        self,
        group: str,
        slug: str,
        value: Any,
        made: bool,
        created: dict[str, int],
        reused: dict[str, int],
        ids: dict[str, dict[str, int]],
    ) -> None:
        self.session.flush()
        ids[group][slug] = value.id
        (created if made else reused)[group] += 1

    @staticmethod
    def _ref(ids: dict[str, dict[str, int]], group: str, slug: str) -> int:
        try:
            return ids[group][slug]
        except KeyError as exc:
            raise BootstrapValidationError(
                f"unknown {group.rstrip('s').replace('_', ' ')} reference: {slug!r}"
            ) from exc

    def _apply(self, doc, created, reused, ids) -> None:
        for spec in doc.data_sources:
            state = dict(
                source_type=spec.source_type,
                authority_level=spec.authority_level,
                base_url=spec.base_url,
                is_active=spec.is_active,
                attributes=spec.attributes,
            )
            value = self.session.scalar(select(DataSource).where(DataSource.name == _clean(spec.name)))
            made = value is None
            if made:
                value = DataSource(name=_clean(spec.name), **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"data source {spec.slug!r} conflicts with existing record")
            self._record("data_sources", spec.slug, value, made, created, reused, ids)

        for spec in doc.manufacturers:
            state = dict(name=_clean(spec.name), website_url=spec.website_url)
            value = self.session.scalar(select(Manufacturer).where(
                or_(Manufacturer.slug == spec.slug, Manufacturer.name == state["name"])
            ))
            made = value is None
            if made:
                value = Manufacturer(slug=spec.slug, **state)
                self.session.add(value)
            elif value.slug != spec.slug or not _same(value, state):
                raise BootstrapValidationError(f"manufacturer {spec.slug!r} conflicts with existing record")
            self._record("manufacturers", spec.slug, value, made, created, reused, ids)

        for spec in doc.models:
            manufacturer_id = self._ref(ids, "manufacturers", spec.manufacturer_slug)
            state = dict(
                display_name=_clean(spec.display_name),
                family=spec.family,
                modality=spec.modality,
                is_active=spec.is_active,
                attributes=spec.attributes,
            )
            canonical_name = _clean(spec.canonical_name)
            value = self.session.scalar(select(StandardModel).where(
                StandardModel.manufacturer_id == manufacturer_id,
                StandardModel.canonical_name == canonical_name,
            ))
            made = value is None
            if made:
                value = StandardModel(manufacturer_id=manufacturer_id, canonical_name=canonical_name, **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"model {spec.slug!r} conflicts with existing record")
            self._record("models", spec.slug, value, made, created, reused, ids)

        for spec in doc.model_versions:
            model_id = self._ref(ids, "models", spec.model_slug)
            state = dict(
                release_date=spec.release_date,
                context_window_tokens=spec.context_window_tokens,
                deprecated_at=spec.deprecated_at,
                attributes=spec.attributes,
            )
            version_name = _clean(spec.version_name)
            value = self.session.scalar(select(ModelVersion).where(
                ModelVersion.standard_model_id == model_id,
                ModelVersion.version_name == version_name,
            ))
            made = value is None
            if made:
                value = ModelVersion(standard_model_id=model_id, version_name=version_name, **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"model version {spec.slug!r} conflicts with existing record")
            self._record("model_versions", spec.slug, value, made, created, reused, ids)

        for spec in doc.providers:
            state = dict(
                name=_clean(spec.name),
                provider_type=spec.provider_type,
                website_url=spec.website_url,
                is_active=spec.is_active,
                attributes=spec.attributes,
            )
            value = self.session.scalar(select(Provider).where(
                or_(Provider.slug == spec.slug, Provider.name == state["name"])
            ))
            made = value is None
            if made:
                value = Provider(slug=spec.slug, **state)
                self.session.add(value)
            elif value.slug != spec.slug or not _same(value, state):
                raise BootstrapValidationError(f"provider {spec.slug!r} conflicts with existing record")
            self._record("providers", spec.slug, value, made, created, reused, ids)

        for spec in doc.offerings:
            provider_id = self._ref(ids, "providers", spec.provider_slug)
            model_id = self._ref(ids, "models", spec.model_slug)
            version_id = None if spec.version_slug is None else self._ref(ids, "model_versions", spec.version_slug)
            if version_id is not None:
                version = self.session.get(ModelVersion, version_id)
                if version is None or version.standard_model_id != model_id:
                    raise BootstrapValidationError(
                        f"offering {spec.slug!r} references a version from another model"
                    )
            state = dict(
                standard_model_id=model_id,
                model_version_id=version_id,
                api_model_id=spec.api_model_id,
                is_active=spec.is_active,
                attributes=spec.attributes,
            )
            name, region = _clean(spec.provider_model_name), _clean(spec.region)
            value = self.session.scalar(select(Offering).where(
                Offering.provider_id == provider_id,
                Offering.provider_model_name == name,
                Offering.region == region,
            ))
            made = value is None
            if made:
                value = Offering(provider_id=provider_id, provider_model_name=name, region=region, **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"offering {spec.slug!r} conflicts with existing record")
            self._record("offerings", spec.slug, value, made, created, reused, ids)

        for spec in doc.pricing_plans:
            offering_id = self._ref(ids, "offerings", spec.offering_slug)
            state = dict(
                billing_mode=spec.billing_mode,
                currency_code=spec.currency_code.upper(),
                effective_to=spec.effective_to,
                minimum_commitment_amount=spec.minimum_commitment_amount,
                terms=spec.terms,
            )
            name = _clean(spec.name)
            value = self.session.scalar(select(PricingPlan).where(
                PricingPlan.offering_id == offering_id,
                PricingPlan.name == name,
                PricingPlan.effective_from == spec.effective_from,
            ))
            made = value is None
            if made:
                value = PricingPlan(offering_id=offering_id, name=name, effective_from=spec.effective_from, **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"pricing plan {spec.slug!r} conflicts with existing record")
            self._record("pricing_plans", spec.slug, value, made, created, reused, ids)

        for spec in doc.billing_metrics:
            state = dict(
                display_name=_clean(spec.display_name),
                dimension=spec.dimension,
                direction=spec.direction,
                unit_name=_clean(spec.unit_name),
                unit_quantity=spec.unit_quantity,
                comparable_group=_clean(spec.comparable_group),
                attributes=spec.attributes,
            )
            code = _clean(spec.code)
            value = self.session.scalar(select(BillingMetric).where(BillingMetric.code == code))
            made = value is None
            if made:
                value = BillingMetric(code=code, **state)
                self.session.add(value)
            elif not _same(value, state):
                raise BootstrapValidationError(f"billing metric {spec.slug!r} conflicts with existing record")
            self._record("billing_metrics", spec.slug, value, made, created, reused, ids)
