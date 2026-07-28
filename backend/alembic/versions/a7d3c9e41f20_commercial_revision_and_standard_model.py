"""commercial revision semantics and standard model lineage

Revision ID: a7d3c9e41f20
Revises: c4f8e1d2a930
Create Date: 2026-07-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d3c9e41f20"
down_revision: Union[str, None] = "c4f8e1d2a930"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MODEL_CHECKSUM = "82b91260222b353b06ff1937417591f64a1cb2b67ea8a96aff8829ef34408106"


def upgrade() -> None:
    op.add_column("platform_accounts", sa.Column("logical_id", sa.String(length=36), nullable=True))
    op.execute(sa.text("UPDATE platform_accounts SET logical_id = id WHERE logical_id IS NULL"))
    with op.batch_alter_table("platform_accounts") as batch:
        batch.alter_column("logical_id", nullable=False)
        batch.create_index("ix_platforms_logical_effective", ["enterprise_id", "logical_id", "effective_from"])

    op.add_column("source_definitions", sa.Column("logical_id", sa.String(length=36), nullable=True))
    op.add_column("source_definitions", sa.Column("import_mode", sa.String(length=32), nullable=False, server_default="monthly_snapshot"))
    op.add_column("source_definitions", sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="orders"))
    op.add_column("source_definitions", sa.Column("amount_directions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.execute(sa.text("UPDATE source_definitions SET logical_id = id WHERE logical_id IS NULL"))
    with op.batch_alter_table("source_definitions") as batch:
        batch.alter_column("logical_id", nullable=False)
        batch.create_index("ix_sources_logical_effective", ["enterprise_id", "logical_id", "effective_from"])

    with op.batch_alter_table("ingestion_runs") as batch:
        batch.add_column(sa.Column("supersedes_run_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("correction_of_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("correction_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("correction_approved_by", sa.Text(), nullable=True))

    with op.batch_alter_table("normalized_records") as batch:
        batch.add_column(sa.Column("store_logical_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_logical_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("business_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("superseded_by_run_id", sa.String(length=36), nullable=True))
    op.execute(sa.text(
        "UPDATE normalized_records SET "
        "store_logical_id = COALESCE((SELECT logical_id FROM stores WHERE stores.id = normalized_records.store_id), 'unassigned'), "
        "source_logical_id = (SELECT source_definitions.logical_id FROM ingestion_runs JOIN source_definitions ON source_definitions.id = ingestion_runs.source_definition_id WHERE ingestion_runs.id = normalized_records.ingestion_run_id)"
    ))
    with op.batch_alter_table("normalized_records") as batch:
        batch.alter_column("store_logical_id", nullable=False)
        batch.alter_column("source_logical_id", nullable=False)
        batch.create_index("ix_records_business_key", ["enterprise_id", "source_logical_id", "store_logical_id", "business_key"])
    op.create_index(
        "uq_records_current_business_key",
        "normalized_records",
        ["enterprise_id", "source_logical_id", "store_logical_id", "business_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1 AND business_key IS NOT NULL"),
        postgresql_where=sa.text("is_current AND business_key IS NOT NULL"),
    )

    with op.batch_alter_table("certified_aggregates") as batch:
        batch.add_column(sa.Column("store_logical_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_definition_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_logical_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("model_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("model_checksum", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("superseded_by_run_id", sa.String(length=36), nullable=True))
    op.execute(sa.text(
        "UPDATE certified_aggregates SET "
        "store_logical_id = COALESCE((SELECT logical_id FROM stores WHERE stores.id = certified_aggregates.store_id), 'unassigned'), "
        "source_definition_id = (SELECT source_definition_id FROM ingestion_runs WHERE ingestion_runs.id = certified_aggregates.ingestion_run_id), "
        "source_logical_id = (SELECT source_definitions.logical_id FROM ingestion_runs JOIN source_definitions ON source_definitions.id = ingestion_runs.source_definition_id WHERE ingestion_runs.id = certified_aggregates.ingestion_run_id), "
        "source_version = (SELECT source_version FROM ingestion_runs WHERE ingestion_runs.id = certified_aggregates.ingestion_run_id), "
        "model_version = COALESCE((SELECT model_version FROM ingestion_runs WHERE ingestion_runs.id = certified_aggregates.ingestion_run_id), 1), "
        f"model_checksum = '{MODEL_CHECKSUM}'"
    ))
    with op.batch_alter_table("certified_aggregates") as batch:
        batch.alter_column("store_logical_id", nullable=False)
        batch.alter_column("source_definition_id", nullable=False)
        batch.alter_column("source_logical_id", nullable=False)
        batch.alter_column("source_version", nullable=False)
        batch.alter_column("model_version", nullable=False)
        batch.alter_column("model_checksum", nullable=False)
        batch.create_foreign_key("fk_certified_source_definition", "source_definitions", ["source_definition_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_certified_current_scope", ["enterprise_id", "source_logical_id", "store_logical_id", "period_start", "is_current"])

    op.create_table(
        "cross_source_reconciliations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("ingestion_run_id", sa.String(length=36), nullable=False),
        sa.Column("dependency_run_id", sa.String(length=36), nullable=True),
        sa.Column("validation_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("store_logical_id", sa.String(length=36), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("actual_value", sa.Text(), nullable=True),
        sa.Column("expected_value", sa.Text(), nullable=True),
        sa.Column("difference", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dependency_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingestion_run_id", "validation_key", "store_logical_id", "period_start", name="uq_cross_source_run_scope"),
    )
    op.create_index("ix_cross_source_reconciliations_enterprise_id", "cross_source_reconciliations", ["enterprise_id"])
    op.create_index("ix_cross_source_reconciliations_ingestion_run_id", "cross_source_reconciliations", ["ingestion_run_id"])
    op.create_index("ix_cross_source_reconciliations_dependency_run_id", "cross_source_reconciliations", ["dependency_run_id"])
    op.create_index("ix_cross_source_scope", "cross_source_reconciliations", ["enterprise_id", "store_logical_id", "period_start", "status"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE cross_source_reconciliations ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(
            "CREATE POLICY cross_source_reconciliations_tenant_isolation ON cross_source_reconciliations "
            "USING (enterprise_id = current_setting('app.current_enterprise_id', true)) "
            "WITH CHECK (enterprise_id = current_setting('app.current_enterprise_id', true))"
        ))
        projection = "enterprise_id, store_id, period_start, grain, row_count, order_count, revenue, refund, platform_fee, advertising_fee, shipping_fee, product_cost, fees, profit"
        op.execute(sa.text("DROP VIEW IF EXISTS certified.sales"))
        op.execute(sa.text("DROP VIEW IF EXISTS certified_sales"))
        op.execute(sa.text(f"CREATE VIEW certified_sales AS SELECT {projection} FROM certified_aggregates WHERE is_current"))
        op.execute(sa.text(f"CREATE VIEW certified.sales AS SELECT {projection} FROM certified_aggregates WHERE is_current"))
        op.execute(sa.text("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='analytics_reader') THEN GRANT SELECT ON certified.sales TO analytics_reader; END IF; END $$"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP VIEW IF EXISTS certified.sales"))
        op.execute(sa.text("DROP VIEW IF EXISTS certified_sales"))
    op.drop_index("ix_cross_source_scope", table_name="cross_source_reconciliations")
    op.drop_index("ix_cross_source_reconciliations_dependency_run_id", table_name="cross_source_reconciliations")
    op.drop_index("ix_cross_source_reconciliations_ingestion_run_id", table_name="cross_source_reconciliations")
    op.drop_index("ix_cross_source_reconciliations_enterprise_id", table_name="cross_source_reconciliations")
    op.drop_table("cross_source_reconciliations")
    with op.batch_alter_table("certified_aggregates") as batch:
        batch.drop_index("ix_certified_current_scope")
        batch.drop_constraint("fk_certified_source_definition", type_="foreignkey")
        for column in ["superseded_by_run_id", "superseded_at", "is_current", "model_checksum", "model_version", "source_version", "source_logical_id", "source_definition_id", "store_logical_id"]:
            batch.drop_column(column)
    op.drop_index("uq_records_current_business_key", table_name="normalized_records")
    with op.batch_alter_table("normalized_records") as batch:
        batch.drop_index("ix_records_business_key")
        for column in ["superseded_by_run_id", "superseded_at", "is_current", "business_key", "source_logical_id", "store_logical_id"]:
            batch.drop_column(column)
    with op.batch_alter_table("ingestion_runs") as batch:
        for column in ["correction_approved_by", "correction_reason", "correction_of_run_id", "supersedes_run_ids"]:
            batch.drop_column(column)
    with op.batch_alter_table("source_definitions") as batch:
        batch.drop_index("ix_sources_logical_effective")
        for column in ["amount_directions", "source_kind", "import_mode", "logical_id"]:
            batch.drop_column(column)
    with op.batch_alter_table("platform_accounts") as batch:
        batch.drop_index("ix_platforms_logical_effective")
        batch.drop_column("logical_id")
    if op.get_bind().dialect.name == "postgresql":
        projection = "enterprise_id, store_id, period_start, grain, row_count, order_count, revenue, refund, platform_fee, advertising_fee, shipping_fee, product_cost, fees, profit"
        op.execute(sa.text(f"CREATE VIEW certified_sales AS SELECT {projection} FROM certified_aggregates"))
        op.execute(sa.text(f"CREATE VIEW certified.sales AS SELECT {projection} FROM certified_aggregates"))
