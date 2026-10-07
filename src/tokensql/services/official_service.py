"""Staging and controlled publication for official price evidence.

The collector deliberately separates fetching/parsing from publication.  Adapter
output is always unverified staging data until a caller explicitly verifies it.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterator, Protocol, runtime_checkable

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from tokensql.db.base import utc_now
from tokensql.db.models import (
    AuditEvent,
    DataSource,
    ImportBatch,
    ImportRow,
    Offering,
    PriceSnapshot,
    PricingPlan,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.official.base import FetchResult
from tokensql.official.http import OfficialHttpClient
from tokensql.services.pricing_service import PricingService


class OfficialSourceValidationError(ValueError):
    """The source, adapter output, or verification request is not trustworthy."""


class OfficialCatalogError(OfficialSourceValidationError):
    """A staged candidate is not completely linked to the catalog."""


@runtime_checkable
class OfficialPriceAdapter(Protocol):
    """Vendor-specific URL selection and deterministic page parsing contract."""

    @property
    def source_name(self) -> str: ...

    def supports(self, offering: Offering) -> bool: ...

    def url_for(self, offering: Offering) -> str: ...

    def parse(self, result: FetchResult) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class OfficialCollectionResult:
    offering_id: int
    status: str
    source_artifact_id: int | None = None
    import_batch_id: int | None = None
    staged_quote_ids: tuple[int, ...] = ()
    review_reasons: tuple[str, ...] = ()
    reused_existing: bool = False

    @property
    def candidate_count(self) -> int:
        return len(self.staged_quote_ids)


@contextmanager
def _transaction(session: Session) -> Iterator[None]:
    manager = session.begin_nested() if session.in_transaction() else session.begin()
    with manager:
        yield


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise OfficialSourceValidationError("naive datetimes are not allowed")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _adapter_name(adapter: OfficialPriceAdapter) -> str:
    name = str(getattr(adapter, "source_name", type(adapter).__name__)).strip()
    if not name:
        raise OfficialSourceValidationError("adapter source_name cannot be blank")
    return name


def _adapter_url(adapter: OfficialPriceAdapter, offering: Offering) -> str:
    # url_for is the protocol method.  The fallbacks keep existing vendor adapters
    # easy to integrate without weakening any transport checks.
    for attribute in ("url_for", "pricing_url", "build_url"):
        method = getattr(adapter, attribute, None)
        if callable(method):
            url = str(method(offering)).strip()
            if url:
                return url
    raise OfficialSourceValidationError("official adapter returned no pricing URL")


def _review_kind(reasons: Sequence[str]) -> str:
    text = " ".join(reasons).casefold()
    if any(token in text for token in ("price", "parse", "parser", "page", "html")):
        return "price"
    return "other"


def _failure_code(reasons: Sequence[str]) -> str:
    text = " ".join(reasons).casefold()
    if "403" in text or "forbidden" in text:
        return "access_forbidden"
    if "429" in text or "rate" in text and "limit" in text:
        return "rate_limited"
    if any(token in text for token in ("region", "地区", "geo")):
        return "region_restricted"
    if any(token in text for token in ("parse", "parser", "page", "selector")):
        return "page_changed"
    if "exceed" in text or "size" in text:
        return "response_too_large"
    return "collection_failed"


class OfficialPriceService:
    """Collect primary official evidence into reviewable staging records."""

    parser_version = "official-v1"

    def __init__(
        self,
        session: Session,
        *,
        data_source_id: int,
        client: OfficialHttpClient | None,
        adapters: Sequence[OfficialPriceAdapter],
        pricing_service_factory: Callable[[Session], PricingService] = PricingService,
        max_artifact_bytes: int = 2_000_000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if data_source_id <= 0:
            raise ValueError("data_source_id must be positive")
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.session = session
        self.data_source_id = data_source_id
        self.client = client
        self.adapters = tuple(adapters)
        self.pricing_service_factory = pricing_service_factory
        self.max_artifact_bytes = max_artifact_bytes
        self.clock = clock

    def _source(self) -> DataSource:
        source = self.session.get(DataSource, self.data_source_id)
        if source is None:
            raise OfficialSourceValidationError(
                f"unknown official data source: {self.data_source_id}"
            )
        self._validate_source(source)
        return source

    @staticmethod
    def _validate_source(source: DataSource) -> None:
        if source.source_type != "official" or source.authority_level != "primary":
            raise OfficialSourceValidationError(
                "official collection and publication require a primary official DataSource"
            )
        if not source.is_active:
            raise OfficialSourceValidationError("official DataSource is inactive")

    def missing_offerings(self, *, limit: int | None = None) -> list[Offering]:
        """Return active official-provider offerings without verified official prices."""
        self._source()
        if limit is not None and limit <= 0:
            return []
        has_official_price = exists(
            select(PriceSnapshot.id)
            .join(PricingPlan, PriceSnapshot.pricing_plan_id == PricingPlan.id)
            .join(DataSource, PriceSnapshot.data_source_id == DataSource.id)
            .where(
                PricingPlan.offering_id == Offering.id,
                PriceSnapshot.verification_status == "verified",
                DataSource.source_type == "official",
                DataSource.authority_level == "primary",
            )
        )
        statement = (
            select(Offering)
            .join(Provider, Offering.provider_id == Provider.id)
            .where(
                Offering.is_active.is_(True),
                Provider.is_active.is_(True),
                Provider.provider_type == "official",
                ~has_official_price,
            )
            .order_by(Offering.standard_model_id, Offering.provider_id, Offering.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    list_missing_official_prices = missing_offerings

    def refresh_offerings(self, *, limit: int | None = None) -> list[Offering]:
        """Prioritize missing prices, then the least recently refreshed offerings."""
        self._source()
        if limit is not None and limit <= 0:
            return []

        latest_verified = (
            select(
                PricingPlan.offering_id.label("offering_id"),
                func.max(PriceSnapshot.observed_at).label("verified_at"),
            )
            .join(
                PriceSnapshot,
                PriceSnapshot.pricing_plan_id == PricingPlan.id,
            )
            .join(DataSource, PriceSnapshot.data_source_id == DataSource.id)
            .where(
                PriceSnapshot.verification_status == "verified",
                DataSource.source_type == "official",
                DataSource.authority_level == "primary",
            )
            .group_by(PricingPlan.offering_id)
            .subquery()
        )
        latest_collection = (
            select(
                StagedQuote.matched_offering_id.label("offering_id"),
                func.max(SourceArtifact.fetched_at).label("collected_at"),
            )
            .join(ImportRow, StagedQuote.import_row_id == ImportRow.id)
            .join(ImportBatch, ImportRow.import_batch_id == ImportBatch.id)
            .join(
                SourceArtifact,
                ImportBatch.source_artifact_id == SourceArtifact.id,
            )
            .where(
                StagedQuote.matched_offering_id.is_not(None),
                SourceArtifact.data_source_id == self.data_source_id,
            )
            .group_by(StagedQuote.matched_offering_id)
            .subquery()
        )
        last_activity = func.coalesce(
            latest_collection.c.collected_at,
            latest_verified.c.verified_at,
        )
        statement = (
            select(Offering)
            .join(Provider, Offering.provider_id == Provider.id)
            .outerjoin(
                latest_verified,
                latest_verified.c.offering_id == Offering.id,
            )
            .outerjoin(
                latest_collection,
                latest_collection.c.offering_id == Offering.id,
            )
            .where(
                Offering.is_active.is_(True),
                Provider.is_active.is_(True),
                Provider.provider_type == "official",
            )
            .order_by(
                case((latest_verified.c.verified_at.is_(None), 0), else_=1),
                case((last_activity.is_(None), 0), else_=1),
                last_activity,
                Offering.standard_model_id,
                Offering.provider_id,
                Offering.id,
            )
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    list_refresh_offerings = refresh_offerings

    def run_once(self, *, limit: int | None = None) -> tuple[OfficialCollectionResult, ...]:
        """Refresh missing first, then existing prices, once per offering."""
        results: list[OfficialCollectionResult] = []
        for offering in self.refresh_offerings(limit=limit):
            try:
                adapter = self._find_adapter(offering)
                if adapter is None:
                    results.append(
                        OfficialCollectionResult(
                            offering_id=offering.id,
                            status="skipped",
                            review_reasons=("no official adapter supports this offering",),
                        )
                    )
                    continue
                results.append(self.collect_offering(offering.id, adapter=adapter))
            except Exception as exc:
                # A broken adapter must not prevent other vendors from being attempted.
                results.append(
                    OfficialCollectionResult(
                        offering_id=offering.id,
                        status="failed",
                        review_reasons=(
                            f"collector failed: {type(exc).__name__}: {exc}",
                        ),
                    )
                )
        return tuple(results)

    def _find_adapter(self, offering: Offering) -> OfficialPriceAdapter | None:
        for adapter in self.adapters:
            supports = getattr(adapter, "supports", None)
            if supports is None or bool(supports(offering)):
                return adapter
        return None

    def collect_offering(
        self,
        offering_id: int,
        *,
        adapter: OfficialPriceAdapter | None = None,
    ) -> OfficialCollectionResult:
        source = self._source()
        offering = self.session.get(Offering, offering_id)
        if offering is None:
            raise OfficialCatalogError(f"offering {offering_id} not found")
        if offering.provider.provider_type != "official":
            raise OfficialCatalogError("official collection requires an official provider")
        selected = adapter or self._find_adapter(offering)
        if selected is None:
            raise OfficialSourceValidationError("no official adapter supports this offering")
        if self.client is None:
            raise OfficialSourceValidationError(
                "official collection requires an HTTP client"
            )
        url = _adapter_url(selected, offering)
        try:
            fetched = self.client.fetch(url)
        except Exception as exc:
            now = self.clock()
            fetched = FetchResult(
                requested_url=url,
                source_url=url,
                fetched_at=now,
                status="pending_review",
                review_reasons=(f"HTTP client failed: {type(exc).__name__}: {exc}",),
            )
        return self._stage_fetch(source, offering, selected, fetched)

    collect = collect_offering

    def _stage_fetch(
        self,
        source: DataSource,
        offering: Offering,
        adapter: OfficialPriceAdapter,
        fetched: FetchResult,
    ) -> OfficialCollectionResult:
        reasons = list(fetched.review_reasons)
        body = fetched.raw_body
        if fetched.status == "fetched":
            if body is None:
                reasons.append("fetched response has no raw body")
            elif len(body) > self.max_artifact_bytes:
                reasons.append(f"response exceeds {self.max_artifact_bytes} bytes")
            else:
                actual_hash = hashlib.sha256(body).hexdigest()
                if fetched.raw_sha256 is not None and fetched.raw_sha256 != actual_hash:
                    reasons.append("raw body SHA-256 does not match transport metadata")

        candidates: tuple[Mapping[str, Any], ...] = ()
        if fetched.status == "fetched" and not reasons:
            try:
                parsed = adapter.parse(fetched)
                if isinstance(parsed, (str, bytes)):
                    raise TypeError("adapter result must be a sequence of objects")
                candidate_list = list(parsed)
                if not candidate_list:
                    reasons.append("official page changed or parser produced no candidates")
                elif not all(isinstance(item, Mapping) for item in candidate_list):
                    reasons.append("official parser returned a non-object candidate")
                else:
                    candidates = tuple(candidate_list)
            except Exception as exc:
                reasons.append(
                    f"official parser failed (possible page change): {type(exc).__name__}: {exc}"
                )
        elif fetched.status != "fetched" and not reasons:
            reasons.append(f"official fetch status is {fetched.status}")

        artifact_sha, metadata = self._artifact_payload(fetched, adapter, reasons)
        key = "official:" + _digest(
            {
                "source_id": source.id,
                "artifact_sha256": artifact_sha,
                "adapter": _adapter_name(adapter),
                "offering_id": offering.id,
                "parser_version": self.parser_version,
            }
        )

        with _transaction(self.session):
            artifact = self._get_or_create_artifact(
                source, fetched, artifact_sha, metadata
            )
            existing = self.session.scalar(
                select(ImportBatch).where(ImportBatch.idempotency_key == key)
            )
            if existing is not None:
                ids = tuple(
                    self.session.scalars(
                        select(StagedQuote.id)
                        .join(ImportRow, StagedQuote.import_row_id == ImportRow.id)
                        .where(ImportRow.import_batch_id == existing.id)
                        .order_by(StagedQuote.id)
                    )
                )
                return OfficialCollectionResult(
                    offering_id=offering.id,
                    status="reused",
                    source_artifact_id=artifact.id,
                    import_batch_id=existing.id,
                    staged_quote_ids=ids,
                    review_reasons=tuple(reasons),
                    reused_existing=True,
                )

            failed = bool(reasons)
            now = self.clock()
            batch = ImportBatch(
                source_artifact=artifact,
                provider_id=offering.provider_id,
                idempotency_key=key,
                parser_version=self.parser_version,
                status="processing",
                started_at=now,
                initiated_by="official-price-service",
                import_context={
                    "adapter": _adapter_name(adapter),
                    "offering_id": offering.id,
                    "requested_url": fetched.requested_url,
                    "source_url": fetched.source_url,
                    "transport_status": fetched.status,
                },
            )
            self.session.add(batch)
            self.session.flush()

            staged_ids: list[int] = []
            if failed:
                staged_ids.append(
                    self._stage_failure(batch, offering, fetched, tuple(reasons))
                )
                batch.total_rows = 1
                batch.review_rows = 1
                batch.status = "failed"
                batch.error_message = "; ".join(reasons)
                self._audit_failure(batch, offering, fetched, tuple(reasons))
            else:
                for index, candidate in enumerate(candidates, start=1):
                    staged_ids.append(
                        self._stage_candidate(batch, offering, fetched, candidate, index)
                    )
                batch.total_rows = len(candidates)
                batch.review_rows = len(candidates)
                batch.status = "partial"
            batch.completed_at = self.clock()
            self.session.flush()
            return OfficialCollectionResult(
                offering_id=offering.id,
                status=batch.status,
                source_artifact_id=artifact.id,
                import_batch_id=batch.id,
                staged_quote_ids=tuple(staged_ids),
                review_reasons=tuple(reasons),
            )

    def _artifact_payload(
        self,
        fetched: FetchResult,
        adapter: OfficialPriceAdapter,
        reasons: Sequence[str],
    ) -> tuple[str, dict[str, Any]]:
        body = fetched.raw_body
        if body is not None:
            sha = hashlib.sha256(body).hexdigest()
        else:
            sha = _digest(
                {
                    "requested_url": fetched.requested_url,
                    "source_url": fetched.source_url,
                    "status": fetched.status,
                    "review_reasons": reasons,
                }
            )
        retain_body = body is not None and len(body) <= self.max_artifact_bytes
        metadata = {
            "adapter": _adapter_name(adapter),
            "requested_url": fetched.requested_url,
            "source_url": fetched.source_url,
            "fetched_at": _json_safe(fetched.fetched_at),
            "transport_status": fetched.status,
            "content_type": fetched.content_type,
            "etag": fetched.etag,
            "last_modified": fetched.last_modified,
            "attempts": fetched.attempts,
            "byte_size": 0 if body is None else len(body),
            "raw_sha256": sha,
            "raw_body_encoding": "base64" if retain_body else None,
            "raw_body_base64": (
                base64.b64encode(body).decode("ascii") if retain_body else None
            ),
            "raw_body_omitted": body is not None and not retain_body,
            "review_reasons": list(reasons),
        }
        return sha, metadata

    def _get_or_create_artifact(
        self,
        source: DataSource,
        fetched: FetchResult,
        sha: str,
        metadata: dict[str, Any],
    ) -> SourceArtifact:
        existing = self.session.scalar(
            select(SourceArtifact).where(
                SourceArtifact.data_source_id == source.id,
                SourceArtifact.sha256 == sha,
            )
        )
        if existing is not None:
            return existing
        artifact = SourceArtifact(
            data_source=source,
            uri=fetched.source_url,
            media_type=fetched.content_type,
            sha256=sha,
            fetched_at=fetched.fetched_at,
            raw_metadata=metadata,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def _stage_candidate(
        self,
        batch: ImportBatch,
        offering: Offering,
        fetched: FetchResult,
        candidate: Mapping[str, Any],
        row_number: int,
    ) -> int:
        raw_candidate = _json_safe(candidate)
        quote = dict(raw_candidate)
        claimed_origin = quote.get("origin")
        quote["origin"] = "official"
        quote.setdefault("observed_at", _json_safe(fetched.fetched_at))
        quote["_official_provenance"] = {
            "source_url": fetched.source_url,
            "raw_sha256": fetched.raw_sha256,
            "claimed_origin": claimed_origin,
            "verification": "unverified",
        }
        raw_data = {
            "candidate": raw_candidate,
            "source_url": fetched.source_url,
            "fetched_at": _json_safe(fetched.fetched_at),
            "raw_sha256": fetched.raw_sha256,
        }
        row = ImportRow(
            import_batch=batch,
            sheet_name="official",
            row_number=row_number,
            row_hash=_digest(raw_data),
            raw_data=raw_data,
            status="staged",
        )
        staged = StagedQuote(
            import_row=row,
            provider_id=offering.provider_id,
            matched_standard_model_id=offering.standard_model_id,
            matched_model_version_id=offering.model_version_id,
            matched_offering_id=offering.id,
            extracted_model_name=offering.provider_model_name,
            extracted_plan_name=(
                str(quote["plan_name"]).strip() if quote.get("plan_name") else None
            ),
            match_method="exact",
            match_confidence=Decimal("1.0000"),
            review_status="needs_review",
            extracted_quote=quote,
            validation_errors=["official_candidate_requires_explicit_verification"],
        )
        staged.review_items.append(
            ReviewItem(
                review_type="price",
                status="open",
                severity="warning",
                reason="official candidate requires explicit verification before publication",
                proposed_resolution={
                    "action": "verify_official_candidate",
                    "source_url": fetched.source_url,
                },
            )
        )
        self.session.add(staged)
        self.session.flush()
        return staged.id

    def _stage_failure(
        self,
        batch: ImportBatch,
        offering: Offering,
        fetched: FetchResult,
        reasons: tuple[str, ...],
    ) -> int:
        code = _failure_code(reasons)
        raw_data = {
            "failure_code": code,
            "reasons": reasons,
            "requested_url": fetched.requested_url,
            "source_url": fetched.source_url,
            "fetched_at": _json_safe(fetched.fetched_at),
        }
        row = ImportRow(
            import_batch=batch,
            sheet_name="official",
            row_number=1,
            row_hash=_digest(raw_data),
            raw_data=_json_safe(raw_data),
            status="error",
            error_code=code,
            error_message="; ".join(reasons),
        )
        staged = StagedQuote(
            import_row=row,
            provider_id=offering.provider_id,
            matched_standard_model_id=offering.standard_model_id,
            matched_model_version_id=offering.model_version_id,
            matched_offering_id=offering.id,
            extracted_model_name=offering.provider_model_name,
            match_method="exact",
            match_confidence=Decimal("1.0000"),
            review_status="needs_review",
            extracted_quote={"origin": "official", "collection_error": raw_data},
            validation_errors=list(reasons),
        )
        staged.review_items.append(
            ReviewItem(
                review_type=_review_kind(reasons),
                status="open",
                severity="error",
                reason="; ".join(reasons),
                proposed_resolution={"action": "inspect_official_source", "code": code},
            )
        )
        self.session.add(staged)
        self.session.flush()
        return staged.id

    def _audit_failure(
        self,
        batch: ImportBatch,
        offering: Offering,
        fetched: FetchResult,
        reasons: tuple[str, ...],
    ) -> None:
        self.session.add(
            AuditEvent(
                actor="official-price-service",
                action="official_collection_failed",
                entity_type="import_batch",
                entity_id=str(batch.id),
                correlation_id=batch.idempotency_key,
                after_data={
                    "offering_id": offering.id,
                    "failure_code": _failure_code(reasons),
                    "requested_url": fetched.requested_url,
                    "source_url": fetched.source_url,
                    "reasons": list(reasons),
                },
            )
        )

    def publish_verified(
        self,
        staged_quote_id: int,
        *,
        verified: bool,
        verified_by: str,
        pricing_plan_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> PriceSnapshot:
        """Publish only after an explicit human/system verification decision."""
        if verified is not True:
            raise OfficialSourceValidationError("explicit verified=True is required")
        actor = str(verified_by).strip()
        if not actor:
            raise OfficialSourceValidationError("verified_by cannot be blank")

        with _transaction(self.session):
            staged = self.session.get(StagedQuote, staged_quote_id)
            if staged is None:
                raise OfficialCatalogError(f"staged quote {staged_quote_id} not found")
            artifact = staged.import_row.import_batch.source_artifact
            self._validate_source(artifact.data_source)
            offering = staged.matched_offering
            if (
                offering is None
                or staged.provider_id is None
                or staged.matched_standard_model_id is None
                or staged.provider_id != offering.provider_id
                or staged.matched_standard_model_id != offering.standard_model_id
                or staged.matched_model_version_id != offering.model_version_id
                or offering.provider.provider_type != "official"
            ):
                raise OfficialCatalogError(
                    "verified official candidate requires complete, consistent catalog links"
                )
            if pricing_plan_id is not None:
                plan = self.session.get(PricingPlan, pricing_plan_id)
                if plan is None or plan.offering_id != offering.id:
                    raise OfficialCatalogError("pricing plan is missing or belongs elsewhere")

            staged.review_status = "approved"
            staged.validation_errors = []
            resolved_at = self.clock()
            for review in staged.review_items:
                if review.status == "open":
                    review.status = "resolved"
                    review.resolution = {
                        "verified": True,
                        "verified_by": actor,
                        "source_artifact_id": artifact.id,
                    }
                    review.resolved_by = actor
                    review.resolved_at = resolved_at

            snapshot = self.pricing_service_factory(self.session).publish(
                staged.id,
                pricing_plan_id=pricing_plan_id,
                idempotency_key=idempotency_key,
            )
            self.session.add(
                AuditEvent(
                    actor=actor,
                    action="verify_official_price",
                    entity_type="price_snapshot",
                    entity_id=str(snapshot.id),
                    correlation_id=idempotency_key,
                    after_data={
                        "staged_quote_id": staged.id,
                        "source_artifact_id": artifact.id,
                        "offering_id": offering.id,
                        "verification_status": "verified",
                    },
                )
            )
            self.session.flush()
            return snapshot

    verify_and_publish = publish_verified
    publish = publish_verified
