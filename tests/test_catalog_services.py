"""Acceptance tests for catalog, review, and publication services."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AliasRule, AuditEvent, BillingMetric, DataSource, ImportBatch, ImportRow,
    ModelAlias, PriceComponent, PriceSnapshot, ReviewItem, SourceArtifact,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.services.catalog_service import CatalogConflictError, CatalogService
from tokensql.services.pricing_service import (
    DuplicatePublicationError, PricingService, PricingValidationError,
)
from tokensql.services.review_service import (
    AliasRuleConflictError, ReviewService, ReviewStateError,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 23, 8, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


def _catalog(session: Session, *, provider_type: str = "reseller"):
    service = CatalogService(session)
    manufacturer = service.create_manufacturer("  Example   AI ")
    model = service.create_model(manufacturer.id, "Model-X", display_name="Model X")
    version = service.create_version(model.id, "2026-09")
    provider = service.create_provider(
        f"{provider_type} Provider", provider_type=provider_type)
    offering = service.create_offering(
        provider.id, model.id, "model-x-api", model_version_id=version.id)
    plan = service.create_plan(
        offering.id, "standard", billing_mode="pay_as_you_go",
        currency_code="CNY", effective_from=NOW)
    return service, model, provider, offering, plan


def _staged(
    session: Session, provider, offering, payload, *,
    source_type: str = "provider_excel", authority: str = "secondary",
    review_status: str = "approved", suffix: str = "a",
):
    source = DataSource(
        name=f"source-{suffix}", source_type=source_type,
        authority_level=authority)
    artifact = SourceArtifact(
        data_source=source, uri=f"memory://{suffix}",
        sha256=suffix[0] * 64, fetched_at=NOW)
    batch = ImportBatch(
        source_artifact=artifact, provider=provider,
        idempotency_key=f"batch-{suffix}", parser_version="test",
        total_rows=1)
    row = ImportRow(
        import_batch=batch, sheet_name="Prices", row_number=1,
        row_hash=(suffix[-1] if suffix[-1].isalnum() else "b") * 64,
        raw_data={"model": offering.provider_model_name}, status="staged")
    staged = StagedQuote(
        import_row=row, provider=provider,
        matched_standard_model_id=offering.standard_model_id,
        matched_model_version_id=offering.model_version_id,
        matched_offering=offering,
        extracted_model_name=offering.provider_model_name,
        extracted_plan_name="standard", match_method="manual",
        review_status=review_status, extracted_quote=payload,
        validation_errors=[])
    session.add(staged)
    session.flush()
    return staged


def _metric(session: Session, code: str, direction: str):
    value = BillingMetric(
        code=code, display_name=code, dimension="token", direction=direction,
        unit_name="token", unit_quantity=1_000_000,
        comparable_group=f"text_{direction}")
    session.add(value)
    session.flush()
    return value


def _payload(input_metric, output_metric=None, **changes):
    components = [{
        "billing_metric_id": input_metric.id,
        "component_type": "monetary", "amount": "10",
        "currency": "CNY", "unit_name": "token",
        "unit_quantity": "1000000", "conditions": {"cache": "miss"},
    }]
    if output_metric is not None:
        components.append({
            "billing_metric_id": output_metric.id,
            "component_type": "monetary", "amount": "20",
            "currency": "CNY", "unit_name": "token",
            "unit_quantity": "1000000", "conditions": {"cache": "miss"},
        })
    value = {
        "plan_name": "standard", "currency_code": "CNY",
        "observed_at": NOW.isoformat(), "effective_from": NOW.isoformat(),
        "components": components,
    }
    value.update(changes)
    return value


def test_catalog_supports_same_model_multiple_providers_and_plans(session):
    service, model, first, offering, plan = _catalog(session)
    second = service.create_provider("Second Provider", provider_type="aggregator")
    second_offering = service.create_offering(second.id, model.id, "other-api")
    committed = service.create_plan(
        offering.id, "committed", billing_mode="committed",
        currency_code="CNY", effective_from=NOW)

    assert {item.provider_id for item in service.list_offerings(
        standard_model_id=model.id)} == {first.id, second.id}
    assert {item.id for item in service.list_plans(offering.id)} == {
        plan.id, committed.id}
    assert second_offering.standard_model_id == model.id


def test_only_verified_aliases_match_and_alias_conflicts_are_safe(session):
    service, model, provider, _, _ = _catalog(session)
    pending = service.create_alias("Secret Alias", model.id, provider_id=provider.id)
    assert not pending.is_verified
    assert service.match_model(
        "Secret Alias", provider_id=provider.id).result.review_required

    service.create_alias(
        "Secret Alias", model.id, provider_id=provider.id, human_confirmed=True)
    assert service.match_model(
        "Secret Alias", provider_id=provider.id).standard_model_id == model.id

    other = service.create_model(
        model.manufacturer_id, "other-model", display_name="Other")
    with pytest.raises(CatalogConflictError):
        service.create_alias(
            " secret--alias ", other.id, provider_id=provider.id, verified=True)


def test_review_approval_creates_rule_and_append_only_audit(session):
    _, model, provider, offering, _ = _catalog(session)
    input_metric = _metric(session, "input", "input")
    staged = _staged(session, provider, offering, _payload(input_metric))
    staged.review_status = "needs_review"
    review = ReviewItem(
        staged_quote=staged, review_type="model_match", severity="warning",
        reason="confirm alias")
    session.add(review)
    session.flush()

    result = ReviewService(session).approve(
        review.id, reviewer="alice",
        resolution={
            "standard_model_id": model.id, "alias": "MODEL X special",
            "create_alias_rule": True,
        })

    assert result.status == "resolved"
    assert result.resolved_by == "alice"
    assert result.resolved_at is not None
    assert staged.review_status == "approved"
    rule = session.scalar(select(AliasRule))
    assert rule.pattern == "model x special"
    audit = session.scalar(select(AuditEvent))
    assert audit.action == "approve"
    assert audit.after_data["status"] == "resolved"
    audit.action = "rewrite"
    with pytest.raises(ValueError, match="append-only"):
        session.flush()
    session.rollback()


def test_alias_rule_conflict_leaves_review_open(session):
    service, model, provider, offering, _ = _catalog(session)
    other = service.create_model(model.manufacturer_id, "other", display_name="Other")
    first_staged = _staged(
        session, provider, offering, {"currency_code": "CNY"}, suffix="c")
    first_staged.review_status = "needs_review"
    first = ReviewItem(
        staged_quote=first_staged, review_type="model_match",
        reason="first", severity="warning")
    session.add(first)
    session.flush()
    ReviewService(session).approve(
        first.id, reviewer="alice",
        resolution={"standard_model_id": model.id, "alias": "shared",
                    "create_alias_rule": True})

    second_staged = _staged(
        session, provider, offering, {"currency_code": "CNY"}, suffix="d")
    second_staged.review_status = "needs_review"
    second = ReviewItem(
        staged_quote=second_staged, review_type="model_match",
        reason="second", severity="warning")
    session.add(second)
    session.flush()
    with pytest.raises(AliasRuleConflictError):
        ReviewService(session).approve(
            second.id, reviewer="bob",
            resolution={"standard_model_id": other.id, "alias": "shared",
                        "create_alias_rule": True})
    assert second.status == "open"
    assert second.resolved_at is None


def test_publish_multiple_components_is_idempotent_and_preserves_history(session):
    _, _, provider, offering, plan = _catalog(session)
    input_metric = _metric(session, "input", "input")
    output_metric = _metric(session, "output", "output")
    staged = _staged(
        session, provider, offering, _payload(input_metric, output_metric))
    service = PricingService(session)

    first = service.publish(staged.id, idempotency_key="one")
    assert len(first.components) == 2
    assert first.publication_idempotency_key == "one"
    with pytest.raises(DuplicatePublicationError):
        service.publish(staged.id, idempotency_key="one")
    assert session.is_active
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1

    later_payload = _payload(
        input_metric, output_metric,
        observed_at=(NOW + timedelta(days=1)).isoformat(),
        effective_from=(NOW + timedelta(days=1)).isoformat())
    later = _staged(
        session, provider, offering, later_payload, suffix="e")
    second = service.publish(later.id, idempotency_key="two")
    assert second.supersedes_snapshot_id == first.id
    assert [item.id for item in service.history(plan.id)] == [first.id, second.id]
    assert service.current(
        plan.id, as_of=NOW + timedelta(days=2)).id == second.id
    assert first.raw_payload["_publication"]["idempotency_key"] == "one"
    assert "snapshot_id" in service.export_csv(service.history(plan.id))


def test_component_validation_failure_closes_savepoint_and_session_remains_usable(session):
    _, _, provider, offering, _ = _catalog(session)
    input_metric = _metric(session, "savepoint-input", "input")
    output_metric = _metric(session, "savepoint-output", "output")
    bad_payload = _payload(input_metric, output_metric)
    bad_payload["components"][1]["currency"] = "ZZZ"
    bad = _staged(
        session, provider, offering, bad_payload, suffix="savepoint-bad"
    )
    service = PricingService(session)

    with pytest.raises(PricingValidationError, match="unknown currency"):
        service.publish(bad.id, idempotency_key="invalid-component")

    assert not session.in_nested_transaction()
    assert session.is_active
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(PriceComponent)) == 0

    good = _staged(
        session, provider, offering, _payload(input_metric, output_metric),
        suffix="savepoint-good",
    )
    published = service.publish(good.id, idempotency_key="valid-after-failure")
    assert published.id is not None
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1


    _, _, provider, offering, _ = _catalog(session, provider_type="reseller")
    metric = _metric(session, "official-input", "input")
    payload = _payload(metric, origin="official")
    staged = _staged(
        session, provider, offering, payload,
        source_type="provider_excel", authority="secondary", suffix="f")
    with pytest.raises(PricingValidationError, match="official origin"):
        PricingService(session).publish(staged.id)


@pytest.mark.parametrize("mutation, match", [
    (lambda c: c.update(amount="1", discount_rate="0.8"), "exactly one"),
    (lambda c: c.update(currency="ZZZ"), "unknown currency"),
    (lambda c: c.update(unit_name="mystery"), "unknown unit"),
    (lambda c: c.update(conditions={"mystery": True}), "unknown pricing condition"),
])
def test_unknown_or_ambiguous_components_are_not_published(session, mutation, match):
    _, _, provider, offering, _ = _catalog(session)
    metric = _metric(session, f"metric-{match[:3]}", "input")
    payload = _payload(metric)
    mutation(payload["components"][0])
    staged = _staged(
        session, provider, offering, payload,
        suffix=chr(103 + len(match) % 10))
    with pytest.raises(PricingValidationError, match=match):
        PricingService(session).publish(staged.id)
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0


def test_different_conditions_and_units_are_not_comparable(session):
    _, _, provider, offering, _ = _catalog(session)
    metric = _metric(session, "compare-input", "input")
    first = _staged(session, provider, offering, _payload(metric), suffix="q")
    first_snapshot = PricingService(session).publish(first.id, idempotency_key="q")
    payload = _payload(metric)
    payload["components"][0]["conditions"] = {"cache": "hit"}
    second = _staged(session, provider, offering, payload, suffix="r")
    second_snapshot = PricingService(session).publish(second.id, idempotency_key="r")
    service = PricingService(session)
    service.store_normalization(first_snapshot.components[0])
    service.store_normalization(second_snapshot.components[0])
    comparison = service.compare(
        first_snapshot.components[0], second_snapshot.components[0])
    assert not comparison.comparable
    assert "comparison_key differs" in comparison.reasons



@pytest.mark.parametrize("order", [("reject", "approve"), ("approve", "reject")])
def test_review_rejection_is_irreversible_across_resolution_order(session, order):
    _, _, provider, offering, _ = _catalog(session)
    metric = _metric(session, "ordered-" + order[0], "input")
    staged = _staged(
        session, provider, offering, _payload(metric), suffix="o" + order[0][0]
    )
    staged.review_status = "needs_review"
    staged.validation_errors = ["currency_missing", "unit_missing"]
    reviews = [
        ReviewItem(
            staged_quote=staged, review_type="currency", severity="warning",
            reason="currency_missing",
        ),
        ReviewItem(
            staged_quote=staged, review_type="unit", severity="warning",
            reason="unit_missing",
        ),
    ]
    session.add_all(reviews)
    session.flush()
    service = ReviewService(session)

    for action, item in zip(order, reviews):
        if action == "reject":
            service.reject(item.id, reviewer="reviewer", resolution={"reason": "invalid"})
        else:
            service.approve(item.id, reviewer="reviewer", resolution={})

    assert staged.review_status == "rejected"
    assert staged.import_row.status == "rejected"
    assert {item.status for item in reviews} == {"dismissed", "resolved"}
    with pytest.raises(PricingValidationError, match="approved"):
        PricingService(session).publish(staged.id)


def test_all_reviews_resolved_clear_validation_errors_and_enable_publication(session):
    _, _, provider, offering, _ = _catalog(session)
    metric = _metric(session, "all-approved", "input")
    staged = _staged(session, provider, offering, _payload(metric), suffix="z")
    staged.review_status = "needs_review"
    staged.validation_errors = ["currency_missing", "unit_missing"]
    reviews = [
        ReviewItem(
            staged_quote=staged, review_type="currency", severity="warning",
            reason="currency_missing",
        ),
        ReviewItem(
            staged_quote=staged, review_type="unit", severity="warning",
            reason="unit_missing",
        ),
    ]
    session.add_all(reviews)
    session.flush()
    service = ReviewService(session)

    service.approve(reviews[0].id, reviewer="alice", resolution={})
    assert staged.review_status == "needs_review"
    assert staged.validation_errors == ["unit_missing"]
    service.approve(reviews[1].id, reviewer="bob", resolution={})

    assert staged.review_status == "approved"
    assert staged.import_row.status == "accepted"
    assert staged.validation_errors == []
    assert PricingService(session).publish(staged.id).id is not None


def test_mismatched_offering_resolution_is_audited_and_remains_open(session):
    catalog, model, provider, offering, _ = _catalog(session)
    metric = _metric(session, "bad-offering", "input")
    staged = _staged(session, provider, offering, _payload(metric), suffix="y")
    staged.review_status = "needs_review"
    review = ReviewItem(
        staged_quote=staged, review_type="model_match", severity="warning",
        reason="offering mismatch",
    )
    session.add(review)
    session.flush()
    other_provider = catalog.create_provider(
        "Unrelated Provider", provider_type="aggregator"
    )
    wrong_offering = catalog.create_offering(
        other_provider.id, model.id, "same-model-wrong-provider",
        model_version_id=offering.model_version_id,
    )

    with pytest.raises(ReviewStateError, match="another provider"):
        ReviewService(session).approve(
            review.id, reviewer="security-reviewer",
            resolution={"offering_id": wrong_offering.id},
        )

    assert review.status == "open"
    assert review.resolved_at is None
    assert staged.matched_offering_id == offering.id
    audit = session.scalar(select(AuditEvent).where(
        AuditEvent.action == "approve_rejected"
    ))
    assert audit is not None
    assert "another provider" in audit.after_data["error"]
