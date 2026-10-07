"""Persistent FX and normalization trust-boundary tests."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    BillingMetric,
    DataSource,
    FXRate,
    PriceNormalization,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.pricing import FXQuote
from tokensql.services.pricing_service import (
    PricingNotFoundError,
    PricingService,
    PricingValidationError,
)
from tests.test_catalog_services import NOW, _catalog, _metric, _payload, _staged


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


def _fx_source(session: Session, suffix: str = "main") -> DataSource:
    source = DataSource(
        name=f"fx-source-{suffix}",
        source_type="official",
        authority_level="primary",
        base_url=f"https://fx-{suffix}.example/rates",
        is_active=True,
        attributes={},
    )
    session.add(source)
    session.flush()
    return source


def _component(
    session: Session,
    *,
    currency: str = "CNY",
    amount: str = "10",
    unit_name: str = "token",
    unit_quantity: str = "1000000",
    conditions: dict[str, object] | None = None,
    suffix: str = "p",
):
    _, _, provider, offering, _ = _catalog(session)
    if unit_name == "token":
        metric = _metric(session, f"persist-{suffix}", "input")
    else:
        metric = BillingMetric(
            code=f"persist-{suffix}",
            display_name=f"persist-{suffix}",
            dimension="time",
            direction=None,
            unit_name="second",
            unit_quantity=1,
            comparable_group="audio_time",
            attributes={},
        )
        session.add(metric)
        session.flush()
    payload = _payload(metric)
    payload["currency_code"] = currency
    payload["components"][0].update(
        amount=amount,
        currency=currency,
        unit_name=unit_name,
        unit_quantity=unit_quantity,
        conditions=conditions if conditions is not None else {"cache": "miss"},
    )
    staged = _staged(session, provider, offering, payload, suffix=suffix)
    return PricingService(session).publish(staged.id).components[0]


def test_verified_fx_normalization_is_persisted_versioned_and_idempotent(session):
    service = PricingService(session)
    component = _component(session, currency="USD", suffix="u")
    source = _fx_source(session)

    fx = service.create_fx_rate(
        base_currency="USD",
        quote_currency="CNY",
        rate=Decimal("7.2"),
        rate_date=NOW.date(),
        data_source_id=source.id,
        verification_status="verified",
        observed_at=NOW,
    )
    assert service.create_fx_rate(
        base_currency="usd",
        quote_currency="cny",
        rate="7.2",
        rate_date=NOW.date(),
        data_source_id=source.id,
        verification_status="verified",
        observed_at=NOW + timedelta(hours=1),
    ).id == fx.id

    first = service.store_normalization(component.id, fx_rate_id=fx.id)
    repeated = service.normalize_component(component, fx_rate=fx)
    assert repeated.id == first.id
    assert first.normalized_amount == Decimal("72")
    assert first.normalized_currency_code == "CNY"
    assert first.normalized_unit_name == "token"
    assert first.normalized_unit_quantity == 1_000_000
    assert first.fx_rate_id == fx.id
    assert first.verification_status == "verified"
    assert len(first.comparison_key) == 64

    next_algorithm = service.store_normalization(
        component,
        fx_rate_id=fx.id,
        algorithm_version="tokensql-normalization-v2",
    )
    revised_fx = service.create_fx_rate(
        base_currency="USD",
        quote_currency="CNY",
        rate="7.3",
        rate_date=NOW.date(),
        data_source_id=source.id,
        verification_status="verified",
        observed_at=NOW + timedelta(hours=2),
    )
    next_fx = service.store_normalization(component, fx_rate_id=revised_fx.id)
    assert len({first.id, next_algorithm.id, next_fx.id}) == 3
    assert next_fx.normalized_amount == Decimal("73")
    assert len(session.scalars(select(PriceNormalization)).all()) == 3


def test_fx_creation_validates_decimal_source_status_and_appends_status(session):
    service = PricingService(session)
    source = _fx_source(session, "validation")
    pending = service.create_fx_rate(
        base_currency="USD",
        quote_currency="CNY",
        rate="7.2",
        rate_date=NOW.date(),
        data_source_id=source.id,
    )
    verified = service.create_fx_rate(
        base_currency="USD",
        quote_currency="CNY",
        rate="7.2",
        rate_date=NOW.date(),
        data_source_id=source.id,
        verification_status="verified",
    )
    assert pending.id != verified.id

    with pytest.raises(PricingValidationError, match="must not be a float"):
        service.create_fx_rate(
            base_currency="USD", quote_currency="CNY", rate=7.2,
            rate_date=NOW.date(), data_source_id=source.id,
        )
    with pytest.raises(PricingValidationError, match="verification status"):
        service.create_fx_rate(
            base_currency="USD", quote_currency="CNY", rate="7.2",
            rate_date=NOW.date(), data_source_id=source.id,
            verification_status="approved",
        )
    with pytest.raises(LookupError, match="data source"):
        service.create_fx_rate(
            base_currency="USD", quote_currency="CNY", rate="7.2",
            rate_date=NOW.date(), data_source_id=999999,
        )


def test_foreign_normalization_requires_verified_matching_dated_fx(session):
    service = PricingService(session)
    component = _component(session, currency="USD", suffix="d")
    source = _fx_source(session, "dates")
    pending = service.create_fx_rate(
        base_currency="USD", quote_currency="CNY", rate="7.2",
        rate_date=NOW.date(), data_source_id=source.id,
    )
    wrong_pair = service.create_fx_rate(
        base_currency="EUR", quote_currency="CNY", rate="8",
        rate_date=NOW.date(), data_source_id=source.id,
        verification_status="verified",
    )
    wrong_date = service.create_fx_rate(
        base_currency="USD", quote_currency="CNY", rate="7.2",
        rate_date=NOW.date() - timedelta(days=1), data_source_id=source.id,
        verification_status="verified",
    )

    with pytest.raises(PricingValidationError, match="required"):
        service.store_normalization(component)
    with pytest.raises(PricingValidationError, match="must be verified"):
        service.store_normalization(component, fx_rate_id=pending.id)
    with pytest.raises(PricingValidationError, match="cannot convert"):
        service.store_normalization(component, fx_rate_id=wrong_pair.id)
    with pytest.raises(PricingValidationError, match="does not match"):
        service.store_normalization(component, fx_rate_id=wrong_date.id)


def test_non_token_normalization_and_condition_sensitive_persisted_compare(session):
    service = PricingService(session)
    seconds = _component(
        session,
        amount="12",
        unit_name="second",
        unit_quantity="60",
        conditions={},
        suffix="s",
    )
    normalized_seconds = service.store_normalization(seconds)
    assert normalized_seconds.normalized_amount == Decimal("0.2")
    assert normalized_seconds.normalized_unit_name == "second"
    assert normalized_seconds.normalized_unit_quantity == 1
    assert normalized_seconds.fx_rate_id is None

    left = _component(session, conditions={"cache": "miss"}, suffix="l")
    right = _component(session, conditions={"cache": "hit"}, suffix="r")
    left_record = service.store_normalization(left)
    right_record = service.store_normalization(right)
    result = service.compare(
        left,
        right,
        left_normalization_id=left_record.id,
        right_normalization_id=right_record.id,
    )
    assert not result.comparable
    assert "comparison_key differs" in result.reasons
    assert any(item.field == "conditions" for item in result.differences)


def test_compare_requires_verified_persisted_normalizations(session):
    service = PricingService(session)
    left = _component(session, suffix="sl")
    right = _component(session, suffix="tr")

    with pytest.raises(PricingNotFoundError, match="verified persisted"):
        service.compare(left, right)

    service.store_normalization(left)
    with pytest.raises(PricingNotFoundError, match="missing-algorithm"):
        service.compare(left, right, algorithm_version="missing-algorithm")

    service.store_normalization(right)
    assert service.compare(left, right).comparable

    with pytest.raises(PricingValidationError, match="require verified persisted"):
        service.compare(left, right, use_persisted=False)

    with pytest.raises(PricingNotFoundError, match="price normalization 999999"):
        service.compare(
            left,
            right,
            left_normalization_id=999999,
        )


def test_discount_component_cannot_be_persistently_normalized(session):
    service = PricingService(session)
    _, _, provider, offering, _ = _catalog(session)
    metric = _metric(session, "discount-only", "input")
    payload = _payload(metric)
    payload["components"] = [{
        "billing_metric_id": metric.id,
        "component_type": "discount",
        "discount_rate": "0.8",
        "conditions": {"cache": "miss"},
    }]
    staged = _staged(session, provider, offering, payload, suffix="z")
    component = service.publish(staged.id).components[0]

    with pytest.raises(PricingValidationError, match="only monetary"):
        service.store_normalization(component)


def test_compare_rejects_unpersisted_fx_and_append_only_guards_hold(session):
    service = PricingService(session)
    component = _component(session, currency="USD", suffix="a")
    source = _fx_source(session, "append")
    fx = service.create_fx_rate(
        base_currency="USD", quote_currency="CNY", rate="7.2",
        rate_date=NOW.date(), data_source_id=source.id,
        verification_status="verified",
    )
    record = service.store_normalization(component, fx_rate_id=fx.id)
    assert service.compare(component, component).comparable

    forged = FXQuote(
        base_currency="USD", quote_currency="CNY", rate=Decimal("100"),
        rate_date=date(2026, 9, 23), source="caller", verification_status="verified",
    )
    with pytest.raises(PricingValidationError, match="not trusted"):
        service.compare(component, component, left_fx=forged, right_fx=forged)

    session.commit()
    fx.rate = Decimal("9")
    with pytest.raises(ValueError, match="FXRate is append-only"):
        session.flush()
    session.rollback()
    record = session.get(PriceNormalization, record.id)
    record.formula = "rewritten"
    with pytest.raises(ValueError, match="PriceNormalization is append-only"):
        session.flush()
    session.rollback()
