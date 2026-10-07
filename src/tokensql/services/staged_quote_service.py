"""Human confirmation boundary for parsed Excel quote candidates."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    ImportBatch,
    ImportRow,
    ModelVersion,
    Offering,
    PricingPlan,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
    StandardModel,
)
from tokensql.services.pricing_service import (
    KNOWN_CONDITION_KEYS,
    KNOWN_CURRENCIES,
    KNOWN_UNITS,
)


CONFIRMATION_REVIEW_REASON = "human_quote_confirmation_requires_approval"


class StagedQuoteNotFoundError(LookupError):
    pass


class StagedQuoteConfirmationError(ValueError):
    pass


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StagedQuoteConfirmationError(
            f"{label} must be decimal-compatible"
        ) from exc
    if not result.is_finite():
        raise StagedQuoteConfirmationError(f"{label} must be finite")
    return result


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise StagedQuoteConfirmationError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class StagedQuoteConfirmationService:
    """Confirm parsed candidates without approving or publishing them."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, staged_quote_id: int) -> StagedQuote:
        value = self.session.scalar(
            select(StagedQuote)
            .options(
                joinedload(StagedQuote.import_row)
                .joinedload(ImportRow.import_batch)
                .joinedload(ImportBatch.source_artifact),
                selectinload(StagedQuote.review_items),
            )
            .where(StagedQuote.id == staged_quote_id)
        )
        if value is None:
            raise StagedQuoteNotFoundError(
                f"staged quote {staged_quote_id} not found"
            )
        return value

    def list(
        self,
        *,
        review_status: str | None = None,
        import_batch_id: int | None = None,
        limit: int = 100,
    ) -> list[StagedQuote]:
        if limit < 1 or limit > 1000:
            raise StagedQuoteConfirmationError("limit must be between 1 and 1000")
        statement = select(StagedQuote).options(
            joinedload(StagedQuote.import_row)
            .joinedload(ImportRow.import_batch)
            .joinedload(ImportBatch.source_artifact),
            selectinload(StagedQuote.review_items),
        )
        if review_status is not None:
            statement = statement.where(StagedQuote.review_status == review_status)
        if import_batch_id is not None:
            statement = statement.join(StagedQuote.import_row).where(
                StagedQuote.import_row.has(import_batch_id=import_batch_id)
            )
        return list(
            self.session.scalars(
                statement.order_by(StagedQuote.created_at, StagedQuote.id).limit(limit)
            ).unique()
        )

    def confirm(
        self,
        staged_quote_id: int,
        *,
        actor: str,
        source_artifact_id: int,
        provider_id: int,
        standard_model_id: int,
        model_version_id: int | None,
        offering_id: int,
        pricing_plan_id: int,
        currency_code: str,
        observed_at: datetime,
        effective_from: datetime,
        effective_to: datetime | None,
        components: Sequence[Mapping[str, Any]],
        note: str,
    ) -> StagedQuote:
        actor = " ".join(actor.split()) if isinstance(actor, str) else ""
        note = " ".join(note.split()) if isinstance(note, str) else ""
        if not actor:
            raise StagedQuoteConfirmationError("actor cannot be blank")
        if not note:
            raise StagedQuoteConfirmationError("confirmation note cannot be blank")
        staged = self.get(staged_quote_id)
        if staged.review_status in {"approved", "rejected"}:
            raise StagedQuoteConfirmationError(
                "approved or rejected staged quotes cannot be reconfirmed"
            )
        original = staged.extracted_quote
        if not isinstance(original, Mapping):
            raise StagedQuoteConfirmationError("staged quote payload is invalid")
        if original.get("_human_confirmation") is not None:
            raise StagedQuoteConfirmationError(
                "staged quote already has a human confirmation"
            )

        artifact = staged.import_row.import_batch.source_artifact
        if artifact.id != source_artifact_id:
            raise StagedQuoteConfirmationError(
                "source artifact does not belong to the staged quote"
            )
        if artifact.data_source.source_type == "official":
            raise StagedQuoteConfirmationError(
                "official candidates require the official verification workflow"
            )
        if not artifact.data_source.is_active:
            raise StagedQuoteConfirmationError("data source is inactive")

        provider = self.session.get(Provider, provider_id)
        model = self.session.get(StandardModel, standard_model_id)
        version = (
            None
            if model_version_id is None
            else self.session.get(ModelVersion, model_version_id)
        )
        offering = self.session.get(Offering, offering_id)
        plan = self.session.get(PricingPlan, pricing_plan_id)
        if provider is None or model is None or offering is None or plan is None:
            raise StagedQuoteConfirmationError(
                "provider, model, offering, and pricing plan must exist"
            )
        if staged.provider_id != provider.id:
            raise StagedQuoteConfirmationError(
                "provider does not match the imported staged quote"
            )
        if version is not None and version.standard_model_id != model.id:
            raise StagedQuoteConfirmationError(
                "model version belongs to another standard model"
            )
        if (
            offering.provider_id != provider.id
            or offering.standard_model_id != model.id
            or offering.model_version_id != model_version_id
        ):
            raise StagedQuoteConfirmationError(
                "offering does not match provider, model, and version"
            )
        if plan.offering_id != offering.id:
            raise StagedQuoteConfirmationError(
                "pricing plan belongs to another offering"
            )

        currency = currency_code.strip().upper()
        if currency not in KNOWN_CURRENCIES:
            raise StagedQuoteConfirmationError("unsupported snapshot currency")
        if currency != plan.currency_code:
            raise StagedQuoteConfirmationError(
                "snapshot currency must match the pricing plan"
            )
        observed = _aware(observed_at, "observed_at")
        starts = _aware(effective_from, "effective_from")
        ends = None if effective_to is None else _aware(effective_to, "effective_to")
        if ends is not None and ends <= starts:
            raise StagedQuoteConfirmationError(
                "effective_to must be after effective_from"
            )

        candidates = original.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise StagedQuoteConfirmationError("staged quote has no parsed candidates")
        if not components:
            raise StagedQuoteConfirmationError(
                "at least one confirmed component is required"
            )
        confirmed: list[dict[str, Any]] = []
        selected: set[int] = set()
        fingerprints: set[tuple[Any, ...]] = set()
        candidate_evidence: list[dict[str, Any]] = []
        for position, component in enumerate(components):
            if not isinstance(component, Mapping):
                raise StagedQuoteConfirmationError(
                    f"component {position} must be an object"
                )
            candidate_index = component.get("candidate_index")
            if (
                isinstance(candidate_index, bool)
                or not isinstance(candidate_index, int)
                or candidate_index < 0
                or candidate_index >= len(candidates)
            ):
                raise StagedQuoteConfirmationError(
                    f"component {position} has an invalid candidate_index"
                )
            if candidate_index in selected:
                raise StagedQuoteConfirmationError(
                    "each parsed candidate may be confirmed only once"
                )
            selected.add(candidate_index)
            candidate = candidates[candidate_index]
            candidate_evidence.append(
                {
                    "candidate_index": candidate_index,
                    "source_coordinate": candidate.get("source_coordinate"),
                    "candidate_sha256": _canonical_digest(candidate),
                }
            )
            normalized = self._component(component, position, currency)
            fingerprint = (
                normalized["billing_metric_id"],
                normalized.get("tier_start", "0"),
                normalized.get("tier_end"),
                json.dumps(
                    normalized.get("conditions", {}),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            if fingerprint in fingerprints:
                raise StagedQuoteConfirmationError(
                    "confirmed components contain a duplicate metric/tier/condition"
                )
            fingerprints.add(fingerprint)
            confirmed.append(normalized)

        before = {
            "review_status": staged.review_status,
            "matched_standard_model_id": staged.matched_standard_model_id,
            "matched_model_version_id": staged.matched_model_version_id,
            "matched_offering_id": staged.matched_offering_id,
            "extracted_quote_sha256": _canonical_digest(original),
        }
        evidence = {
            "actor": actor,
            "note": note,
            "source_artifact_id": artifact.id,
            "source_sha256": artifact.sha256,
            "candidate_evidence": candidate_evidence,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        staged.provider_id = provider.id
        staged.matched_standard_model_id = model.id
        staged.matched_model_version_id = model_version_id
        staged.matched_offering_id = offering.id
        staged.extracted_plan_name = plan.name
        staged.match_method = "manual"
        staged.extracted_quote = {
            **dict(original),
            "schema_version": 2,
            "origin": "third_party",
            "pricing_plan_id": plan.id,
            "currency_code": currency,
            "observed_at": observed.isoformat(),
            "effective_from": starts.isoformat(),
            "effective_to": None if ends is None else ends.isoformat(),
            "components": confirmed,
            "_human_confirmation": evidence,
        }
        staged.review_status = "needs_review"
        if CONFIRMATION_REVIEW_REASON not in staged.validation_errors:
            staged.validation_errors = [
                *staged.validation_errors,
                CONFIRMATION_REVIEW_REASON,
            ]
        staged.review_items.append(
            ReviewItem(
                review_type="price",
                status="open",
                severity="warning",
                reason=CONFIRMATION_REVIEW_REASON,
                proposed_resolution={
                    "action": "approve_human_quote_confirmation",
                    "source_artifact_id": artifact.id,
                    "pricing_plan_id": plan.id,
                    "confirmation_sha256": _canonical_digest(evidence),
                },
            )
        )
        self.session.flush()
        self.session.add(
            AuditEvent(
                actor=actor,
                action="confirm_staged_quote",
                entity_type="staged_quote",
                entity_id=str(staged.id),
                correlation_id=staged.import_row.import_batch.idempotency_key,
                before_data=before,
                after_data={
                    "review_status": staged.review_status,
                    "provider_id": provider.id,
                    "standard_model_id": model.id,
                    "model_version_id": model_version_id,
                    "offering_id": offering.id,
                    "pricing_plan_id": plan.id,
                    "source_artifact_id": artifact.id,
                    "component_count": len(confirmed),
                    "confirmation_sha256": _canonical_digest(evidence),
                },
            )
        )
        self.session.flush()
        return staged

    def _component(
        self, value: Mapping[str, Any], position: int, snapshot_currency: str
    ) -> dict[str, Any]:
        metric_id = value.get("billing_metric_id")
        if isinstance(metric_id, bool) or not isinstance(metric_id, int):
            raise StagedQuoteConfirmationError(
                f"component {position} has an invalid billing_metric_id"
            )
        metric = self.session.get(BillingMetric, metric_id)
        if metric is None:
            raise StagedQuoteConfirmationError(
                f"component {position} has an unknown billing metric"
            )
        kind = str(value.get("component_type", "")).casefold()
        conditions = value.get("conditions") or {}
        if not isinstance(conditions, Mapping):
            raise StagedQuoteConfirmationError("conditions must be an object")
        unknown = sorted(set(conditions) - KNOWN_CONDITION_KEYS)
        if unknown:
            raise StagedQuoteConfirmationError(
                "unknown pricing condition(s): " + ", ".join(unknown)
            )
        try:
            safe_conditions = json.loads(json.dumps(dict(conditions)))
        except (TypeError, ValueError) as exc:
            raise StagedQuoteConfirmationError(
                "conditions must be JSON-compatible"
            ) from exc
        tier_start = _decimal(value.get("tier_start", 0), "tier_start")
        tier_end = (
            None
            if value.get("tier_end") is None
            else _decimal(value["tier_end"], "tier_end")
        )
        if tier_start < 0 or (tier_end is not None and tier_end <= tier_start):
            raise StagedQuoteConfirmationError("invalid component tier range")
        common = {
            "billing_metric_id": metric.id,
            "component_type": kind,
            "tier_start": str(tier_start),
            "tier_end": None if tier_end is None else str(tier_end),
            "conditions": safe_conditions,
        }
        if kind == "discount":
            rate = _decimal(value.get("discount_rate"), "discount_rate")
            if rate < 0 or rate > 1:
                raise StagedQuoteConfirmationError(
                    "discount_rate must be between 0 and 1"
                )
            return {**common, "discount_rate": str(rate)}
        if kind != "monetary":
            raise StagedQuoteConfirmationError("unsupported component type")
        amount = _decimal(value.get("raw_amount"), "raw_amount")
        quantity = _decimal(value.get("raw_unit_quantity"), "raw_unit_quantity")
        currency = str(value.get("raw_currency_code", "")).strip().upper()
        unit = str(value.get("raw_unit_name", "")).strip().casefold()
        if amount < 0 or quantity <= 0:
            raise StagedQuoteConfirmationError(
                "amount must be non-negative and unit quantity positive"
            )
        if currency not in KNOWN_CURRENCIES or currency != snapshot_currency:
            raise StagedQuoteConfirmationError(
                "component currency must match the supported snapshot currency"
            )
        if unit not in KNOWN_UNITS or unit != metric.unit_name.casefold():
            raise StagedQuoteConfirmationError(
                "component unit does not match the billing metric"
            )
        return {
            **common,
            "raw_amount": str(amount),
            "raw_currency_code": currency,
            "raw_unit_name": unit,
            "raw_unit_quantity": str(quantity),
        }
