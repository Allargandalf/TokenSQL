"""Acceptance tests for safe official-price collection and publication."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    DataSource,
    ImportBatch,
    Offering,
    PriceSnapshot,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.official.base import FetchResult
from tokensql.services.catalog_service import CatalogService
from tokensql.services.official_service import (
    OfficialPriceService,
    OfficialSourceValidationError,
)
from tokensql.services.pricing_service import PricingService


NOW = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


class FakeClient:
    def __init__(self, result: FetchResult):
        self.result = result
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        return self.result


class FakeAdapter:
    source_name = "example-official"

    def __init__(self, candidates=None, error: Exception | None = None):
        self.candidates = candidates or []
        self.error = error

    def supports(self, offering: Offering) -> bool:
        return offering.provider.slug == "official-provider"

    def url_for(self, offering: Offering) -> str:
        return f"https://official.example/pricing/{offering.api_model_id}"

    def parse(self, result: FetchResult):
        if self.error:
            raise self.error
        return self.candidates


def _catalog(session: Session, *, offerings: int = 1):
    catalog = CatalogService(session)
    manufacturer = catalog.create_manufacturer("Example AI")
    provider = catalog.create_provider(
        "Official Provider", provider_type="official", slug="official-provider"
    )
    metric = BillingMetric(
        code="official-input",
        display_name="Input token",
        dimension="token",
        direction="input",
        unit_name="token",
        unit_quantity=1_000_000,
        comparable_group="text_input",
    )
    session.add(metric)
    session.flush()
    values = []
    for index in range(offerings):
        model = catalog.create_model(
            manufacturer.id, f"model-{index}", display_name=f"Model {index}"
        )
        offering = catalog.create_offering(
            provider.id, model.id, f"model-{index}", api_model_id=f"model-{index}"
        )
        plan = catalog.create_plan(
            offering.id,
            "standard",
            billing_mode="pay_as_you_go",
            currency_code="USD",
            effective_from=NOW,
        )
        values.append((offering, plan))
    source = DataSource(
        name="official-docs",
        source_type="official",
        authority_level="primary",
        base_url="https://official.example/",
    )
    session.add(source)
    session.flush()
    return source, metric, values


def _candidate(metric: BillingMetric):
    return {
        "plan_name": "standard",
        "currency_code": "USD",
        "observed_at": NOW.isoformat(),
        "effective_from": NOW.isoformat(),
        "components": [
            {
                "billing_metric_id": metric.id,
                "component_type": "monetary",
                "amount": "2.50",
                "currency": "USD",
                "unit_name": "token",
                "unit_quantity": "1000000",
                "conditions": {},
            }
        ],
    }


def _fetch(body: bytes = b'{"pricing":true}') -> FetchResult:
    return FetchResult(
        requested_url="https://official.example/pricing/model-0",
        source_url="https://official.example/pricing/model-0",
        fetched_at=NOW,
        status="fetched",
        raw_sha256=hashlib.sha256(body).hexdigest(),
        raw_body=body,
        content_type="application/json",
        etag='"v1"',
        last_modified="Thu, 24 Sep 2026 03:00:00 GMT",
        attempts=1,
    )


def _service(session, source, metric, *, fetched=None, adapter=None, **changes):
    values = {
        "data_source_id": source.id,
        "client": FakeClient(fetched or _fetch()),
        "adapters": [adapter or FakeAdapter([_candidate(metric)])],
        "clock": lambda: NOW,
    }
    values.update(changes)
    return OfficialPriceService(session, **values)


def test_collection_is_idempotent_and_only_stages_raw_official_evidence(session):
    source, metric, ((offering, _),) = _catalog(session)
    service = _service(session, source, metric)

    first = service.collect_offering(offering.id)
    second = service.collect_offering(offering.id)

    assert first.status == "partial"
    assert second.reused_existing
    assert first.import_batch_id == second.import_batch_id
    assert session.scalar(select(func.count()).select_from(SourceArtifact)) == 1
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 1
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0
    artifact = session.get(SourceArtifact, first.source_artifact_id)
    assert artifact.uri.startswith("https://official.example/")
    assert artifact.raw_metadata["etag"] == '"v1"'
    assert artifact.raw_metadata["last_modified"].startswith("Thu")
    assert artifact.raw_metadata["raw_sha256"] == artifact.sha256
    assert artifact.raw_metadata["raw_body_base64"]
    staged = session.get(StagedQuote, first.staged_quote_ids[0])
    assert staged.review_status == "needs_review"
    assert staged.validation_errors
    assert staged.extracted_quote["origin"] == "official"
    assert len(staged.review_items) == 1


def test_explicit_verification_is_required_before_pricing_publication(session):
    source, metric, ((offering, plan),) = _catalog(session)
    service = _service(session, source, metric)
    collected = service.collect_offering(offering.id)
    staged_id = collected.staged_quote_ids[0]

    with pytest.raises(OfficialSourceValidationError, match="verified=True"):
        service.publish_verified(staged_id, verified=False, verified_by="alice")
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0

    snapshot = service.publish_verified(
        staged_id,
        verified=True,
        verified_by="alice",
        pricing_plan_id=plan.id,
        idempotency_key="official-model-0-v1",
    )
    assert snapshot.verification_status == "verified"
    assert snapshot.pricing_plan_id == plan.id
    assert snapshot.data_source_id == source.id
    assert session.get(StagedQuote, staged_id).review_status == "approved"
    assert all(
        item.status == "resolved"
        for item in session.scalars(select(ReviewItem)).all()
    )


def test_parser_page_change_and_transport_failures_create_review_and_audit(session):
    source, metric, ((offering, _),) = _catalog(session)
    service = _service(
        session,
        source,
        metric,
        adapter=FakeAdapter(error=ValueError("selector missing")),
    )
    parsed = service.collect_offering(offering.id)
    assert parsed.status == "failed"
    assert "page change" in parsed.review_reasons[0]
    assert session.scalar(select(AuditEvent)).action == "official_collection_failed"
    assert session.scalar(select(ReviewItem)).severity == "error"

    body = b""
    limited = FetchResult(
        requested_url="https://official.example/pricing/model-0?retry=1",
        source_url="https://official.example/pricing/model-0?retry=1",
        fetched_at=NOW,
        status="pending_review",
        raw_body=body,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        review_reasons=("unexpected HTTP status 429; region restricted",),
        attempts=3,
    )
    second = _service(session, source, metric, fetched=limited).collect_offering(
        offering.id
    )
    row = session.get(StagedQuote, second.staged_quote_ids[0]).import_row
    assert row.error_code == "rate_limited"
    assert second.status == "failed"


def test_missing_price_priority_and_run_once(session):
    source, metric, values = _catalog(session, offerings=2)
    service = _service(session, source, metric)
    first_offering, first_plan = values[0]
    second_offering, _ = values[1]

    staged_id = service.collect_offering(first_offering.id).staged_quote_ids[0]
    service.publish_verified(
        staged_id,
        verified=True,
        verified_by="reviewer",
        pricing_plan_id=first_plan.id,
    )

    assert [item.id for item in service.missing_offerings()] == [second_offering.id]
    assert [item.id for item in service.refresh_offerings()] == [
        second_offering.id,
        first_offering.id,
    ]
    result = service.run_once(limit=1)
    assert [item.offering_id for item in result] == [second_offering.id]
    assert result[0].status == "partial"


def test_run_once_refreshes_existing_price_then_explicit_publish_keeps_history(session):
    source, metric, ((offering, plan),) = _catalog(session)
    adapter = FakeAdapter([_candidate(metric)])
    client = FakeClient(_fetch(b'{"pricing":"v1"}'))
    service = OfficialPriceService(
        session,
        data_source_id=source.id,
        client=client,
        adapters=(adapter,),
        clock=lambda: NOW,
    )

    old_staged_id = service.collect_offering(offering.id).staged_quote_ids[0]
    old_snapshot = service.publish_verified(
        old_staged_id,
        verified=True,
        verified_by="reviewer",
        pricing_plan_id=plan.id,
        idempotency_key="official-v1",
    )

    later = NOW + timedelta(days=1)
    new_body = b'{"pricing":"v2"}'
    client.result = FetchResult(
        requested_url="https://official.example/pricing/model-0",
        source_url="https://official.example/pricing/model-0",
        fetched_at=later,
        status="fetched",
        raw_sha256=hashlib.sha256(new_body).hexdigest(),
        raw_body=new_body,
        content_type="application/json",
        etag='"v2"',
        last_modified="Fri, 25 Sep 2026 03:00:00 GMT",
        attempts=1,
    )
    updated_candidate = _candidate(metric)
    updated_candidate["observed_at"] = later.isoformat()
    updated_candidate["effective_from"] = later.isoformat()
    updated_candidate["components"][0]["amount"] = "3.75"
    adapter.candidates = [updated_candidate]

    (refresh,) = service.run_once(limit=1)

    assert refresh.offering_id == offering.id
    assert refresh.status == "partial"
    assert not refresh.reused_existing
    refreshed = session.get(StagedQuote, refresh.staged_quote_ids[0])
    assert refreshed.review_status == "needs_review"
    assert refreshed.extracted_quote["components"][0]["amount"] == "3.75"
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(SourceArtifact)) == 2

    new_snapshot = service.publish_verified(
        refreshed.id,
        verified=True,
        verified_by="reviewer",
        pricing_plan_id=plan.id,
        idempotency_key="official-v2",
    )
    history = PricingService(session).history(plan.id)
    assert [item.id for item in history] == [old_snapshot.id, new_snapshot.id]
    assert new_snapshot.supersedes_snapshot_id == old_snapshot.id
    assert history[0].components[0].raw_amount == old_snapshot.components[0].raw_amount
    assert history[1].components[0].raw_amount != history[0].components[0].raw_amount


def test_non_primary_or_non_official_sources_are_rejected(session):
    source, metric, ((offering, _),) = _catalog(session)
    service = _service(session, source, metric)
    staged_id = service.collect_offering(offering.id).staged_quote_ids[0]

    source.authority_level = "secondary"
    session.flush()
    with pytest.raises(OfficialSourceValidationError, match="primary official"):
        service.missing_offerings()
    with pytest.raises(OfficialSourceValidationError, match="primary official"):
        service.publish_verified(
            staged_id, verified=True, verified_by="reviewer"
        )
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0


def test_oversize_body_is_not_retained_and_goes_to_review(session):
    source, metric, ((offering, _),) = _catalog(session)
    body = b"123456"
    result = _service(
        session, source, metric, fetched=_fetch(body), max_artifact_bytes=5
    ).collect_offering(offering.id)
    artifact = session.get(SourceArtifact, result.source_artifact_id)
    assert result.status == "failed"
    assert artifact.raw_metadata["raw_body_omitted"] is True
    assert artifact.raw_metadata["raw_body_base64"] is None
