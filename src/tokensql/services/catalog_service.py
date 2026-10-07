"""Catalog creation and safe model-candidate matching services."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
import unicodedata
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tokensql.db.models import (
    Manufacturer, ModelAlias, ModelVersion, Offering, PricingPlan, Provider,
    StandardModel,
)
from tokensql.llm.schemas import MatchResult, ModelCandidate
from tokensql.matching import CandidateSelector, ModelMatcher, normalize_model_name


class CatalogConflictError(ValueError):
    """The requested catalog identity is already bound to another record."""


class CatalogNotFoundError(LookupError):
    """A referenced catalog record does not exist."""


@dataclass(frozen=True)
class CatalogMatch:
    """Validated match with server-side identity mapped after the LLM returns."""
    result: MatchResult
    standard_model: StandardModel | None = None
    model_version: ModelVersion | None = None

    @property
    def standard_model_id(self) -> int | None:
        return None if self.standard_model is None else self.standard_model.id

    @property
    def model_version_id(self) -> int | None:
        return None if self.model_version is None else self.model_version.id


def _clean(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise ValueError(f"{label} cannot be blank")
    return cleaned


def _slug(value: str) -> str:
    slug = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKC", value).casefold()).strip("-_")
    if not slug:
        raise ValueError("name cannot be converted to a non-empty slug")
    return slug


class CatalogService:
    """Transaction-neutral catalog CRUD and deterministic-first matching."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _flush(self, conflict: str) -> None:
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as exc:
            raise CatalogConflictError(conflict) from exc

    def create_manufacturer(self, name: str, *, slug: str | None = None,
                            website_url: str | None = None) -> Manufacturer:
        name, slug = _clean(name, "manufacturer name"), _slug(slug or name)
        existing = self.session.scalar(select(Manufacturer).where(or_(
            func.lower(Manufacturer.name) == name.casefold(), Manufacturer.slug == slug)))
        if existing:
            if existing.name.casefold() != name.casefold() or existing.slug != slug:
                raise CatalogConflictError("manufacturer name or slug is already in use")
            return existing
        value = Manufacturer(name=name, slug=slug, website_url=website_url)
        self.session.add(value)
        self._flush("manufacturer name or slug is already in use")
        return value

    get_or_create_manufacturer = create_manufacturer

    def get_manufacturer(self, manufacturer_id: int) -> Manufacturer:
        value = self.session.get(Manufacturer, manufacturer_id)
        if value is None:
            raise CatalogNotFoundError(f"manufacturer {manufacturer_id} not found")
        return value

    def list_manufacturers(self) -> list[Manufacturer]:
        return list(self.session.scalars(select(Manufacturer).order_by(Manufacturer.name)))

    def create_model(self, manufacturer_id: int, canonical_name: str, *,
                     display_name: str | None = None, family: str | None = None,
                     modality: str = "text",
                     attributes: dict[str, Any] | None = None) -> StandardModel:
        manufacturer = self.get_manufacturer(manufacturer_id)
        canonical = normalize_model_name(_clean(canonical_name, "canonical name"))
        existing = self.session.scalar(select(StandardModel).where(
            StandardModel.manufacturer_id == manufacturer.id,
            StandardModel.canonical_name == canonical))
        if existing:
            return existing
        value = StandardModel(
            manufacturer=manufacturer, canonical_name=canonical,
            display_name=_clean(display_name or canonical_name, "display name"),
            family=None if family is None else _clean(family, "family"),
            modality=_clean(modality, "modality").casefold(),
            attributes=dict(attributes or {}))
        self.session.add(value)
        self._flush("model already exists for this manufacturer")
        return value

    create_standard_model = create_model
    get_or_create_model = create_model

    def get_model(self, model_id: int) -> StandardModel:
        value = self.session.get(StandardModel, model_id)
        if value is None:
            raise CatalogNotFoundError(f"model {model_id} not found")
        return value

    get_standard_model = get_model

    def list_models(self, manufacturer_id: int | None = None) -> list[StandardModel]:
        statement: Select[tuple[StandardModel]] = select(StandardModel)
        if manufacturer_id is not None:
            statement = statement.where(StandardModel.manufacturer_id == manufacturer_id)
        return list(self.session.scalars(statement.order_by(
            StandardModel.manufacturer_id, StandardModel.canonical_name)))

    def create_version(self, standard_model_id: int, version_name: str, *,
                       release_date: date | None = None,
                       context_window_tokens: int | None = None,
                       attributes: dict[str, Any] | None = None) -> ModelVersion:
        model, version_name = self.get_model(standard_model_id), _clean(version_name, "version name")
        existing = self.session.scalar(select(ModelVersion).where(
            ModelVersion.standard_model_id == model.id,
            ModelVersion.version_name == version_name))
        if existing:
            return existing
        value = ModelVersion(
            standard_model=model, version_name=version_name, release_date=release_date,
            context_window_tokens=context_window_tokens, attributes=dict(attributes or {}))
        self.session.add(value)
        self._flush("version already exists for this model")
        return value

    create_model_version = create_version
    get_or_create_version = create_version

    def get_version(self, version_id: int) -> ModelVersion:
        value = self.session.get(ModelVersion, version_id)
        if value is None:
            raise CatalogNotFoundError(f"model version {version_id} not found")
        return value

    def create_provider(self, name: str, *, slug: str | None = None,
                        provider_type: str = "reseller",
                        website_url: str | None = None,
                        attributes: dict[str, Any] | None = None) -> Provider:
        name, slug = _clean(name, "provider name"), _slug(slug or name)
        provider_type = _clean(provider_type, "provider type").casefold()
        if provider_type not in {"official", "reseller", "aggregator", "internal"}:
            raise ValueError(f"unsupported provider type: {provider_type}")
        existing = self.session.scalar(select(Provider).where(or_(
            func.lower(Provider.name) == name.casefold(), Provider.slug == slug)))
        if existing:
            if (existing.name.casefold(), existing.slug, existing.provider_type) != (
                    name.casefold(), slug, provider_type):
                raise CatalogConflictError("provider name or slug is already in use")
            return existing
        value = Provider(name=name, slug=slug, provider_type=provider_type,
                         website_url=website_url, attributes=dict(attributes or {}))
        self.session.add(value)
        self._flush("provider name or slug is already in use")
        return value

    get_or_create_provider = create_provider

    def get_provider(self, provider_id: int) -> Provider:
        value = self.session.get(Provider, provider_id)
        if value is None:
            raise CatalogNotFoundError(f"provider {provider_id} not found")
        return value

    def list_providers(self) -> list[Provider]:
        return list(self.session.scalars(select(Provider).order_by(Provider.name)))

    def create_offering(self, provider_id: int, standard_model_id: int,
                        provider_model_name: str, *, model_version_id: int | None = None,
                        api_model_id: str | None = None, region: str = "global",
                        attributes: dict[str, Any] | None = None) -> Offering:
        provider, model = self.get_provider(provider_id), self.get_model(standard_model_id)
        version = None
        if model_version_id is not None:
            version = self.get_version(model_version_id)
            if version.standard_model_id != model.id:
                raise CatalogConflictError("model version belongs to a different model")
        provider_name, region = _clean(provider_model_name, "provider model name"), _clean(region, "region").casefold()
        existing = self.session.scalar(select(Offering).where(
            Offering.provider_id == provider.id,
            Offering.provider_model_name == provider_name, Offering.region == region))
        if existing:
            if existing.standard_model_id != model.id or existing.model_version_id != model_version_id:
                raise CatalogConflictError("provider offering identity is already in use")
            return existing
        value = Offering(
            provider=provider, standard_model=model, model_version=version,
            provider_model_name=provider_name,
            api_model_id=None if api_model_id is None else _clean(api_model_id, "API model id"),
            region=region, attributes=dict(attributes or {}))
        self.session.add(value)
        self._flush("provider offering already exists")
        return value

    get_or_create_offering = create_offering

    def get_offering(self, offering_id: int) -> Offering:
        value = self.session.get(Offering, offering_id)
        if value is None:
            raise CatalogNotFoundError(f"offering {offering_id} not found")
        return value

    def list_offerings(self, *, provider_id: int | None = None,
                       standard_model_id: int | None = None) -> list[Offering]:
        statement: Select[tuple[Offering]] = select(Offering)
        if provider_id is not None:
            statement = statement.where(Offering.provider_id == provider_id)
        if standard_model_id is not None:
            statement = statement.where(Offering.standard_model_id == standard_model_id)
        return list(self.session.scalars(statement.order_by(
            Offering.provider_id, Offering.provider_model_name)))

    def create_plan(self, offering_id: int, name: str, *, billing_mode: str,
                    currency_code: str, effective_from: datetime,
                    effective_to: datetime | None = None,
                    minimum_commitment_amount: Any = None,
                    terms: dict[str, Any] | None = None) -> PricingPlan:
        offering, name = self.get_offering(offering_id), _clean(name, "plan name")
        currency = _clean(currency_code, "currency code").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency code must contain exactly three letters")
        existing = self.session.scalar(select(PricingPlan).where(
            PricingPlan.offering_id == offering.id, PricingPlan.name == name,
            PricingPlan.effective_from == effective_from))
        if existing:
            return existing
        value = PricingPlan(
            offering=offering, name=name,
            billing_mode=_clean(billing_mode, "billing mode").casefold(),
            currency_code=currency, effective_from=effective_from,
            effective_to=effective_to,
            minimum_commitment_amount=minimum_commitment_amount, terms=dict(terms or {}))
        self.session.add(value)
        self._flush("pricing plan already exists for this effective date")
        return value

    create_pricing_plan = create_plan
    get_or_create_plan = create_plan

    def get_plan(self, plan_id: int) -> PricingPlan:
        value = self.session.get(PricingPlan, plan_id)
        if value is None:
            raise CatalogNotFoundError(f"pricing plan {plan_id} not found")
        return value

    def list_plans(self, offering_id: int) -> list[PricingPlan]:
        return list(self.session.scalars(select(PricingPlan).where(
            PricingPlan.offering_id == offering_id).order_by(
            PricingPlan.effective_from, PricingPlan.name)))

    def create_alias(self, alias: str, standard_model_id: int, *,
                     provider_id: int | None = None,
                     model_version_id: int | None = None,
                     alias_type: str = "imported", confidence: Any = None,
                     verified: bool = False, human_confirmed: bool = False,
                     source_note: str | None = None) -> ModelAlias:
        model = self.get_model(standard_model_id)
        provider = None if provider_id is None else self.get_provider(provider_id)
        version = None
        if model_version_id is not None:
            version = self.get_version(model_version_id)
            if version.standard_model_id != model.id:
                raise CatalogConflictError("alias version belongs to a different model")
        alias, active = _clean(alias, "alias"), bool(verified or human_confirmed)
        normalized = normalize_model_name(alias)
        scope = ModelAlias.provider_id.is_(None) if provider is None else ModelAlias.provider_id == provider.id
        existing = self.session.scalar(select(ModelAlias).where(
            scope, ModelAlias.normalized_alias == normalized))
        if existing:
            if existing.standard_model_id != model.id or existing.model_version_id != model_version_id:
                raise CatalogConflictError("normalized alias is bound to another model")
            if active and not existing.is_verified:
                existing.is_verified = True
                existing.source_note = source_note or existing.source_note
                self.session.flush()
            return existing
        value = ModelAlias(
            alias=alias, normalized_alias=normalized,
            alias_type=_clean(alias_type, "alias type").casefold(),
            provider=provider, standard_model=model, model_version=version,
            confidence=confidence, is_verified=active, source_note=source_note)
        self.session.add(value)
        self._flush("normalized alias is already in use in this scope")
        return value

    def list_aliases(self, *, standard_model_id: int | None = None,
                     provider_id: int | None = None,
                     verified_only: bool = False) -> list[ModelAlias]:
        statement: Select[tuple[ModelAlias]] = select(ModelAlias)
        if standard_model_id is not None:
            statement = statement.where(ModelAlias.standard_model_id == standard_model_id)
        if provider_id is not None:
            statement = statement.where(ModelAlias.provider_id == provider_id)
        if verified_only:
            statement = statement.where(ModelAlias.is_verified.is_(True))
        return list(self.session.scalars(statement.order_by(ModelAlias.normalized_alias)))

    def match_model(self, query: str, *, provider_id: int | None = None,
                    qwen_client: CandidateSelector | None = None,
                    fuzzy_threshold: float = 88.0,
                    ambiguity_margin: float = 5.0) -> CatalogMatch:
        """Match presentations only; no database identifier is sent to Qwen."""
        models = list(self.session.scalars(select(StandardModel).options(
            selectinload(StandardModel.aliases), selectinload(StandardModel.versions)
        ).where(StandardModel.is_active.is_(True)).order_by(StandardModel.id)))
        presentations, mappings = [], []
        for model in models:
            aliases = [a.alias for a in model.aliases if a.is_verified and (
                a.provider_id is None or a.provider_id == provider_id)]
            presentations.append(ModelCandidate(
                canonical_name=model.display_name or model.canonical_name,
                aliases=tuple(aliases),
                description=model.family,
            ))
            mappings.append((model, None))
        result = ModelMatcher(
            presentations, qwen_client=qwen_client,
            fuzzy_threshold=fuzzy_threshold,
            ambiguity_margin=ambiguity_margin).match(query)
        if result.review_required or result.candidate_index is None:
            return CatalogMatch(result=result)
        model, version = mappings[result.candidate_index]
        normalized_query = normalize_model_name(query)
        version_ids = {a.model_version_id for a in model.aliases if
                       a.is_verified and a.model_version_id is not None and
                       (a.provider_id is None or a.provider_id == provider_id) and
                       a.normalized_alias == normalized_query}
        if len(version_ids) == 1:
            version = self.session.get(ModelVersion, version_ids.pop())
        return CatalogMatch(result=result, standard_model=model, model_version=version)

    match = match_model

