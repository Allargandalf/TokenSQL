"""Regression tests for reusable aliases created by human review."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AliasRule,
    AuditEvent,
    DataSource,
    ImportRow,
    ModelAlias,
    PriceSnapshot,
    ReviewItem,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.importers import ParserProfile
from tokensql.services import ExcelImportService, ImportContext
from tokensql.services.catalog_service import CatalogService
from tokensql.services.review_service import (
    AliasRuleConflictError,
    ReviewService,
    ReviewStateError,
)


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


def _catalog(session: Session):
    catalog = CatalogService(session)
    manufacturer = catalog.create_manufacturer("Review Reuse AI")
    model = catalog.create_model(
        manufacturer.id,
        "canonical-model-v2",
        display_name="Canonical Model v2",
    )
    version = catalog.create_version(model.id, "2026-09")
    provider = catalog.create_provider(
        "Reviewed Provider",
        provider_type="reseller",
    )
    source = DataSource(
        name="Review reuse workbook",
        source_type="provider_excel",
        authority_level="secondary",
    )
    session.add(source)
    session.flush()
    return catalog, model, version, provider, source


def _workbook(path: Path, model_name: str) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["模型名称", "报价或折扣（最低）"])
    sheet.append([model_name, "1元/秒"])
    workbook.save(path)
    workbook.close()


def _import(
    session: Session,
    path: Path,
    *,
    source_id: int,
    provider_id: int,
    parser_version: str,
):
    return ExcelImportService().import_file(
        session,
        path,
        ImportContext(
            data_source_id=source_id,
            provider_id=provider_id,
            parser_version=parser_version,
            profile=ParserProfile(),
        ),
    )


def _model_review(session: Session, batch_id: int) -> ReviewItem:
    value = session.scalar(
        select(ReviewItem)
        .join(StagedQuote)
        .join(ImportRow)
        .where(
            ImportRow.import_batch_id == batch_id,
            ReviewItem.review_type == "model_match",
        )
    )
    assert value is not None
    return value


@pytest.mark.parametrize("match_type", ["exact", "normalized"])
def test_approved_rule_is_reused_by_catalog_and_next_excel_import(
    session: Session,
    tmp_path: Path,
    match_type: str,
) -> None:
    catalog, model, version, provider, source = _catalog(session)
    path = tmp_path / f"{match_type}.xlsx"
    reviewed_alias = "Human Only Model Code"
    _workbook(path, reviewed_alias)

    first = _import(
        session,
        path,
        source_id=source.id,
        provider_id=provider.id,
        parser_version=f"{match_type}-v1",
    )
    review = _model_review(session, first.batch_id)

    ReviewService(session).approve(
        review.id,
        reviewer="catalog-reviewer",
        resolution={
            "standard_model_id": model.id,
            "model_version_id": version.id,
            "alias": reviewed_alias,
            "match_type": match_type,
            "create_alias_rule": True,
        },
    )

    alias = session.scalar(
        select(ModelAlias).where(
            ModelAlias.provider_id == provider.id,
            ModelAlias.normalized_alias == "human only model code",
        )
    )
    assert alias is not None
    assert alias.is_verified
    assert alias.standard_model_id == model.id
    assert alias.model_version_id == version.id
    assert alias.alias_type == "imported"
    assert alias.source_note == f"approved review_item:{review.id}"

    catalog_match = catalog.match_model(
        reviewed_alias,
        provider_id=provider.id,
    )
    assert catalog_match.standard_model_id == model.id
    assert catalog_match.model_version_id == version.id
    assert catalog_match.result.stage.value == "exact"

    other_provider = catalog.create_provider(
        "Other Reviewed Provider",
        provider_type="reseller",
    )
    outside_scope = catalog.match_model(
        reviewed_alias,
        provider_id=other_provider.id,
    )
    assert outside_scope.standard_model_id is None
    assert outside_scope.result.review_required

    second = _import(
        session,
        path,
        source_id=source.id,
        provider_id=provider.id,
        parser_version=f"{match_type}-v2",
    )
    staged = session.scalar(
        select(StagedQuote)
        .join(ImportRow)
        .where(ImportRow.import_batch_id == second.batch_id)
    )
    assert staged is not None
    assert staged.matched_standard_model_id == model.id
    assert staged.matched_model_version_id == version.id
    assert staged.match_method == "exact"
    assert not any(
        str(reason).startswith("model_match:")
        for reason in staged.validation_errors
    )
    assert session.scalar(select(func.count(PriceSnapshot.id))) == 0


def test_regex_rule_is_not_materialized_as_literal_model_alias(
    session: Session,
    tmp_path: Path,
) -> None:
    _, model, version, provider, source = _catalog(session)
    path = tmp_path / "regex.xlsx"
    _workbook(path, "unmatched-regex-source")

    imported = _import(
        session,
        path,
        source_id=source.id,
        provider_id=provider.id,
        parser_version="regex-v1",
    )
    review = _model_review(session, imported.batch_id)
    ReviewService(session).approve(
        review.id,
        reviewer="regex-reviewer",
        resolution={
            "standard_model_id": model.id,
            "model_version_id": version.id,
            "pattern": r"vendor-model-.*",
            "match_type": "regex",
            "create_alias_rule": True,
        },
    )

    rule = session.scalar(select(AliasRule))
    assert rule.match_type == "regex"
    assert rule.pattern == r"vendor-model-.*"
    assert session.scalar(select(func.count(ModelAlias.id))) == 0
    result = CatalogService(session).match_model(
        r"vendor-model-.*",
        provider_id=provider.id,
    )
    assert result.standard_model_id is None
    assert result.result.review_required


def test_alias_conflict_rolls_back_rule_and_writes_failed_approval_audit(
    session: Session,
    tmp_path: Path,
) -> None:
    catalog, model, version, provider, source = _catalog(session)
    other = catalog.create_model(
        model.manufacturer_id,
        "other-canonical-model",
        display_name="Other Canonical Model",
    )
    catalog.create_alias(
        "already-taken",
        other.id,
        provider_id=provider.id,
        human_confirmed=True,
    )
    path = tmp_path / "conflict.xlsx"
    _workbook(path, "fresh-review-value")

    imported = _import(
        session,
        path,
        source_id=source.id,
        provider_id=provider.id,
        parser_version="conflict-v1",
    )
    review = _model_review(session, imported.batch_id)

    with pytest.raises(AliasRuleConflictError, match="verified alias conflicts"):
        ReviewService(session).approve(
            review.id,
            reviewer="conflict-reviewer",
            resolution={
                "standard_model_id": model.id,
                "model_version_id": version.id,
                "alias": "already-taken",
                "match_type": "normalized",
                "create_alias_rule": True,
            },
        )

    assert review.status == "open"
    assert review.resolved_at is None
    assert session.scalar(select(func.count(AliasRule.id))) == 0
    aliases = session.scalars(select(ModelAlias)).all()
    assert len(aliases) == 1
    assert aliases[0].standard_model_id == other.id
    audit = session.scalar(
        select(AuditEvent).where(AuditEvent.action == "approve_rejected")
    )
    assert audit is not None
    assert "verified alias conflicts" in audit.after_data["error"]


def test_provider_scope_escalation_is_rejected_and_audited(
    session: Session,
    tmp_path: Path,
) -> None:
    catalog, model, version, provider, source = _catalog(session)
    other_provider = catalog.create_provider(
        "Out-of-scope Provider",
        provider_type="reseller",
    )
    path = tmp_path / "provider-scope.xlsx"
    _workbook(path, "provider-scoped-human-alias")
    imported = _import(
        session,
        path,
        source_id=source.id,
        provider_id=provider.id,
        parser_version="provider-scope-v1",
    )
    review = _model_review(session, imported.batch_id)

    with pytest.raises(ReviewStateError, match="provider scope differs"):
        ReviewService(session).approve(
            review.id,
            reviewer="scope-reviewer",
            resolution={
                "standard_model_id": model.id,
                "model_version_id": version.id,
                "alias": "provider-scoped-human-alias",
                "provider_id": other_provider.id,
                "match_type": "normalized",
                "create_alias_rule": True,
            },
        )

    assert review.status == "open"
    assert session.scalar(select(func.count(AliasRule.id))) == 0
    assert session.scalar(select(func.count(ModelAlias.id))) == 0
    audit = session.scalar(
        select(AuditEvent).where(AuditEvent.action == "approve_rejected")
    )
    assert audit is not None
