"""Relational model for traceable token-price ingestion and normalization.

Status and category fields intentionally use strings plus CHECK constraints rather
than database enums, so migrations remain portable across PostgreSQL and SQLite.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from tokensql.db.base import (
    Base,
    IntegerPrimaryKeyMixin,
    TimestampMixin,
    UTCDateTime,
    utc_now,
)

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
MONEY = Numeric(28, 12)
RATE = Numeric(28, 12)
CONFIDENCE = Numeric(5, 4)


class Manufacturer(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manufacturer"

    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    website_url: Mapped[str | None] = mapped_column(String(1024))

    models: Mapped[list[StandardModel]] = relationship(back_populates="manufacturer")


class StandardModel(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "standard_model"
    __table_args__ = (
        UniqueConstraint("manufacturer_id", "canonical_name"),
        Index("ix_standard_model_canonical_name", "canonical_name"),
    )

    manufacturer_id: Mapped[int] = mapped_column(
        ForeignKey("manufacturer.id", ondelete="RESTRICT"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    family: Mapped[str | None] = mapped_column(String(160))
    modality: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    manufacturer: Mapped[Manufacturer] = relationship(back_populates="models")
    versions: Mapped[list[ModelVersion]] = relationship(back_populates="standard_model")
    aliases: Mapped[list[ModelAlias]] = relationship(back_populates="standard_model")
    offerings: Mapped[list[Offering]] = relationship(back_populates="standard_model")


class ModelVersion(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_version"
    __table_args__ = (
        UniqueConstraint("standard_model_id", "version_name"),
        UniqueConstraint("id", "standard_model_id"),
    )

    standard_model_id: Mapped[int] = mapped_column(
        ForeignKey("standard_model.id", ondelete="CASCADE"), nullable=False
    )
    version_name: Mapped[str] = mapped_column(String(255), nullable=False)
    release_date: Mapped[date | None] = mapped_column(Date)
    context_window_tokens: Mapped[int | None] = mapped_column(BigInteger)
    deprecated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    standard_model: Mapped[StandardModel] = relationship(back_populates="versions")
    aliases: Mapped[list[ModelAlias]] = relationship(
        back_populates="model_version", foreign_keys="ModelAlias.model_version_id"
    )
    offerings: Mapped[list[Offering]] = relationship(
        back_populates="model_version", foreign_keys="Offering.model_version_id"
    )


class Provider(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider"
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('official', 'reseller', 'aggregator', 'internal')",
            name="provider_type_allowed",
        ),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(1024))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    offerings: Mapped[list[Offering]] = relationship(back_populates="provider")
    aliases: Mapped[list[ModelAlias]] = relationship(back_populates="provider")


class ModelAlias(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_alias"
    __table_args__ = (
        CheckConstraint(
            "alias_type IN ('api', 'provider', 'marketing', 'legacy', 'imported')",
            name="alias_type_allowed",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        CheckConstraint(
            "model_version_id IS NULL OR standard_model_id IS NOT NULL",
            name="version_requires_model",
        ),
        ForeignKeyConstraint(
            ["model_version_id", "standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_model_alias_global_normalized",
            "normalized_alias",
            unique=True,
            sqlite_where=text("provider_id IS NULL"),
            postgresql_where=text("provider_id IS NULL"),
        ),
        Index(
            "uq_model_alias_provider_normalized",
            "provider_id",
            "normalized_alias",
            unique=True,
            sqlite_where=text("provider_id IS NOT NULL"),
            postgresql_where=text("provider_id IS NOT NULL"),
        ),
    )

    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider.id", ondelete="CASCADE")
    )
    standard_model_id: Mapped[int] = mapped_column(
        ForeignKey("standard_model.id", ondelete="CASCADE"), nullable=False
    )
    model_version_id: Mapped[int | None] = mapped_column()
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[Provider | None] = relationship(back_populates="aliases")
    standard_model: Mapped[StandardModel] = relationship(
        back_populates="aliases", foreign_keys=[standard_model_id]
    )
    model_version: Mapped[ModelVersion | None] = relationship(
        back_populates="aliases", foreign_keys=[model_version_id]
    )


class Offering(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "offering"
    __table_args__ = (
        UniqueConstraint("provider_id", "provider_model_name", "region"),
        CheckConstraint(
            "model_version_id IS NULL OR standard_model_id IS NOT NULL",
            name="version_requires_model",
        ),
        ForeignKeyConstraint(
            ["model_version_id", "standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            ondelete="RESTRICT",
        ),
    )

    provider_id: Mapped[int] = mapped_column(
        ForeignKey("provider.id", ondelete="RESTRICT"), nullable=False
    )
    standard_model_id: Mapped[int] = mapped_column(
        ForeignKey("standard_model.id", ondelete="RESTRICT"), nullable=False
    )
    model_version_id: Mapped[int | None] = mapped_column()
    provider_model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_model_id: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(64), default="global", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    provider: Mapped[Provider] = relationship(back_populates="offerings")
    standard_model: Mapped[StandardModel] = relationship(
        back_populates="offerings", foreign_keys=[standard_model_id]
    )
    model_version: Mapped[ModelVersion | None] = relationship(
        back_populates="offerings", foreign_keys=[model_version_id]
    )
    pricing_plans: Mapped[list[PricingPlan]] = relationship(back_populates="offering")


class PricingPlan(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pricing_plan"
    __table_args__ = (
        UniqueConstraint("offering_id", "name", "effective_from"),
        CheckConstraint(
            "billing_mode IN ('pay_as_you_go', 'subscription', 'committed', 'negotiated')",
            name="billing_mode_allowed",
        ),
        CheckConstraint("length(currency_code) = 3", name="currency_code_length"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range",
        ),
        CheckConstraint(
            "minimum_commitment_amount IS NULL OR minimum_commitment_amount >= 0",
            name="minimum_commitment_nonnegative",
        ),
    )

    offering_id: Mapped[int] = mapped_column(
        ForeignKey("offering.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime())
    minimum_commitment_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    terms: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    offering: Mapped[Offering] = relationship(back_populates="pricing_plans")
    snapshots: Mapped[list[PriceSnapshot]] = relationship(back_populates="pricing_plan")


class BillingMetric(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_metric"
    __table_args__ = (
        CheckConstraint(
            "dimension IN ('token', 'request', 'image', 'audio', 'time', 'storage', 'other')",
            name="dimension_allowed",
        ),
        CheckConstraint("unit_quantity > 0", name="unit_quantity_positive"),
    )

    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(32))
    unit_name: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comparable_group: Mapped[str] = mapped_column(String(120), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    components: Mapped[list[PriceComponent]] = relationship(back_populates="billing_metric")


class DataSource(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_source"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('official', 'provider_excel', 'provider_api', 'web', 'manual')",
            name="source_type_allowed",
        ),
        CheckConstraint(
            "authority_level IN ('primary', 'secondary', 'internal')",
            name="authority_level_allowed",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_level: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    artifacts: Mapped[list[SourceArtifact]] = relationship(back_populates="data_source")


class SourceArtifact(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "source_artifact"
    __table_args__ = (
        UniqueConstraint("data_source_id", "sha256"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
    )

    data_source_id: Mapped[int] = mapped_column(
        ForeignKey("data_source.id", ondelete="RESTRICT"), nullable=False
    )
    uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    storage_uri: Mapped[str | None] = mapped_column(String(2048))
    raw_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    media_type: Mapped[str | None] = mapped_column(String(160))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    data_source: Mapped[DataSource] = relationship(back_populates="artifacts")
    import_batches: Mapped[list[ImportBatch]] = relationship(back_populates="source_artifact")


class FXRate(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "fx_rate"
    __table_args__ = (
        UniqueConstraint(
            "base_currency",
            "quote_currency",
            "rate",
            "rate_date",
            "data_source_id",
            "verification_status",
        ),
        CheckConstraint("length(base_currency) = 3", name="base_currency_length"),
        CheckConstraint("length(quote_currency) = 3", name="quote_currency_length"),
        CheckConstraint("base_currency <> quote_currency", name="different_currencies"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')",
            name="verification_status_allowed",
        ),
    )

    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    data_source_id: Mapped[int] = mapped_column(
        ForeignKey("data_source.id", ondelete="RESTRICT"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    data_source: Mapped[DataSource] = relationship()
    normalizations: Mapped[list[PriceNormalization]] = relationship(back_populates="fx_rate")


class PriceSnapshot(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "price_snapshot"
    __table_args__ = (
        UniqueConstraint("pricing_plan_id", "data_source_id", "observed_at"),
        Index(
            "uq_price_snapshot_publication_idempotency",
            "pricing_plan_id",
            "source_artifact_id",
            "publication_idempotency_key",
            unique=True,
            sqlite_where=text("publication_idempotency_key IS NOT NULL"),
            postgresql_where=text("publication_idempotency_key IS NOT NULL"),
        ),
        CheckConstraint("length(currency_code) = 3", name="currency_code_length"),
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected', 'superseded')",
            name="verification_status_allowed",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range",
        ),
        Index("ix_price_snapshot_plan_effective", "pricing_plan_id", "effective_from"),
    )

    pricing_plan_id: Mapped[int] = mapped_column(
        ForeignKey("pricing_plan.id", ondelete="RESTRICT"), nullable=False
    )
    data_source_id: Mapped[int] = mapped_column(
        ForeignKey("data_source.id", ondelete="RESTRICT"), nullable=False
    )
    source_artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_artifact.id", ondelete="RESTRICT")
    )
    supersedes_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_snapshot.id", ondelete="RESTRICT")
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime())
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    publication_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    pricing_plan: Mapped[PricingPlan] = relationship(back_populates="snapshots")
    data_source: Mapped[DataSource] = relationship()
    source_artifact: Mapped[SourceArtifact | None] = relationship()
    supersedes: Mapped[PriceSnapshot | None] = relationship(remote_side="PriceSnapshot.id")
    components: Mapped[list[PriceComponent]] = relationship(back_populates="snapshot")


class PriceComponent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "price_component"
    __table_args__ = (
        UniqueConstraint(
            "price_snapshot_id", "billing_metric_id", "tier_start", "condition_key"
        ),
        CheckConstraint(
            "component_type IN ('monetary', 'discount')",
            name="component_type_allowed",
        ),
        CheckConstraint(
            "tier_start >= 0 AND (tier_end IS NULL OR tier_end > tier_start)",
            name="tier_range",
        ),
        CheckConstraint(
            "(component_type = 'monetary' AND raw_amount IS NOT NULL "
            "AND raw_currency_code IS NOT NULL AND raw_unit_name IS NOT NULL "
            "AND raw_unit_quantity IS NOT NULL AND raw_unit_quantity > 0 "
            "AND discount_rate IS NULL) OR "
            "(component_type = 'discount' AND discount_rate IS NOT NULL "
            "AND discount_rate >= 0 AND discount_rate <= 1 "
            "AND raw_amount IS NULL AND raw_currency_code IS NULL "
            "AND raw_unit_name IS NULL AND raw_unit_quantity IS NULL)",
            name="amount_or_discount_shape",
        ),
        CheckConstraint(
            "raw_currency_code IS NULL OR length(raw_currency_code) = 3",
            name="raw_currency_code_length",
        ),
    )

    price_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("price_snapshot.id", ondelete="RESTRICT"), nullable=False
    )
    billing_metric_id: Mapped[int] = mapped_column(
        ForeignKey("billing_metric.id", ondelete="RESTRICT"), nullable=False
    )
    component_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tier_start: Mapped[Decimal] = mapped_column(Numeric(28, 6), default=0, nullable=False)
    tier_end: Mapped[Decimal | None] = mapped_column(Numeric(28, 6))
    condition_key: Mapped[str] = mapped_column(String(255), default="default", nullable=False)
    raw_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    raw_currency_code: Mapped[str | None] = mapped_column(String(3))
    raw_unit_name: Mapped[str | None] = mapped_column(String(64))
    raw_unit_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 6))
    discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 8))
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    snapshot: Mapped[PriceSnapshot] = relationship(back_populates="components")
    billing_metric: Mapped[BillingMetric] = relationship(back_populates="components")
    normalizations: Mapped[list[PriceNormalization]] = relationship(back_populates="price_component")


class PriceNormalization(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "price_normalization"
    __table_args__ = (
        Index(
            "uq_price_normalization_without_fx",
            "price_component_id",
            "algorithm_version",
            unique=True,
            sqlite_where=text("fx_rate_id IS NULL"),
            postgresql_where=text("fx_rate_id IS NULL"),
        ),
        Index(
            "uq_price_normalization_with_fx",
            "price_component_id",
            "fx_rate_id",
            "algorithm_version",
            unique=True,
            sqlite_where=text("fx_rate_id IS NOT NULL"),
            postgresql_where=text("fx_rate_id IS NOT NULL"),
        ),
        CheckConstraint("normalized_amount >= 0", name="normalized_amount_nonnegative"),
        CheckConstraint("length(normalized_currency_code) = 3", name="currency_code_length"),
        CheckConstraint("normalized_unit_quantity > 0", name="unit_quantity_positive"),
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')",
            name="verification_status_allowed",
        ),
        CheckConstraint("length(comparison_key) = 64", name="comparison_key_length"),
    )

    price_component_id: Mapped[int] = mapped_column(
        ForeignKey("price_component.id", ondelete="RESTRICT"), nullable=False
    )
    fx_rate_id: Mapped[int | None] = mapped_column(
        ForeignKey("fx_rate.id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(80), nullable=False)
    comparison_key: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    normalized_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    normalized_currency_code: Mapped[str] = mapped_column(
        String(3), default="CNY", nullable=False
    )
    normalized_unit_name: Mapped[str] = mapped_column(
        String(64), default="token", nullable=False
    )
    normalized_unit_quantity: Mapped[int] = mapped_column(
        BigInteger, default=1_000_000, nullable=False
    )
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    price_component: Mapped[PriceComponent] = relationship(back_populates="normalizations")
    fx_rate: Mapped[FXRate | None] = relationship(back_populates="normalizations")


class ImportBatch(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'partial', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="completion_after_start",
        ),
        CheckConstraint(
            "total_rows >= 0 AND accepted_rows >= 0 AND rejected_rows >= 0 "
            "AND review_rows >= 0",
            name="row_counts_nonnegative",
        ),
        CheckConstraint(
            "accepted_rows + rejected_rows + review_rows <= total_rows",
            name="row_counts_within_total",
        ),
    )

    source_artifact_id: Mapped[int] = mapped_column(
        ForeignKey("source_artifact.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    import_context: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    source_artifact: Mapped[SourceArtifact] = relationship(back_populates="import_batches")
    provider: Mapped[Provider | None] = relationship()
    rows: Mapped[list[ImportRow]] = relationship(back_populates="import_batch")


class ImportRow(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "import_row"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "sheet_name", "row_number"),
        CheckConstraint("row_number > 0", name="row_number_positive"),
        CheckConstraint(
            "status IN ('pending', 'staged', 'accepted', 'rejected', 'error')",
            name="status_allowed",
        ),
        CheckConstraint("length(row_hash) = 64", name="row_hash_length"),
    )

    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False
    )
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    import_batch: Mapped[ImportBatch] = relationship(back_populates="rows")
    staged_quote: Mapped[StagedQuote | None] = relationship(
        back_populates="import_row", uselist=False
    )


class StagedQuote(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "staged_quote"
    __table_args__ = (
        CheckConstraint(
            "match_method IS NULL OR match_method IN ('exact', 'fuzzy', 'qwen', 'manual')",
            name="match_method_allowed",
        ),
        CheckConstraint(
            "match_confidence IS NULL OR (match_confidence >= 0 AND match_confidence <= 1)",
            name="match_confidence_range",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected', 'needs_review')",
            name="review_status_allowed",
        ),
        CheckConstraint(
            "matched_model_version_id IS NULL OR matched_standard_model_id IS NOT NULL",
            name="version_requires_model",
        ),
        ForeignKeyConstraint(
            ["matched_model_version_id", "matched_standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            ondelete="RESTRICT",
        ),
    )

    import_row_id: Mapped[int] = mapped_column(
        ForeignKey("import_row.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider.id", ondelete="RESTRICT")
    )
    matched_standard_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("standard_model.id", ondelete="RESTRICT")
    )
    matched_model_version_id: Mapped[int | None] = mapped_column()
    matched_offering_id: Mapped[int | None] = mapped_column(
        ForeignKey("offering.id", ondelete="RESTRICT")
    )
    extracted_model_name: Mapped[str | None] = mapped_column(String(512))
    extracted_plan_name: Mapped[str | None] = mapped_column(String(512))
    match_method: Mapped[str | None] = mapped_column(String(32))
    match_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    review_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    extracted_quote: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    validation_errors: Mapped[list[Any]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)

    import_row: Mapped[ImportRow] = relationship(back_populates="staged_quote")
    provider: Mapped[Provider | None] = relationship()
    matched_standard_model: Mapped[StandardModel | None] = relationship(
        foreign_keys=[matched_standard_model_id]
    )
    matched_model_version: Mapped[ModelVersion | None] = relationship(
        foreign_keys=[matched_model_version_id]
    )
    matched_offering: Mapped[Offering | None] = relationship()
    review_items: Mapped[list[ReviewItem]] = relationship(back_populates="staged_quote")


class ReviewItem(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_item"
    __table_args__ = (
        CheckConstraint(
            "review_type IN ('model_match', 'version', 'currency', 'price', 'unit', 'duplicate', 'other')",
            name="review_type_allowed",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name="status_allowed"
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'error')", name="severity_allowed"
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL)",
            name="resolution_state_consistent",
        ),
        Index("ix_review_item_status_severity", "status", "severity"),
    )

    staged_quote_id: Mapped[int] = mapped_column(
        ForeignKey("staged_quote.id", ondelete="CASCADE"), nullable=False
    )
    review_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="warning", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    staged_quote: Mapped[StagedQuote] = relationship(back_populates="review_items")


class AliasRule(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alias_rule"
    __table_args__ = (
        CheckConstraint(
            "match_type IN ('exact', 'regex', 'normalized')", name="match_type_allowed"
        ),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint(
            "target_model_version_id IS NULL OR target_standard_model_id IS NOT NULL",
            name="version_requires_model",
        ),
        ForeignKeyConstraint(
            ["target_model_version_id", "target_standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_alias_rule_global",
            "match_type",
            "pattern",
            unique=True,
            sqlite_where=text("provider_id IS NULL"),
            postgresql_where=text("provider_id IS NULL"),
        ),
        Index(
            "uq_alias_rule_provider",
            "provider_id",
            "match_type",
            "pattern",
            unique=True,
            sqlite_where=text("provider_id IS NOT NULL"),
            postgresql_where=text("provider_id IS NOT NULL"),
        ),
    )

    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider.id", ondelete="CASCADE")
    )
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    target_standard_model_id: Mapped[int] = mapped_column(
        ForeignKey("standard_model.id", ondelete="CASCADE"), nullable=False
    )
    target_model_version_id: Mapped[int | None] = mapped_column()
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_from_review_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_item.id", ondelete="SET NULL")
    )

    provider: Mapped[Provider | None] = relationship()
    target_standard_model: Mapped[StandardModel] = relationship(
        foreign_keys=[target_standard_model_id]
    )
    target_model_version: Mapped[ModelVersion | None] = relationship(
        foreign_keys=[target_model_version_id]
    )
    created_from_review_item: Mapped[ReviewItem | None] = relationship()


class AuditEvent(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_entity", "entity_type", "entity_id"),
        Index("ix_audit_event_occurred_at", "occurred_at"),
    )

    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(255))
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


def _append_only_error(target: object) -> ValueError:
    return ValueError(
        f"{type(target).__name__} is append-only; insert a replacement record instead"
    )


def _deny_historical_update(mapper: object, connection: object, target: object) -> None:
    del mapper, connection
    session = object_session(target)
    # Relationship collection maintenance does not mutate the historical row.
    # Database triggers remain the final guard against every SQL UPDATE/DELETE.
    if session is None or session.is_modified(target, include_collections=False):
        raise _append_only_error(target)


def _deny_historical_delete(mapper: object, connection: object, target: object) -> None:
    del mapper, connection
    raise _append_only_error(target)


for _append_only_model in (
    SourceArtifact,
    FXRate,
    PriceSnapshot,
    PriceComponent,
    PriceNormalization,
    AuditEvent,
):
    event.listen(_append_only_model, "before_update", _deny_historical_update)
    event.listen(_append_only_model, "before_delete", _deny_historical_delete)
