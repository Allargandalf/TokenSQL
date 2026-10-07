"""Database model and migration acceptance tests."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AliasRule,
    AuditEvent,
    BillingMetric,
    DataSource,
    FXRate,
    ImportBatch,
    ImportRow,
    Manufacturer,
    ModelVersion,
    Offering,
    PriceComponent,
    PriceSnapshot,
    PricingPlan,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
    StandardModel,
)
from tokensql.db.session import (
    create_db_engine,
    create_session_factory,
    session_scope,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    db_engine = create_db_engine(
        "sqlite+pysqlite:///:memory:", sqlite_busy_timeout_ms=2_345
    )
    Base.metadata.create_all(db_engine)
    try:
        yield db_engine
    finally:
        # Disposing the sole in-memory connection discards the entire database.
        db_engine.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = create_session_factory(engine)
    with factory() as db_session:
        yield db_session


def _pricing_graph(session: Session) -> dict[str, Any]:
    manufacturer = Manufacturer(name="OpenAI", slug="openai")
    model = StandardModel(
        manufacturer=manufacturer,
        canonical_name="gpt-test",
        display_name="GPT Test",
    )
    version = ModelVersion(
        standard_model=model,
        version_name="2026-09-01",
        release_date=date(2026, 9, 1),
    )
    provider_a = Provider(
        name="Provider A", slug="provider-a", provider_type="reseller"
    )
    provider_b = Provider(
        name="Provider B", slug="provider-b", provider_type="aggregator"
    )
    offering_a = Offering(
        provider=provider_a,
        standard_model=model,
        model_version=version,
        provider_model_name="gpt-test-a",
    )
    offering_b = Offering(
        provider=provider_b,
        standard_model=model,
        model_version=version,
        provider_model_name="gpt-test-b",
    )
    plan_a = PricingPlan(
        offering=offering_a,
        name="on-demand",
        billing_mode="pay_as_you_go",
        currency_code="USD",
        effective_from=NOW,
    )
    plan_a_committed = PricingPlan(
        offering=offering_a,
        name="committed",
        billing_mode="committed",
        currency_code="USD",
        effective_from=NOW,
    )
    PricingPlan(
        offering=offering_b,
        name="standard",
        billing_mode="pay_as_you_go",
        currency_code="CNY",
        effective_from=NOW,
    )
    source = DataSource(
        name="Provider A price sheet",
        source_type="provider_excel",
        authority_level="secondary",
    )
    snapshot = PriceSnapshot(
        pricing_plan=plan_a,
        data_source=source,
        observed_at=NOW,
        effective_from=NOW,
        currency_code="USD",
    )
    input_metric = BillingMetric(
        code="input_token",
        display_name="Input tokens",
        dimension="token",
        direction="input",
        unit_name="token",
        unit_quantity=1_000_000,
        comparable_group="text_input",
    )
    output_metric = BillingMetric(
        code="output_token",
        display_name="Output tokens",
        dimension="token",
        direction="output",
        unit_name="token",
        unit_quantity=1_000_000,
        comparable_group="text_output",
    )
    input_component = PriceComponent(
        snapshot=snapshot,
        billing_metric=input_metric,
        component_type="monetary",
        raw_amount=Decimal("1.234567890123"),
        raw_currency_code="USD",
        raw_unit_name="token",
        raw_unit_quantity=Decimal("1000000"),
    )
    output_component = PriceComponent(
        snapshot=snapshot,
        billing_metric=output_metric,
        component_type="monetary",
        raw_amount=Decimal("9.876543210987"),
        raw_currency_code="USD",
        raw_unit_name="token",
        raw_unit_quantity=Decimal("1000000"),
    )
    session.add_all(
        [
            plan_a_committed,
            input_component,
            output_component,
        ]
    )
    session.commit()
    return {
        "model": model,
        "provider_a": provider_a,
        "provider_b": provider_b,
        "offering_a": offering_a,
        "plan_a": plan_a,
        "source": source,
        "snapshot": snapshot,
        "input_metric": input_metric,
        "input_component": input_component,
    }


def _import_graph(session: Session) -> dict[str, Any]:
    provider = Provider(
        name="Import Provider",
        slug="import-provider",
        provider_type="reseller",
    )
    source = DataSource(
        name="Import source",
        source_type="provider_excel",
        authority_level="secondary",
    )
    artifact = SourceArtifact(
        data_source=source,
        uri="memory://quote.xlsx",
        original_filename="quote.xlsx",
        sha256="a" * 64,
        fetched_at=NOW,
    )
    batch = ImportBatch(
        source_artifact=artifact,
        provider=provider,
        idempotency_key="sha256:parser-v1",
        parser_version="parser-v1",
        total_rows=1,
    )
    row = ImportRow(
        import_batch=batch,
        sheet_name="Prices",
        row_number=1,
        row_hash="b" * 64,
        raw_data={"model": "gpt-test"},
        status="staged",
    )
    staged = StagedQuote(
        import_row=row,
        provider=provider,
        extracted_model_name="gpt-test",
        review_status="needs_review",
        extracted_quote={"currency": "USD"},
        validation_errors=["ambiguous version"],
    )
    session.add(staged)
    session.commit()
    return {
        "provider": provider,
        "artifact": artifact,
        "batch": batch,
        "staged": staged,
    }


def test_sqlite_memory_schema_and_pragmas(engine) -> None:
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables)
    assert len(Base.metadata.tables) == 20

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 2_345
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "memory"


def test_relationship_cardinalities_and_multiple_components(session: Session) -> None:
    graph = _pricing_graph(session)

    assert {offering.provider.slug for offering in graph["model"].offerings} == {
        "provider-a",
        "provider-b",
    }
    assert {plan.name for plan in graph["offering_a"].pricing_plans} == {
        "on-demand",
        "committed",
    }
    assert {
        component.billing_metric.code
        for component in graph["snapshot"].components
    } == {"input_token", "output_token"}


def test_foreign_keys_are_enforced(session: Session) -> None:
    manufacturer = Manufacturer(name="M", slug="m")
    model = StandardModel(
        manufacturer=manufacturer,
        canonical_name="model",
        display_name="Model",
    )
    session.add(model)
    session.commit()

    session.add(
        Offering(
            provider_id=999_999,
            standard_model_id=model.id,
            provider_model_name="missing-provider",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    provider = Provider(
        name="Valid Provider", slug="valid-provider", provider_type="reseller"
    )
    other_model = StandardModel(
        manufacturer_id=manufacturer.id,
        canonical_name="other-model",
        display_name="Other Model",
    )
    other_version = ModelVersion(
        standard_model=other_model, version_name="other-version"
    )
    session.add_all([provider, other_version])
    session.commit()

    session.add(
        Offering(
            provider_id=provider.id,
            standard_model_id=model.id,
            model_version_id=other_version.id,
            provider_model_name="mismatched-version",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    ("component_type", "values"),
    [
        (
            "monetary",
            {
                "raw_amount": Decimal("1"),
                "raw_currency_code": "USD",
                "raw_unit_name": "token",
                "raw_unit_quantity": Decimal("1000000"),
                "discount_rate": Decimal("0.1"),
            },
        ),
        (
            "discount",
            {
                "raw_amount": Decimal("1"),
                "raw_currency_code": "USD",
                "raw_unit_name": "token",
                "raw_unit_quantity": Decimal("1000000"),
                "discount_rate": Decimal("0.1"),
            },
        ),
    ],
)
def test_money_and_discount_shapes_are_mutually_exclusive(
    session: Session, component_type: str, values: dict[str, Any]
) -> None:
    graph = _pricing_graph(session)
    session.add(
        PriceComponent(
            price_snapshot_id=graph["snapshot"].id,
            billing_metric_id=graph["input_metric"].id,
            component_type=component_type,
            tier_start=Decimal("1"),
            condition_key=component_type,
            **values,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_price_history_is_appended_and_not_mutated(session: Session) -> None:
    graph = _pricing_graph(session)
    original = graph["snapshot"]
    replacement = PriceSnapshot(
        pricing_plan_id=graph["plan_a"].id,
        data_source_id=graph["source"].id,
        supersedes_snapshot_id=original.id,
        observed_at=NOW + timedelta(days=1),
        effective_from=NOW + timedelta(days=1),
        currency_code="USD",
        verification_status="verified",
        raw_payload={"revision": 2},
    )
    session.add(replacement)
    session.commit()

    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 2
    assert replacement.supersedes is original

    original.verification_status = "superseded"
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
    session.rollback()
    assert session.get(PriceSnapshot, original.id).verification_status == "pending"


def test_import_idempotency_and_review_workflow(session: Session) -> None:
    graph = _import_graph(session)
    review = ReviewItem(
        staged_quote=graph["staged"],
        review_type="version",
        severity="warning",
        reason="Version could not be determined",
        proposed_resolution={"version": "2026-09-01"},
    )
    session.add(review)
    session.commit()

    assert graph["staged"].review_items == [review]
    assert review.status == "open"

    session.add(
        ImportBatch(
            source_artifact_id=graph["artifact"].id,
            provider_id=graph["provider"].id,
            idempotency_key=graph["batch"].idempotency_key,
            parser_version="parser-v1",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    invalid_review = ReviewItem(
        staged_quote_id=graph["staged"].id,
        review_type="other",
        status="resolved",
        severity="info",
        reason="Missing resolution timestamp",
    )
    session.add(invalid_review)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_alias_rule_can_persist_a_review_decision(session: Session) -> None:
    pricing = _pricing_graph(session)
    imported = _import_graph(session)
    review = ReviewItem(
        staged_quote=imported["staged"],
        review_type="model_match",
        status="resolved",
        severity="warning",
        reason="Alias confirmed by reviewer",
        resolution={"standard_model_id": pricing["model"].id},
        resolved_by="reviewer",
        resolved_at=NOW,
    )
    rule = AliasRule(
        provider=imported["provider"],
        match_type="normalized",
        pattern="gpt test",
        target_standard_model=pricing["model"],
        created_from_review_item=review,
    )
    session.add(rule)
    session.commit()

    assert rule.created_from_review_item.resolution == {
        "standard_model_id": pricing["model"].id
    }


def test_utc_decimal_and_session_scope(engine, session: Session) -> None:
    graph = _pricing_graph(session)
    component_id = graph["input_component"].id
    snapshot_id = graph["snapshot"].id
    session.expire_all()

    component = session.get(PriceComponent, component_id)
    snapshot = session.get(PriceSnapshot, snapshot_id)
    assert component.raw_amount == Decimal("1.234567890123")
    assert snapshot.observed_at.tzinfo is not None
    assert snapshot.observed_at.utcoffset() == timedelta(0)

    session.add(
        AuditEvent(
            actor="test",
            action="create",
            entity_type="price",
            entity_id="1",
            occurred_at=datetime(2026, 9, 23, 8, 0),
        )
    )
    with pytest.raises(StatementError, match="naive datetimes"):
        session.commit()
    session.rollback()

    factory = create_session_factory(engine)
    with session_scope(factory) as scoped:
        scoped.add(Manufacturer(name="Scoped", slug="scoped"))
    with factory() as verification:
        assert verification.scalar(
            select(func.count())
            .select_from(Manufacturer)
            .where(Manufacturer.slug == "scoped")
        ) == 1


def test_audit_events_are_append_only(session: Session) -> None:
    event = AuditEvent(
        actor="tester",
        action="approve",
        entity_type="review_item",
        entity_id="42",
        after_data={"status": "resolved"},
    )
    session.add(event)
    session.commit()

    event.action = "rewrite"
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
    session.rollback()

    session.delete(event)
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
    session.rollback()



def test_source_artifact_and_fx_rate_are_append_only(session: Session) -> None:
    source = DataSource(
        name="Immutable evidence source",
        source_type="web",
        authority_level="secondary",
    )
    artifact = SourceArtifact(
        data_source=source,
        uri="https://evidence.example/original",
        sha256="f" * 64,
        fetched_at=NOW,
        raw_metadata={"revision": 1},
    )
    fx_rate = FXRate(
        base_currency="USD",
        quote_currency="CNY",
        rate=Decimal("7.1"),
        rate_date=date(2026, 9, 23),
        data_source=source,
        observed_at=NOW,
        verification_status="verified",
    )
    session.add_all([artifact, fx_rate])
    session.commit()

    artifact.raw_metadata = {"revision": 2}
    with pytest.raises(ValueError, match="SourceArtifact is append-only"):
        session.flush()
    session.rollback()

    fx_rate = session.get(FXRate, fx_rate.id)
    fx_rate.rate = Decimal("7.2")
    with pytest.raises(ValueError, match="FXRate is append-only"):
        session.flush()
    session.rollback()

    artifact = session.get(SourceArtifact, artifact.id)
    session.delete(artifact)
    with pytest.raises(ValueError, match="SourceArtifact is append-only"):
        session.flush()
    session.rollback()

def test_publication_idempotency_has_a_database_unique_index(engine) -> None:
    index = next(
        item
        for item in inspect(engine).get_indexes("price_snapshot")
        if item["name"] == "uq_price_snapshot_publication_idempotency"
    )
    assert index["unique"] == 1
    assert index["column_names"] == [
        "pricing_plan_id", "source_artifact_id", "publication_idempotency_key"
    ]



def test_initial_migration_matches_metadata_and_downgrades_cleanly() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0001_initial_schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        "tokensql_initial_migration", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    migration_engine = create_engine("sqlite+pysqlite:///:memory:")
    with migration_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        assert set(inspect(connection).get_table_names()) == set(
            Base.metadata.tables
        )
        differences = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                },
            ),
            Base.metadata,
        )
        assert differences == []

        source_id = connection.execute(
            DataSource.__table__.insert().values(
                name="migration immutable source",
                source_type="web",
                authority_level="secondary",
                is_active=True,
                attributes={},
                created_at=NOW,
                updated_at=NOW,
            )
        ).inserted_primary_key[0]
        connection.execute(
            SourceArtifact.__table__.insert().values(
                data_source_id=source_id,
                uri="https://evidence.example/source",
                sha256="e" * 64,
                fetched_at=NOW,
                raw_metadata={},
                created_at=NOW,
            )
        )
        connection.execute(
            FXRate.__table__.insert().values(
                base_currency="USD",
                quote_currency="CNY",
                rate=Decimal("7.1"),
                rate_date=date(2026, 9, 23),
                data_source_id=source_id,
                observed_at=NOW,
                verification_status="verified",
                created_at=NOW,
            )
        )
        with pytest.raises(DBAPIError, match="append-only"):
            connection.exec_driver_sql(
                "UPDATE source_artifact SET uri = 'mutated'"
            )
        with pytest.raises(DBAPIError, match="append-only"):
            connection.exec_driver_sql("UPDATE fx_rate SET rate = 7.2")

        connection.execute(
            AuditEvent.__table__.insert().values(
                actor="migration-test",
                action="insert",
                entity_type="audit_event",
                entity_id="1",
                occurred_at=NOW,
            )
        )
        with pytest.raises(DBAPIError, match="append-only"):
            connection.exec_driver_sql(
                "UPDATE audit_event SET action = 'mutated' WHERE entity_id = '1'"
            )

        migration.downgrade()
        assert inspect(connection).get_table_names() == []

    migration_engine.dispose()
