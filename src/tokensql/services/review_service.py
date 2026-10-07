"""Human-review workflow with append-only audit events."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tokensql.db.base import utc_now
from tokensql.db.models import (
    AliasRule, AuditEvent, ModelVersion, Offering, Provider, ReviewItem,
    StagedQuote, StandardModel,
)
from tokensql.matching import normalize_model_name

from .catalog_service import CatalogConflictError, CatalogService


class ReviewNotFoundError(LookupError):
    pass


class ReviewStateError(ValueError):
    pass


class AliasRuleConflictError(ReviewStateError):
    """The requested alias scope/pattern is already bound elsewhere."""


class ReviewService:
    """Resolve review items atomically and record every decision."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending(
        self,
        *,
        severity: str | None = None,
        review_type: str | None = None,
        limit: int | None = None,
    ) -> list[ReviewItem]:
        statement = select(ReviewItem).where(ReviewItem.status == "open")
        if severity is not None:
            statement = statement.where(ReviewItem.severity == severity)
        if review_type is not None:
            statement = statement.where(ReviewItem.review_type == review_type)
        statement = statement.order_by(ReviewItem.created_at, ReviewItem.id)
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    pending = list_pending

    def get(self, review_item_id: int) -> ReviewItem:
        item = self.session.get(ReviewItem, review_item_id)
        if item is None:
            raise ReviewNotFoundError(f"review item {review_item_id} not found")
        return item

    def approve(
        self,
        review_item_id: int,
        *,
        reviewer: str,
        resolution: Mapping[str, Any],
        resolved_at: datetime | None = None,
        create_alias_rule: bool | None = None,
    ) -> ReviewItem:
        item = self.get(review_item_id)
        self._require_open(item)
        reviewer = self._reviewer(reviewer)
        decision = dict(resolution)
        before = self._state(item)
        try:
            resolved_ids = self._validate_resolution(item.staged_quote, decision)
            make_rule = (
                bool(decision.get("create_alias_rule", False))
                if create_alias_rule is None
                else create_alias_rule
            )
            if make_rule:
                with self.session.begin_nested():
                    self._create_alias_rule(item, decision, resolved_ids)
        except ReviewStateError as exc:
            self._audit_failed_approval(item, reviewer, before, decision, exc)
            raise

        self._apply_staged_resolution(item.staged_quote, decision, resolved_ids)
        item.status = "resolved"
        item.resolution = decision
        item.resolved_by = reviewer
        item.resolved_at = resolved_at or utc_now()
        item.staged_quote.validation_errors = [
            error
            for error in item.staged_quote.validation_errors
            if str(error) != item.reason
        ]
        self.session.flush()
        self._refresh_staged_state(item.staged_quote)
        self.session.add(
            AuditEvent(
                actor=reviewer,
                action="approve",
                entity_type="review_item",
                entity_id=str(item.id),
                before_data=before,
                after_data=self._state(item),
            )
        )
        self.session.flush()
        return item

    approve_review = approve

    def reject(
        self,
        review_item_id: int,
        *,
        reviewer: str,
        resolution: Mapping[str, Any] | None = None,
        resolved_at: datetime | None = None,
    ) -> ReviewItem:
        item = self.get(review_item_id)
        self._require_open(item)
        reviewer = self._reviewer(reviewer)
        before = self._state(item)
        item.status = "dismissed"
        item.resolution = dict(resolution or {})
        item.resolved_by = reviewer
        item.resolved_at = resolved_at or utc_now()
        self.session.flush()
        self._refresh_staged_state(item.staged_quote)
        self.session.add(
            AuditEvent(
                actor=reviewer,
                action="reject",
                entity_type="review_item",
                entity_id=str(item.id),
                before_data=before,
                after_data=self._state(item),
            )
        )
        self.session.flush()
        return item

    reject_review = reject

    @staticmethod
    def _reviewer(value: str) -> str:
        value = " ".join(value.split()) if isinstance(value, str) else ""
        if not value:
            raise ValueError("reviewer cannot be blank")
        return value

    @staticmethod
    def _require_open(item: ReviewItem) -> None:
        if item.status != "open":
            raise ReviewStateError(f"review item {item.id} is already {item.status}")

    @staticmethod
    def _state(item: ReviewItem) -> dict[str, Any]:
        return {
            "status": item.status,
            "resolution": item.resolution,
            "resolved_by": item.resolved_by,
            "resolved_at": (
                None if item.resolved_at is None else item.resolved_at.isoformat()
            ),
            "staged_review_status": item.staged_quote.review_status,
            "row_status": item.staged_quote.import_row.status,
        }

    def _validate_resolution(
        self, staged: StagedQuote, resolution: Mapping[str, Any]
    ) -> tuple[int | None, int | None, int | None]:
        model_id = resolution.get(
            "standard_model_id", staged.matched_standard_model_id
        )
        if (
            "standard_model_id" in resolution
            and resolution["standard_model_id"] != staged.matched_standard_model_id
            and "model_version_id" not in resolution
        ):
            version_id = None
        else:
            version_id = resolution.get(
                "model_version_id", staged.matched_model_version_id
            )
        offering_id = resolution.get("offering_id", staged.matched_offering_id)

        if model_id is not None and self.session.get(StandardModel, model_id) is None:
            raise ReviewStateError(f"standard model {model_id} not found")
        if version_id is not None:
            version = self.session.get(ModelVersion, version_id)
            if version is None:
                raise ReviewStateError(f"model version {version_id} not found")
            if model_id != version.standard_model_id:
                raise ReviewStateError("resolved version belongs to another model")

        if "offering_id" in resolution:
            offering = self.session.get(Offering, resolution["offering_id"])
            if offering is None:
                raise ReviewStateError(
                    f"offering {resolution['offering_id']} not found"
                )
            if staged.provider_id is None or offering.provider_id != staged.provider_id:
                raise ReviewStateError(
                    "resolved offering belongs to another provider"
                )
            if model_id is None or offering.standard_model_id != model_id:
                raise ReviewStateError("resolved offering belongs to another model")
            if offering.model_version_id != version_id:
                raise ReviewStateError(
                    "resolved offering belongs to another model version"
                )
        elif offering_id is not None:
            existing = self.session.get(Offering, offering_id)
            if (
                existing is None
                or existing.provider_id != staged.provider_id
                or existing.standard_model_id != model_id
                or existing.model_version_id != version_id
            ):
                offering_id = None
        return model_id, version_id, offering_id

    @staticmethod
    def _apply_staged_resolution(
        staged: StagedQuote,
        resolution: Mapping[str, Any],
        resolved_ids: tuple[int | None, int | None, int | None],
    ) -> None:
        model_id, version_id, offering_id = resolved_ids
        staged.matched_standard_model_id = model_id
        staged.matched_model_version_id = version_id
        staged.matched_offering_id = offering_id
        if "standard_model_id" in resolution:
            staged.match_method = "manual"

    def _refresh_staged_state(self, staged: StagedQuote) -> None:
        statuses = list(
            self.session.scalars(
                select(ReviewItem.status).where(
                    ReviewItem.staged_quote_id == staged.id
                )
            )
        )
        if "dismissed" in statuses:
            staged.review_status = "rejected"
            staged.import_row.status = "rejected"
        elif statuses and all(status == "resolved" for status in statuses):
            staged.review_status = "approved"
            staged.import_row.status = "accepted"
            staged.validation_errors = []
        else:
            staged.review_status = "needs_review"
            staged.import_row.status = "staged"
        self.session.flush()

    def _audit_failed_approval(
        self,
        item: ReviewItem,
        reviewer: str,
        before: dict[str, Any],
        resolution: Mapping[str, Any],
        error: ReviewStateError,
    ) -> None:
        after = self._state(item)
        after["attempted_resolution"] = dict(resolution)
        after["error"] = str(error)
        self.session.add(
            AuditEvent(
                actor=reviewer,
                action="approve_rejected",
                entity_type="review_item",
                entity_id=str(item.id),
                before_data=before,
                after_data=after,
            )
        )
        self.session.flush()

    def _create_alias_rule(
        self,
        item: ReviewItem,
        resolution: Mapping[str, Any],
        resolved_ids: tuple[int | None, int | None, int | None],
    ) -> AliasRule:
        staged = item.staged_quote
        pattern_value = (
            resolution.get("alias")
            or resolution.get("pattern")
            or staged.extracted_model_name
        )
        if not isinstance(pattern_value, str) or not pattern_value.strip():
            raise ReviewStateError(
                "alias-rule approval requires a non-blank pattern"
            )
        alias_text = pattern_value.strip()
        match_type = str(
            resolution.get("match_type", "normalized")
        ).strip().casefold()
        if match_type not in {"exact", "regex", "normalized"}:
            raise ReviewStateError(
                f"unsupported alias match type: {match_type}"
            )
        pattern = (
            normalize_model_name(alias_text)
            if match_type == "normalized"
            else alias_text
        )
        provider_id = resolution.get("provider_id", staged.provider_id)
        if provider_id is not None:
            if (
                not isinstance(provider_id, int)
                or isinstance(provider_id, bool)
                or provider_id <= 0
                or self.session.get(Provider, provider_id) is None
            ):
                raise ReviewStateError(
                    f"alias-rule provider {provider_id!r} not found"
                )
            if (
                staged.provider_id is not None
                and provider_id != staged.provider_id
            ):
                raise ReviewStateError(
                    "alias-rule provider scope differs from the reviewed row"
                )
        elif staged.provider_id is not None:
            raise ReviewStateError(
                "provider-scoped review cannot create a global alias rule"
            )

        model_id, version_id, _ = resolved_ids
        if model_id is None:
            raise ReviewStateError(
                "alias-rule approval requires a resolved model"
            )
        try:
            priority = int(resolution.get("priority", 100))
        except (TypeError, ValueError) as exc:
            raise ReviewStateError(
                "alias-rule priority must be a non-negative integer"
            ) from exc
        if priority < 0:
            raise ReviewStateError(
                "alias-rule priority must be a non-negative integer"
            )

        provider_scope = (
            AliasRule.provider_id.is_(None)
            if provider_id is None
            else AliasRule.provider_id == provider_id
        )
        existing = self.session.scalar(
            select(AliasRule).where(
                provider_scope,
                AliasRule.match_type == match_type,
                AliasRule.pattern == pattern,
            )
        )
        if existing is not None and (
            existing.target_standard_model_id,
            existing.target_model_version_id,
        ) != (model_id, version_id):
            raise AliasRuleConflictError(
                "alias rule conflicts with an existing target; "
                "review remains open"
            )

        if match_type in {"exact", "normalized"}:
            try:
                CatalogService(self.session).create_alias(
                    alias_text,
                    model_id,
                    provider_id=provider_id,
                    model_version_id=version_id,
                    alias_type="imported",
                    confidence=1,
                    human_confirmed=True,
                    source_note=f"approved review_item:{item.id}",
                )
            except CatalogConflictError as exc:
                raise AliasRuleConflictError(
                    "verified alias conflicts with an existing target; "
                    "review remains open"
                ) from exc

        if existing is not None:
            return existing

        rule = AliasRule(
            provider_id=provider_id,
            match_type=match_type,
            pattern=pattern,
            target_standard_model_id=model_id,
            target_model_version_id=version_id,
            priority=priority,
            is_active=True,
            created_from_review_item=item,
        )
        self.session.add(rule)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise AliasRuleConflictError(
                "alias rule conflicts with a concurrently created rule; "
                "review remains open"
            ) from exc
        return rule

