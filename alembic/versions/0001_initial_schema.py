"""Create the initial traceable pricing schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
MONEY = sa.Numeric(28, 12)
RATE = sa.Numeric(28, 12)
CONFIDENCE = sa.Numeric(5, 4)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = (
        "source_artifact",
        "fx_rate",
        "price_snapshot",
        "price_component",
        "price_normalization",
        "audit_event",
    )
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tokensql_deny_historical_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only; insert a replacement record', TG_TABLE_NAME;
            END;
            $$
            """
        )
        for table in tables:
            op.execute(
                f"CREATE TRIGGER trg_{table}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION tokensql_deny_historical_mutation()"
            )
    elif dialect == "sqlite":
        for table in tables:
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'historical pricing is append-only'); END"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'historical pricing is append-only'); END"
            )


def _drop_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = (
        "source_artifact",
        "fx_rate",
        "price_snapshot",
        "price_component",
        "price_normalization",
        "audit_event",
    )
    if dialect == "postgresql":
        for table in tables:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS tokensql_deny_historical_mutation()")
    elif dialect == "sqlite":
        for table in tables:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def upgrade() -> None:
    op.create_table(
        "manufacturer",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("website_url", sa.String(1024)),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_manufacturer"),
        sa.UniqueConstraint("name", name="uq_manufacturer_name"),
        sa.UniqueConstraint("slug", name="uq_manufacturer_slug"),
    )

    op.create_table(
        "provider",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("website_url", sa.String(1024)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "provider_type IN ('official', 'reseller', 'aggregator', 'internal')",
            name="ck_provider_provider_type_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider"),
        sa.UniqueConstraint("name", name="uq_provider_name"),
        sa.UniqueConstraint("slug", name="uq_provider_slug"),
    )

    op.create_table(
        "data_source",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("authority_level", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "source_type IN ('official', 'provider_excel', 'provider_api', 'web', 'manual')",
            name="ck_data_source_source_type_allowed",
        ),
        sa.CheckConstraint(
            "authority_level IN ('primary', 'secondary', 'internal')",
            name="ck_data_source_authority_level_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_source"),
        sa.UniqueConstraint("name", name="uq_data_source_name"),
    )

    op.create_table(
        "billing_metric",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32)),
        sa.Column("unit_name", sa.String(64), nullable=False),
        sa.Column("unit_quantity", sa.BigInteger(), nullable=False),
        sa.Column("comparable_group", sa.String(120), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "dimension IN ('token', 'request', 'image', 'audio', 'time', 'storage', 'other')",
            name="ck_billing_metric_dimension_allowed",
        ),
        sa.CheckConstraint("unit_quantity > 0", name="ck_billing_metric_unit_quantity_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_billing_metric"),
        sa.UniqueConstraint("code", name="uq_billing_metric_code"),
    )

    op.create_table(
        "standard_model",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manufacturer_id", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("family", sa.String(160)),
        sa.Column("modality", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["manufacturer_id"], ["manufacturer.id"],
            name="fk_standard_model_manufacturer_id_manufacturer", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_standard_model"),
        sa.UniqueConstraint(
            "manufacturer_id", "canonical_name",
            name="uq_standard_model_manufacturer_id_canonical_name"
        ),
    )
    op.create_index(
        "ix_standard_model_canonical_name", "standard_model", ["canonical_name"]
    )

    op.create_table(
        "model_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("standard_model_id", sa.Integer(), nullable=False),
        sa.Column("version_name", sa.String(255), nullable=False),
        sa.Column("release_date", sa.Date()),
        sa.Column("context_window_tokens", sa.BigInteger()),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["standard_model_id"], ["standard_model.id"],
            name="fk_model_version_standard_model_id_standard_model", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_version"),
        sa.UniqueConstraint(
            "standard_model_id", "version_name",
            name="uq_model_version_standard_model_id_version_name"
        ),
        sa.UniqueConstraint(
            "id", "standard_model_id",
            name="uq_model_version_id_standard_model_id",
        ),
    )

    op.create_table(
        "model_alias",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("normalized_alias", sa.String(255), nullable=False),
        sa.Column("alias_type", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.Integer()),
        sa.Column("standard_model_id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer()),
        sa.Column("confidence", CONFIDENCE),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("source_note", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(
            "alias_type IN ('api', 'provider', 'marketing', 'legacy', 'imported')",
            name="ck_model_alias_alias_type_allowed",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_model_alias_confidence_range",
        ),
        sa.CheckConstraint(
            "model_version_id IS NULL OR standard_model_id IS NOT NULL",
            name="ck_model_alias_version_requires_model",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"],
            name="fk_model_alias_provider_id_provider", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["standard_model_id"], ["standard_model.id"],
            name="fk_model_alias_standard_model_id_standard_model", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id", "standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            name=op.f(
                "fk_model_alias_model_version_id_standard_model_id_model_version"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_alias"),
    )
    op.create_index(
        "uq_model_alias_global_normalized", "model_alias", ["normalized_alias"],
        unique=True,
        sqlite_where=sa.text("provider_id IS NULL"),
        postgresql_where=sa.text("provider_id IS NULL"),
    )
    op.create_index(
        "uq_model_alias_provider_normalized", "model_alias",
        ["provider_id", "normalized_alias"], unique=True,
        sqlite_where=sa.text("provider_id IS NOT NULL"),
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    op.create_table(
        "offering",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("standard_model_id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer()),
        sa.Column("provider_model_name", sa.String(255), nullable=False),
        sa.Column("api_model_id", sa.String(255)),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "model_version_id IS NULL OR standard_model_id IS NOT NULL",
            name="ck_offering_version_requires_model",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"],
            name="fk_offering_provider_id_provider", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["standard_model_id"], ["standard_model.id"],
            name="fk_offering_standard_model_id_standard_model", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id", "standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            name=op.f(
                "fk_offering_model_version_id_standard_model_id_model_version"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_offering"),
        sa.UniqueConstraint(
            "provider_id", "provider_model_name", "region",
            name="uq_offering_provider_id_provider_model_name_region"
        ),
    )

    op.create_table(
        "pricing_plan",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("offering_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("billing_mode", sa.String(32), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("minimum_commitment_amount", MONEY),
        sa.Column("terms", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "billing_mode IN ('pay_as_you_go', 'subscription', 'committed', 'negotiated')",
            name="ck_pricing_plan_billing_mode_allowed",
        ),
        sa.CheckConstraint(
            "length(currency_code) = 3", name="ck_pricing_plan_currency_code_length"
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_pricing_plan_effective_range",
        ),
        sa.CheckConstraint(
            "minimum_commitment_amount IS NULL OR minimum_commitment_amount >= 0",
            name="ck_pricing_plan_minimum_commitment_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"], ["offering.id"],
            name="fk_pricing_plan_offering_id_offering", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pricing_plan"),
        sa.UniqueConstraint(
            "offering_id", "name", "effective_from",
            name="uq_pricing_plan_offering_id_name_effective_from"
        ),
    )

    op.create_table(
        "source_artifact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("data_source_id", sa.Integer(), nullable=False),
        sa.Column("uri", sa.String(2048), nullable=False),
        sa.Column("storage_uri", sa.String(2048)),
        sa.Column("raw_content", sa.LargeBinary()),
        sa.Column("original_filename", sa.String(512)),
        sa.Column("media_type", sa.String(160)),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_metadata", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(sha256) = 64", name="ck_source_artifact_sha256_length"),
        sa.ForeignKeyConstraint(
            ["data_source_id"], ["data_source.id"],
            name="fk_source_artifact_data_source_id_data_source", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_artifact"),
        sa.UniqueConstraint(
            "data_source_id", "sha256",
            name="uq_source_artifact_data_source_id_sha256"
        ),
    )

    op.create_table(
        "fx_rate",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("rate", RATE, nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("data_source_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(base_currency) = 3", name="ck_fx_rate_base_currency_length"
        ),
        sa.CheckConstraint(
            "length(quote_currency) = 3", name="ck_fx_rate_quote_currency_length"
        ),
        sa.CheckConstraint(
            "base_currency <> quote_currency", name="ck_fx_rate_different_currencies"
        ),
        sa.CheckConstraint("rate > 0", name="ck_fx_rate_rate_positive"),
        sa.CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')",
            name="ck_fx_rate_verification_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"], ["data_source.id"],
            name="fk_fx_rate_data_source_id_data_source", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fx_rate"),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "rate",
            "rate_date",
            "data_source_id",
            "verification_status",
            name=op.f(
                "uq_fx_rate_base_currency_quote_currency_rate_rate_date_"
                "data_source_id_verification_status"
            ),
        ),
    )

    op.create_table(
        "price_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pricing_plan_id", sa.Integer(), nullable=False),
        sa.Column("data_source_id", sa.Integer(), nullable=False),
        sa.Column("source_artifact_id", sa.Integer()),
        sa.Column("supersedes_snapshot_id", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column(
            "publication_idempotency_key", sa.String(255), nullable=True
        ),
        sa.Column("raw_payload", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency_code) = 3", name="ck_price_snapshot_currency_code_length"
        ),
        sa.CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected', 'superseded')",
            name="ck_price_snapshot_verification_status_allowed",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_price_snapshot_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_plan_id"], ["pricing_plan.id"],
            name="fk_price_snapshot_pricing_plan_id_pricing_plan", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"], ["data_source.id"],
            name="fk_price_snapshot_data_source_id_data_source", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"], ["source_artifact.id"],
            name="fk_price_snapshot_source_artifact_id_source_artifact", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_snapshot_id"], ["price_snapshot.id"],
            name="fk_price_snapshot_supersedes_snapshot_id_price_snapshot", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_snapshot"),
        sa.UniqueConstraint(
            "pricing_plan_id", "data_source_id", "observed_at",
            name="uq_price_snapshot_pricing_plan_id_data_source_id_observed_at"
        ),
    )
    op.create_index(
        "ix_price_snapshot_plan_effective", "price_snapshot",
        ["pricing_plan_id", "effective_from"]
    )

    op.create_index(
        "uq_price_snapshot_publication_idempotency",
        "price_snapshot",
        [
            "pricing_plan_id",
            "source_artifact_id",
            "publication_idempotency_key",
        ],
        unique=True,
        sqlite_where=sa.text("publication_idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("publication_idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "price_component",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("price_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("billing_metric_id", sa.Integer(), nullable=False),
        sa.Column("component_type", sa.String(32), nullable=False),
        sa.Column("tier_start", sa.Numeric(28, 6), nullable=False),
        sa.Column("tier_end", sa.Numeric(28, 6)),
        sa.Column("condition_key", sa.String(255), nullable=False),
        sa.Column("raw_amount", MONEY),
        sa.Column("raw_currency_code", sa.String(3)),
        sa.Column("raw_unit_name", sa.String(64)),
        sa.Column("raw_unit_quantity", sa.Numeric(28, 6)),
        sa.Column("discount_rate", sa.Numeric(9, 8)),
        sa.Column("conditions", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "component_type IN ('monetary', 'discount')",
            name="ck_price_component_component_type_allowed",
        ),
        sa.CheckConstraint(
            "tier_start >= 0 AND (tier_end IS NULL OR tier_end > tier_start)",
            name="ck_price_component_tier_range",
        ),
        sa.CheckConstraint(
            "(component_type = 'monetary' AND raw_amount IS NOT NULL "
            "AND raw_currency_code IS NOT NULL AND raw_unit_name IS NOT NULL "
            "AND raw_unit_quantity IS NOT NULL AND raw_unit_quantity > 0 "
            "AND discount_rate IS NULL) OR "
            "(component_type = 'discount' AND discount_rate IS NOT NULL "
            "AND discount_rate >= 0 AND discount_rate <= 1 "
            "AND raw_amount IS NULL AND raw_currency_code IS NULL "
            "AND raw_unit_name IS NULL AND raw_unit_quantity IS NULL)",
            name="ck_price_component_amount_or_discount_shape",
        ),
        sa.CheckConstraint(
            "raw_currency_code IS NULL OR length(raw_currency_code) = 3",
            name="ck_price_component_raw_currency_code_length",
        ),
        sa.ForeignKeyConstraint(
            ["price_snapshot_id"], ["price_snapshot.id"],
            name="fk_price_component_price_snapshot_id_price_snapshot", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["billing_metric_id"], ["billing_metric.id"],
            name="fk_price_component_billing_metric_id_billing_metric", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_component"),
        sa.UniqueConstraint(
            "price_snapshot_id", "billing_metric_id", "tier_start", "condition_key",
            name=op.f(
                "uq_price_component_price_snapshot_id_billing_metric_id_"
                "tier_start_condition_key"
            ),
        ),
    )

    op.create_table(
        "price_normalization",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("price_component_id", sa.Integer(), nullable=False),
        sa.Column("fx_rate_id", sa.Integer()),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("comparison_key", sa.String(64), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("normalized_amount", MONEY, nullable=False),
        sa.Column("normalized_currency_code", sa.String(3), nullable=False),
        sa.Column("normalized_unit_name", sa.String(64), nullable=False),
        sa.Column("normalized_unit_quantity", sa.BigInteger(), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "normalized_amount >= 0",
            name="ck_price_normalization_normalized_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "length(normalized_currency_code) = 3",
            name="ck_price_normalization_currency_code_length",
        ),
        sa.CheckConstraint(
            "normalized_unit_quantity > 0",
            name="ck_price_normalization_unit_quantity_positive",
        ),
        sa.CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')",
            name="ck_price_normalization_verification_status_allowed",
        ),
        sa.CheckConstraint(
            "length(comparison_key) = 64",
            name="ck_price_normalization_comparison_key_length",
        ),
        sa.ForeignKeyConstraint(
            ["price_component_id"], ["price_component.id"],
            name="fk_price_normalization_price_component_id_price_component", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["fx_rate_id"], ["fx_rate.id"],
            name="fk_price_normalization_fx_rate_id_fx_rate", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_normalization"),
    )
    op.create_index(
        "uq_price_normalization_without_fx",
        "price_normalization",
        ["price_component_id", "algorithm_version"],
        unique=True,
        sqlite_where=sa.text("fx_rate_id IS NULL"),
        postgresql_where=sa.text("fx_rate_id IS NULL"),
    )
    op.create_index(
        "uq_price_normalization_with_fx",
        "price_normalization",
        ["price_component_id", "fx_rate_id", "algorithm_version"],
        unique=True,
        sqlite_where=sa.text("fx_rate_id IS NOT NULL"),
        postgresql_where=sa.text("fx_rate_id IS NOT NULL"),
    )


    op.create_table(
        "import_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_artifact_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer()),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("parser_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("accepted_rows", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), nullable=False),
        sa.Column("review_rows", sa.Integer(), nullable=False),
        sa.Column("initiated_by", sa.String(255)),
        sa.Column("error_message", sa.Text()),
        sa.Column("import_context", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'partial', 'failed')",
            name="ck_import_batch_status_allowed",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_import_batch_completion_after_start",
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND accepted_rows >= 0 AND rejected_rows >= 0 "
            "AND review_rows >= 0",
            name="ck_import_batch_row_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_rows + rejected_rows + review_rows <= total_rows",
            name="ck_import_batch_row_counts_within_total",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifact.id"],
            name="fk_import_batch_source_artifact_id_source_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name="fk_import_batch_provider_id_provider",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batch"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_import_batch_idempotency_key"
        ),
    )

    op.create_table(
        "import_row",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("sheet_name", sa.String(255), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.Column("raw_data", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "row_number > 0", name="ck_import_row_row_number_positive"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'staged', 'accepted', 'rejected', 'error')",
            name="ck_import_row_status_allowed",
        ),
        sa.CheckConstraint(
            "length(row_hash) = 64", name="ck_import_row_row_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batch.id"],
            name="fk_import_row_import_batch_id_import_batch",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_row"),
        sa.UniqueConstraint(
            "import_batch_id",
            "sheet_name",
            "row_number",
            name="uq_import_row_import_batch_id_sheet_name_row_number",
        ),
    )

    op.create_table(
        "staged_quote",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_row_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer()),
        sa.Column("matched_standard_model_id", sa.Integer()),
        sa.Column("matched_model_version_id", sa.Integer()),
        sa.Column("matched_offering_id", sa.Integer()),
        sa.Column("extracted_model_name", sa.String(512)),
        sa.Column("extracted_plan_name", sa.String(512)),
        sa.Column("match_method", sa.String(32)),
        sa.Column("match_confidence", CONFIDENCE),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("extracted_quote", JSON_DOCUMENT, nullable=False),
        sa.Column("validation_errors", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "match_method IS NULL OR match_method IN ('exact', 'fuzzy', 'qwen', 'manual')",
            name="ck_staged_quote_match_method_allowed",
        ),
        sa.CheckConstraint(
            "match_confidence IS NULL OR "
            "(match_confidence >= 0 AND match_confidence <= 1)",
            name="ck_staged_quote_match_confidence_range",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected', 'needs_review')",
            name="ck_staged_quote_review_status_allowed",
        ),
        sa.CheckConstraint(
            "matched_model_version_id IS NULL OR matched_standard_model_id IS NOT NULL",
            name="ck_staged_quote_version_requires_model",
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_row.id"],
            name="fk_staged_quote_import_row_id_import_row",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name="fk_staged_quote_provider_id_provider",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_standard_model_id"],
            ["standard_model.id"],
            name="fk_staged_quote_matched_standard_model_id_standard_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_model_version_id", "matched_standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            name=op.f(
                "fk_staged_quote_matched_model_version_id_"
                "matched_standard_model_id_model_version"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_offering_id"],
            ["offering.id"],
            name="fk_staged_quote_matched_offering_id_offering",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staged_quote"),
        sa.UniqueConstraint(
            "import_row_id", name="uq_staged_quote_import_row_id"
        ),
    )

    op.create_table(
        "review_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("staged_quote_id", sa.Integer(), nullable=False),
        sa.Column("review_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_resolution", JSON_DOCUMENT),
        sa.Column("resolution", JSON_DOCUMENT),
        sa.Column("resolved_by", sa.String(255)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "review_type IN "
            "('model_match', 'version', 'currency', 'price', 'unit', 'duplicate', 'other')",
            name="ck_review_item_review_type_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_review_item_status_allowed",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error')",
            name="ck_review_item_severity_allowed",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL)",
            name="ck_review_item_resolution_state_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["staged_quote_id"],
            ["staged_quote.id"],
            name="fk_review_item_staged_quote_id_staged_quote",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_item"),
    )
    op.create_index(
        "ix_review_item_status_severity",
        "review_item",
        ["status", "severity"],
    )

    op.create_table(
        "alias_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.Integer()),
        sa.Column("match_type", sa.String(32), nullable=False),
        sa.Column("pattern", sa.String(512), nullable=False),
        sa.Column("target_standard_model_id", sa.Integer(), nullable=False),
        sa.Column("target_model_version_id", sa.Integer()),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_from_review_item_id", sa.Integer()),
        *_timestamps(),
        sa.CheckConstraint(
            "match_type IN ('exact', 'regex', 'normalized')",
            name="ck_alias_rule_match_type_allowed",
        ),
        sa.CheckConstraint(
            "priority >= 0", name="ck_alias_rule_priority_nonnegative"
        ),
        sa.CheckConstraint(
            "target_model_version_id IS NULL OR target_standard_model_id IS NOT NULL",
            name="ck_alias_rule_version_requires_model",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name="fk_alias_rule_provider_id_provider",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_standard_model_id"],
            ["standard_model.id"],
            name="fk_alias_rule_target_standard_model_id_standard_model",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id", "target_standard_model_id"],
            ["model_version.id", "model_version.standard_model_id"],
            name=op.f(
                "fk_alias_rule_target_model_version_id_"
                "target_standard_model_id_model_version"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_from_review_item_id"],
            ["review_item.id"],
            name="fk_alias_rule_created_from_review_item_id_review_item",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alias_rule"),
    )
    op.create_index(
        "uq_alias_rule_global",
        "alias_rule",
        ["match_type", "pattern"],
        unique=True,
        sqlite_where=sa.text("provider_id IS NULL"),
        postgresql_where=sa.text("provider_id IS NULL"),
    )
    op.create_index(
        "uq_alias_rule_provider",
        "alias_rule",
        ["provider_id", "match_type", "pattern"],
        unique=True,
        sqlite_where=sa.text("provider_id IS NOT NULL"),
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(120), nullable=False),
        sa.Column("entity_id", sa.String(120), nullable=False),
        sa.Column("correlation_id", sa.String(255)),
        sa.Column("before_data", JSON_DOCUMENT),
        sa.Column("after_data", JSON_DOCUMENT),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
    )
    op.create_index(
        "ix_audit_event_entity",
        "audit_event",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_audit_event_occurred_at", "audit_event", ["occurred_at"]
    )

    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()

    op.drop_index("ix_audit_event_occurred_at", table_name="audit_event")
    op.drop_index("ix_audit_event_entity", table_name="audit_event")
    op.drop_table("audit_event")

    op.drop_index("uq_alias_rule_provider", table_name="alias_rule")
    op.drop_index("uq_alias_rule_global", table_name="alias_rule")
    op.drop_table("alias_rule")

    op.drop_index("ix_review_item_status_severity", table_name="review_item")
    op.drop_table("review_item")
    op.drop_table("staged_quote")
    op.drop_table("import_row")
    op.drop_table("import_batch")
    op.drop_index("uq_price_normalization_with_fx", table_name="price_normalization")
    op.drop_index("uq_price_normalization_without_fx", table_name="price_normalization")
    op.drop_table("price_normalization")
    op.drop_table("price_component")

    op.drop_index(
        "ix_price_snapshot_plan_effective", table_name="price_snapshot"
    )
    op.drop_table("price_snapshot")
    op.drop_table("fx_rate")
    op.drop_table("source_artifact")
    op.drop_table("pricing_plan")
    op.drop_table("offering")

    op.drop_index(
        "uq_model_alias_provider_normalized", table_name="model_alias"
    )
    op.drop_index(
        "uq_model_alias_global_normalized", table_name="model_alias"
    )
    op.drop_table("model_alias")
    op.drop_table("model_version")

    op.drop_index(
        "ix_standard_model_canonical_name", table_name="standard_model"
    )
    op.drop_table("standard_model")
    op.drop_table("billing_metric")
    op.drop_table("data_source")
    op.drop_table("provider")
    op.drop_table("manufacturer")
