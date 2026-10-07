"""Validated append-only price publication, history, comparison, and CSV export."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from tokensql.db.base import utc_now
from tokensql.db.models import (
    BillingMetric, DataSource, FXRate, PriceComponent, PriceNormalization,
    PriceSnapshot, PricingPlan, StagedQuote,
)
from tokensql.pricing import (
    ComparisonResult, FXQuote, NormalizedPrice, PriceKind, PriceQuote,
    compare_prices, normalize_price,
)


KNOWN_CURRENCIES = frozenset({
    "CNY", "USD", "EUR", "GBP", "JPY", "HKD", "SGD", "AUD", "CAD", "KRW",
})
KNOWN_UNITS = frozenset({
    "token", "tokens", "tok", "second", "seconds", "sec", "s", "request",
    "requests", "call", "image", "images", "picture", "character",
    "characters", "char", "voice", "voices", "voice_tone", "音色", "字符",
    "次", "张", "秒",
})
KNOWN_CONDITION_KEYS = frozenset({
    "cache", "batch", "region", "service_tier", "tier", "time_window",
    "input_length", "output_length", "context_length", "priority",
})
DEFAULT_NORMALIZATION_ALGORITHM = "tokensql-normalization-v1"


class PricingValidationError(ValueError):
    pass


class DuplicatePublicationError(PricingValidationError):
    pass


class PricingNotFoundError(LookupError):
    pass


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float):
        raise PricingValidationError(f"{label} must not be a float")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PricingValidationError(f"{label} must be decimal-compatible") from exc
    if not result.is_finite():
        raise PricingValidationError(f"{label} must be finite")
    return result


def _datetime(value: Any, label: str, *, default: datetime | None = None) -> datetime:
    if value is None:
        if default is None:
            raise PricingValidationError(f"{label} is required")
        return default
    result = value
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PricingValidationError(f"{label} is not an ISO datetime") from exc
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise PricingValidationError(f"{label} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise PricingValidationError(f"unsupported JSON value: {type(value).__name__}")


def _conditions(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PricingValidationError("conditions must be an object")
    unknown = sorted(set(value) - KNOWN_CONDITION_KEYS)
    if unknown:
        raise PricingValidationError(f"unknown pricing condition(s): {', '.join(unknown)}")
    return _json_value(value)


def _currency(value: Any, label: str = "currency") -> str:
    currency = str(value or "").strip().upper()
    if currency not in KNOWN_CURRENCIES:
        raise PricingValidationError(f"unknown {label}: {currency or '<blank>'}")
    return currency


def _condition_key(conditions: Mapping[str, Any]) -> str:
    if not conditions:
        return "default"
    canonical = json.dumps(conditions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PricingService:
    """Publish only approved staged quotes into immutable price snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_fx_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        rate: Any,
        rate_date: date,
        data_source_id: int,
        verification_status: str = "pending",
        observed_at: datetime | None = None,
    ) -> FXRate:
        """Create immutable, attributable FX evidence or reuse an exact fact."""

        base = _currency(base_currency, "base currency")
        quote = _currency(quote_currency, "quote currency")
        if base == quote:
            raise PricingValidationError("FX currencies must differ")
        value = _decimal(rate, "FX rate")
        if value <= 0:
            raise PricingValidationError("FX rate must be positive")
        if isinstance(rate_date, datetime) or not isinstance(rate_date, date):
            raise PricingValidationError("rate_date must be a date")
        if (
            isinstance(data_source_id, bool)
            or not isinstance(data_source_id, int)
            or data_source_id <= 0
        ):
            raise PricingValidationError("data_source_id must be a positive integer")
        source = self.session.get(DataSource, data_source_id)
        if source is None:
            raise PricingNotFoundError(f"data source {data_source_id} not found")
        status = str(verification_status).strip().casefold()
        if status not in {"pending", "verified", "rejected"}:
            raise PricingValidationError("unsupported FX verification status")
        observed = _datetime(observed_at, "observed_at", default=utc_now())

        existing = self.session.scalar(
            select(FXRate).where(
                FXRate.base_currency == base,
                FXRate.quote_currency == quote,
                FXRate.rate == value,
                FXRate.rate_date == rate_date,
                FXRate.data_source_id == source.id,
                FXRate.verification_status == status,
            )
        )
        if existing is not None:
            return existing
        result = FXRate(
            base_currency=base,
            quote_currency=quote,
            rate=value,
            rate_date=rate_date,
            data_source=source,
            observed_at=observed,
            verification_status=status,
        )
        self.session.add(result)
        self.session.flush()
        return result

    def store_normalization(
        self,
        component: PriceComponent | int,
        *,
        fx_rate: FXRate | int | None = None,
        fx_rate_id: int | None = None,
        algorithm_version: str = DEFAULT_NORMALIZATION_ALGORITHM,
    ) -> PriceNormalization:
        """Normalize a monetary component using only persisted FX evidence."""

        value = self._get_component(component)
        if value.component_type != "monetary":
            raise PricingValidationError("only monetary components can be normalized")
        if not isinstance(algorithm_version, str):
            raise PricingValidationError("algorithm_version must be a string")
        algorithm = algorithm_version.strip()
        if not algorithm or len(algorithm) > 80:
            raise PricingValidationError(
                "algorithm_version must contain between 1 and 80 characters"
            )
        if fx_rate is not None and fx_rate_id is not None:
            raise PricingValidationError("pass fx_rate or fx_rate_id, not both")
        requested_fx = fx_rate if fx_rate is not None else fx_rate_id
        persisted_fx = self._persisted_fx(requested_fx)
        currency = _currency(value.raw_currency_code)
        if currency == "CNY" and persisted_fx is not None:
            raise PricingValidationError("CNY normalization must not attach an FX rate")
        if currency != "CNY":
            if persisted_fx is None:
                raise PricingValidationError(
                    f"verified persisted FX evidence is required for {currency}/CNY"
                )
            self._validate_fx_for_component(value, persisted_fx, currency)

        fx_quote = None
        if persisted_fx is not None:
            source = self.session.get(DataSource, persisted_fx.data_source_id)
            if source is None:
                raise PricingNotFoundError(
                    f"data source {persisted_fx.data_source_id} not found"
                )
            fx_quote = FXQuote(
                base_currency=persisted_fx.base_currency,
                quote_currency=persisted_fx.quote_currency,
                rate=persisted_fx.rate,
                rate_date=persisted_fx.rate_date,
                source=source.base_url or source.name,
                verification_status=persisted_fx.verification_status,
            )

        quote_status = value.snapshot.verification_status
        if quote_status == "superseded":
            quote_status = "verified"
        quote = PriceQuote(
            amount=value.raw_amount,
            currency=currency,
            unit_name=value.raw_unit_name,
            unit_quantity=value.raw_unit_quantity,
            metric=value.billing_metric.comparable_group,
            direction=value.billing_metric.direction,
            conditions=value.conditions,
            verification_status=quote_status,
        )
        normalized = normalize_price(quote, fx_quote)
        if (
            normalized.normalized_amount is None
            or normalized.normalized_currency is None
            or normalized.normalized_unit_name is None
            or normalized.normalized_unit_quantity is None
            or normalized.comparison_key is None
            or normalized.formula is None
        ):
            detail = "; ".join(normalized.pending_reasons) or "normalization failed"
            raise PricingValidationError(detail)
        quantity = normalized.normalized_unit_quantity
        if quantity != quantity.to_integral_value():
            raise PricingValidationError("normalized unit quantity must be integral")

        status = "pending"
        if value.snapshot.verification_status in {"verified", "superseded"}:
            status = "verified"
        elif value.snapshot.verification_status == "rejected":
            status = "rejected"
        fx_id = None if persisted_fx is None else persisted_fx.id
        statement = select(PriceNormalization).where(
            PriceNormalization.price_component_id == value.id,
            PriceNormalization.algorithm_version == algorithm,
        )
        statement = statement.where(
            PriceNormalization.fx_rate_id.is_(None)
            if fx_id is None
            else PriceNormalization.fx_rate_id == fx_id
        )
        existing = self.session.scalar(statement)
        if existing is not None:
            return existing

        record = PriceNormalization(
            price_component=value,
            fx_rate=persisted_fx,
            algorithm_version=algorithm,
            comparison_key=normalized.comparison_key,
            verification_status=status,
            normalized_amount=normalized.normalized_amount,
            normalized_currency_code=normalized.normalized_currency,
            normalized_unit_name=normalized.normalized_unit_name,
            normalized_unit_quantity=int(quantity),
            formula=normalized.formula,
        )
        self.session.add(record)
        self.session.flush()
        return record

    normalize_component = store_normalization

    def _persisted_fx(self, value: FXRate | int | None) -> FXRate | None:
        if value is None:
            return None
        identifier = value.id if isinstance(value, FXRate) else value
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier <= 0
        ):
            raise PricingValidationError("FX rate must be a persisted database row")
        result = self.session.get(FXRate, identifier)
        if result is None:
            raise PricingNotFoundError(f"FX rate {identifier} not found")
        return result

    @staticmethod
    def _validate_fx_for_component(
        component: PriceComponent, fx: FXRate, currency: str
    ) -> None:
        if fx.verification_status != "verified":
            raise PricingValidationError("FX rate must be verified")
        direct = fx.base_currency == currency and fx.quote_currency == "CNY"
        inverse = fx.base_currency == "CNY" and fx.quote_currency == currency
        if not (direct or inverse):
            raise PricingValidationError(
                f"FX pair {fx.base_currency}/{fx.quote_currency} cannot convert "
                f"{currency} to CNY"
            )
        expected_date = component.snapshot.observed_at.astimezone(timezone.utc).date()
        if fx.rate_date != expected_date:
            raise PricingValidationError(
                f"FX rate date {fx.rate_date.isoformat()} does not match price "
                f"observation date {expected_date.isoformat()}"
            )

    def publish(
        self,
        staged_quote_id: int,
        *,
        pricing_plan_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> PriceSnapshot:
        staged = self.session.scalar(
            select(StagedQuote)
            .options(
                joinedload(StagedQuote.matched_offering),
                joinedload(StagedQuote.provider),
                joinedload(StagedQuote.import_row),
            )
            .where(StagedQuote.id == staged_quote_id)
        )
        if staged is None:
            raise PricingNotFoundError(f"staged quote {staged_quote_id} not found")
        if staged.review_status != "approved" or staged.validation_errors:
            raise PricingValidationError("only validated, approved staged quotes may be published")
        if staged.matched_offering_id is None:
            raise PricingValidationError("staged quote has no resolved offering")

        payload = staged.extracted_quote
        if not isinstance(payload, Mapping):
            raise PricingValidationError("extracted quote must be an object")
        plan = self._resolve_plan(staged, payload, pricing_plan_id)
        artifact = staged.import_row.import_batch.source_artifact
        source = artifact.data_source
        provider = staged.matched_offering.provider
        origin = str(payload.get("origin") or (
            "official" if source.source_type == "official" else "third_party"
        )).strip().casefold()
        if origin not in {"official", "third_party"}:
            raise PricingValidationError(f"unknown origin: {origin}")
        if origin == "official" and (
            source.authority_level != "primary"
            or source.source_type != "official"
            or provider.provider_type != "official"
        ):
            raise PricingValidationError(
                "official origin requires an official provider and primary official source"
            )

        components_data = payload.get("components")
        if not isinstance(components_data, Sequence) or isinstance(components_data, (str, bytes)):
            raise PricingValidationError("components must be a non-empty array")
        if not components_data:
            raise PricingValidationError("components must be a non-empty array")

        currency = _currency(payload.get("currency_code", plan.currency_code), "snapshot currency")
        observed_at = _datetime(payload.get("observed_at"), "observed_at", default=utc_now())
        effective_from = _datetime(
            payload.get("effective_from"), "effective_from", default=observed_at)
        effective_to = None
        if payload.get("effective_to") is not None:
            effective_to = _datetime(payload["effective_to"], "effective_to")
            if effective_to <= effective_from:
                raise PricingValidationError("effective_to must be after effective_from")

        safe_payload = _json_value(payload)
        key = idempotency_key or hashlib.sha256(json.dumps(
            {"staged_quote_id": staged.id, "payload": safe_payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        if not isinstance(key, str) or not key.strip():
            raise PricingValidationError("idempotency_key cannot be blank")
        key = key.strip()
        if len(key) > 255:
            raise PricingValidationError("idempotency_key cannot exceed 255 characters")

        previous = self.session.scalar(select(PriceSnapshot).where(
            PriceSnapshot.pricing_plan_id == plan.id
        ).order_by(
            PriceSnapshot.effective_from.desc(),
            PriceSnapshot.observed_at.desc(),
            PriceSnapshot.id.desc()).limit(1))
        raw_payload = dict(safe_payload)
        raw_payload["_publication"] = {
            "staged_quote_id": staged.id, "idempotency_key": key,
            "origin": origin,
        }
        try:
            with self.session.begin_nested():
                snapshot = PriceSnapshot(
                    pricing_plan=plan, data_source=source, source_artifact=artifact,
                    supersedes=previous, observed_at=observed_at,
                    effective_from=effective_from, effective_to=effective_to,
                    currency_code=currency, verification_status="verified",
                    publication_idempotency_key=key,
                    raw_payload=raw_payload)
                components = [
                    self._component(snapshot, component_data, index)
                    for index, component_data in enumerate(components_data)
                ]
                self.session.add_all([snapshot, *components])
                self.session.flush()
        except IntegrityError:
            if self._publication_exists(plan.id, artifact.id, key):
                raise DuplicatePublicationError(
                    f"publication idempotency key already exists: {key}"
                ) from None
            raise
        return snapshot

    publish_staged_quote = publish

    def _resolve_plan(
        self, staged: StagedQuote, payload: Mapping[str, Any],
        explicit_id: int | None,
    ) -> PricingPlan:
        plan_id = explicit_id if explicit_id is not None else payload.get("pricing_plan_id")
        if plan_id is not None:
            plan = self.session.get(PricingPlan, int(plan_id))
            if plan is None:
                raise PricingValidationError(f"pricing plan {plan_id} not found")
            if plan.offering_id != staged.matched_offering_id:
                raise PricingValidationError("pricing plan belongs to another offering")
            return plan
        name = payload.get("plan_name") or staged.extracted_plan_name
        if not isinstance(name, str) or not name.strip():
            raise PricingValidationError("staged quote has no resolved pricing plan")
        plans = list(self.session.scalars(select(PricingPlan).where(
            PricingPlan.offering_id == staged.matched_offering_id,
            PricingPlan.name == name.strip())))
        if len(plans) != 1:
            raise PricingValidationError("pricing plan name is missing or ambiguous")
        return plans[0]

    def _publication_exists(self, plan_id: int, artifact_id: int, key: str) -> bool:
        return self.session.scalar(select(PriceSnapshot.id).where(
            PriceSnapshot.pricing_plan_id == plan_id,
            PriceSnapshot.source_artifact_id == artifact_id,
            PriceSnapshot.publication_idempotency_key == key,
        ).limit(1)) is not None

    def _component(
        self, snapshot: PriceSnapshot, data: Any, index: int
    ) -> PriceComponent:
        if not isinstance(data, Mapping):
            raise PricingValidationError(f"component {index} must be an object")
        metric = None
        if data.get("billing_metric_id") is not None:
            metric = self.session.get(BillingMetric, int(data["billing_metric_id"]))
        elif data.get("metric_code") is not None:
            metric = self.session.scalar(select(BillingMetric).where(
                BillingMetric.code == str(data["metric_code"]).strip()))
        if metric is None:
            raise PricingValidationError(f"component {index} has unknown billing metric")

        kind = str(data.get("component_type", "")).casefold()
        has_amount = data.get("raw_amount", data.get("amount")) is not None
        has_discount = data.get("discount_rate") is not None
        if kind not in {"monetary", "discount"}:
            raise PricingValidationError(f"component {index} has unknown component type")
        if has_amount == has_discount or (kind == "monetary") != has_amount:
            raise PricingValidationError(
                f"component {index} must contain exactly one of amount or discount_rate")
        conditions = _conditions(data.get("conditions"))
        tier_start = _decimal(data.get("tier_start", 0), "tier_start")
        tier_end = None if data.get("tier_end") is None else _decimal(data["tier_end"], "tier_end")
        if tier_start < 0 or (tier_end is not None and tier_end <= tier_start):
            raise PricingValidationError(f"component {index} has invalid tier range")

        common = dict(
            snapshot=snapshot, billing_metric=metric,
            component_type=kind, tier_start=tier_start, tier_end=tier_end,
            condition_key=str(data.get("condition_key") or _condition_key(conditions)),
            conditions=conditions)
        if kind == "discount":
            forbidden = ("raw_currency_code", "currency", "raw_unit_name", "unit_name",
                         "raw_unit_quantity", "unit_quantity")
            if any(data.get(field) is not None for field in forbidden):
                raise PricingValidationError(
                    f"component {index} discount cannot contain monetary fields")
            rate = _decimal(data["discount_rate"], "discount_rate")
            if rate < 0 or rate > 1:
                raise PricingValidationError("discount_rate must be between 0 and 1")
            return PriceComponent(discount_rate=rate, **common)

        amount = _decimal(data.get("raw_amount", data.get("amount")), "amount")
        if amount < 0:
            raise PricingValidationError("amount must be non-negative")
        currency = _currency(data.get(
            "raw_currency_code", data.get("currency", snapshot.currency_code)))
        unit = str(data.get("raw_unit_name", data.get("unit_name", ""))).strip().casefold()
        if unit not in KNOWN_UNITS:
            raise PricingValidationError(f"unknown unit: {unit or '<blank>'}")
        quantity = _decimal(data.get(
            "raw_unit_quantity", data.get("unit_quantity")), "unit_quantity")
        if quantity <= 0:
            raise PricingValidationError("unit_quantity must be positive")
        return PriceComponent(
            raw_amount=amount, raw_currency_code=currency,
            raw_unit_name=unit, raw_unit_quantity=quantity, **common)

    def history(self, pricing_plan_id: int) -> list[PriceSnapshot]:
        return list(self.session.scalars(select(PriceSnapshot).options(
            selectinload(PriceSnapshot.components).joinedload(
                PriceComponent.billing_metric)
        ).where(PriceSnapshot.pricing_plan_id == pricing_plan_id).order_by(
            PriceSnapshot.effective_from, PriceSnapshot.observed_at,
            PriceSnapshot.id)))

    get_history = history

    def current(
        self, pricing_plan_id: int, *, as_of: datetime | None = None
    ) -> PriceSnapshot | None:
        when = as_of or utc_now()
        return self.session.scalar(select(PriceSnapshot).options(
            selectinload(PriceSnapshot.components).joinedload(
                PriceComponent.billing_metric)
        ).where(
            PriceSnapshot.pricing_plan_id == pricing_plan_id,
            PriceSnapshot.verification_status == "verified",
            PriceSnapshot.effective_from <= when,
            or_(PriceSnapshot.effective_to.is_(None), PriceSnapshot.effective_to > when)
        ).order_by(
            PriceSnapshot.effective_from.desc(),
            PriceSnapshot.observed_at.desc(), PriceSnapshot.id.desc()).limit(1))

    get_current = current

    @staticmethod
    def normalized_component(
        component: PriceComponent, fx: FXQuote | None = None
    ):
        if component.component_type != "monetary":
            raise PricingValidationError(
                "discount components require an explicit verified base price")
        quote = PriceQuote(
            amount=component.raw_amount,
            currency=component.raw_currency_code,
            unit_name=component.raw_unit_name,
            unit_quantity=component.raw_unit_quantity,
            metric=component.billing_metric.comparable_group,
            direction=component.billing_metric.direction,
            conditions=component.conditions,
            verification_status=component.snapshot.verification_status,
        )
        return normalize_price(quote, fx)

    def compare(
        self, left: PriceComponent | int, right: PriceComponent | int, *,
        left_fx: FXQuote | None = None, right_fx: FXQuote | None = None,
        left_normalization_id: int | None = None,
        right_normalization_id: int | None = None,
        algorithm_version: str | None = None,
        use_persisted: bool = True,
    ) -> ComparisonResult:
        if left_fx is not None or right_fx is not None:
            raise PricingValidationError(
                "in-memory FX quotes are not trusted; persist a verified FXRate "
                "and normalization first"
            )
        left_value = self._get_component(left)
        right_value = self._get_component(right)
        left_normalized = self._comparison_normalization(
            left_value,
            normalization_id=left_normalization_id,
            algorithm_version=algorithm_version,
            use_persisted=use_persisted,
        )
        right_normalized = self._comparison_normalization(
            right_value,
            normalization_id=right_normalization_id,
            algorithm_version=algorithm_version,
            use_persisted=use_persisted,
        )
        return compare_prices(
            left_normalized,
            right_normalized,
        )

    compare_components = compare

    def _comparison_normalization(
        self,
        component: PriceComponent,
        *,
        normalization_id: int | None,
        algorithm_version: str | None,
        use_persisted: bool,
    ) -> NormalizedPrice:
        if not use_persisted:
            raise PricingValidationError(
                "comparisons require verified persisted normalizations"
            )
        record: PriceNormalization | None = None
        algorithm: str | None = None
        if algorithm_version is not None:
            if not isinstance(algorithm_version, str):
                raise PricingValidationError("algorithm_version must be a string")
            algorithm = algorithm_version.strip()
            if not algorithm:
                raise PricingValidationError("algorithm_version cannot be blank")
        if normalization_id is not None:
            record = self.session.get(PriceNormalization, normalization_id)
            if record is None:
                raise PricingNotFoundError(
                    f"price normalization {normalization_id} not found"
                )
            if record.price_component_id != component.id:
                raise PricingValidationError(
                    "price normalization belongs to another component"
                )
            if record.verification_status != "verified":
                raise PricingValidationError(
                    "only verified persisted normalizations may be compared"
                )
            if algorithm is not None and record.algorithm_version != algorithm:
                raise PricingValidationError(
                    "price normalization uses another algorithm version"
                )
        elif use_persisted:
            statement = select(PriceNormalization).where(
                PriceNormalization.price_component_id == component.id,
                PriceNormalization.verification_status == "verified",
            )
            if algorithm is not None:
                statement = statement.where(
                    PriceNormalization.algorithm_version == algorithm
                )
            record = self.session.scalar(
                statement.order_by(
                    PriceNormalization.created_at.desc(),
                    PriceNormalization.id.desc(),
                ).limit(1)
            )
        if record is None:
            qualifier = f" for algorithm {algorithm!r}" if algorithm else ""
            raise PricingNotFoundError(
                "verified persisted price normalization not found "
                f"for component {component.id}{qualifier}"
            )
        return self._normalization_value(component, record)

    @staticmethod
    def _normalization_value(
        component: PriceComponent, record: PriceNormalization
    ) -> NormalizedPrice:
        return NormalizedPrice(
            kind=PriceKind.MONETARY,
            raw_amount=component.raw_amount,
            raw_currency=component.raw_currency_code,
            raw_unit_name=component.raw_unit_name,
            raw_unit_quantity=component.raw_unit_quantity,
            metric=component.billing_metric.comparable_group,
            direction=component.billing_metric.direction,
            conditions=component.conditions,
            normalized_amount=record.normalized_amount,
            normalized_currency=record.normalized_currency_code,
            normalized_unit_name=record.normalized_unit_name,
            normalized_unit_quantity=Decimal(record.normalized_unit_quantity),
            comparison_key=record.comparison_key,
            verification_status=record.verification_status,
            formula=record.formula,
        )

    def _get_component(self, value: PriceComponent | int) -> PriceComponent:
        if isinstance(value, PriceComponent):
            return value
        component = self.session.scalar(select(PriceComponent).options(
            joinedload(PriceComponent.billing_metric),
            joinedload(PriceComponent.snapshot)
        ).where(PriceComponent.id == value))
        if component is None:
            raise PricingNotFoundError(f"price component {value} not found")
        return component

    @staticmethod
    def export_csv(snapshots: Iterable[PriceSnapshot]) -> str:
        return price_snapshots_to_csv(snapshots)

    @staticmethod
    def stream_csv(snapshots: Iterable[PriceSnapshot]) -> Iterator[str]:
        return iter_price_snapshots_csv(snapshots)


_CSV_HEADER = (
    "snapshot_id", "pricing_plan_id", "observed_at", "effective_from",
    "effective_to", "source_id", "verification_status", "component_id",
    "metric_code", "component_type", "amount", "currency", "unit_name",
    "unit_quantity", "discount_rate", "conditions",
)


def price_snapshots_to_csv(snapshots: Iterable[PriceSnapshot]) -> str:
    """Pure serialization: no queries, mutation, commits, or network access."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(_CSV_HEADER)
    for snapshot in snapshots:
        for component in snapshot.components:
            writer.writerow((
                snapshot.id, snapshot.pricing_plan_id,
                snapshot.observed_at.isoformat(), snapshot.effective_from.isoformat(),
                "" if snapshot.effective_to is None else snapshot.effective_to.isoformat(),
                snapshot.data_source_id, snapshot.verification_status, component.id,
                component.billing_metric.code, component.component_type,
                "" if component.raw_amount is None else str(component.raw_amount),
                component.raw_currency_code or "", component.raw_unit_name or "",
                "" if component.raw_unit_quantity is None else str(component.raw_unit_quantity),
                "" if component.discount_rate is None else str(component.discount_rate),
                json.dumps(component.conditions, sort_keys=True, ensure_ascii=False),
            ))
    return output.getvalue()


def iter_price_snapshots_csv(snapshots: Iterable[PriceSnapshot]) -> Iterator[str]:
    yield from price_snapshots_to_csv(snapshots).splitlines(keepends=True)

