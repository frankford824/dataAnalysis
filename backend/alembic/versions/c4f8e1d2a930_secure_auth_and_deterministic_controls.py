"""secure authentication and deterministic controls

Revision ID: c4f8e1d2a930
Revises: 9c2e4a7d1b30
Create Date: 2026-07-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4f8e1d2a930"
down_revision: Union[str, None] = "9c2e4a7d1b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("logical_id", sa.String(length=36), nullable=True))
    op.execute(sa.text("UPDATE stores SET logical_id = id WHERE logical_id IS NULL"))
    with op.batch_alter_table("stores") as batch:
        batch.alter_column("logical_id", nullable=False)
        batch.create_index("ix_stores_logical_effective", ["enterprise_id", "logical_id", "effective_from"])

    with op.batch_alter_table("user_accounts") as batch:
        batch.drop_constraint("uq_user_email_version", type_="unique")
        batch.create_unique_constraint("uq_user_email_global", ["email"])
        batch.add_column(sa.Column("password_hash", sa.Text(), nullable=True))
        batch.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    # Existing rows cannot be safely assigned a password. They remain inactive until reset by an administrator.
    op.execute(sa.text("UPDATE user_accounts SET password_hash = 'disabled', password_changed_at = created_at, status = 'inactive' WHERE password_hash IS NULL"))
    with op.batch_alter_table("user_accounts") as batch:
        batch.alter_column("password_hash", nullable=False)
        batch.alter_column("password_changed_at", nullable=False)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    with op.batch_alter_table("upload_sessions") as batch:
        batch.alter_column("source_definition_id", nullable=True)
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.add_column(sa.Column("source_config_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("rule_config_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("semantic_model_id", sa.String(length=36), nullable=True))
    op.execute(sa.text("UPDATE ingestion_runs SET source_config_id = source_definition_id, rule_config_id = source_definition_id WHERE source_config_id IS NULL"))
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.alter_column("source_config_id", nullable=False)
        batch.alter_column("rule_config_id", nullable=False)
        batch.drop_constraint("uq_ingestion_file", type_="unique")
        batch.create_unique_constraint("uq_ingestion_file_global", ["enterprise_id", "source_sha256"])

    for column in ["platform_fee", "advertising_fee", "shipping_fee", "product_cost"]:
        op.add_column("certified_aggregates", sa.Column(column, sa.Numeric(20, 4), nullable=False, server_default="0"))

    op.create_table(
        "problems",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("ingestion_run_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("technical_detail", sa.JSON(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_problems_enterprise_id", "problems", ["enterprise_id"])
    op.create_index("ix_problems_ingestion_run_id", "problems", ["ingestion_run_id"])
    op.create_index("ix_problem_tenant_status", "problems", ["enterprise_id", "status", "created_at"])
    op.create_table(
        "review_queue",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("problem_id", sa.String(length=36), nullable=False),
        sa.Column("assigned_to", sa.String(length=36), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_to"], ["user_accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("problem_id"),
    )
    op.create_index("ix_review_queue_enterprise_id", "review_queue", ["enterprise_id"])

    if op.get_bind().dialect.name == "postgresql":
        # Login must locate a user before tenant context exists; application RBAC protects this table.
        op.execute(sa.text("DROP POLICY IF EXISTS user_accounts_tenant_isolation ON user_accounts"))
        op.execute(sa.text("ALTER TABLE user_accounts DISABLE ROW LEVEL SECURITY"))
        for table in ["problems", "review_queue"]:
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            op.execute(sa.text(
                f"CREATE POLICY {table}_tenant_isolation ON {table} "
                "USING (enterprise_id = current_setting('app.current_enterprise_id', true)) "
                "WITH CHECK (enterprise_id = current_setting('app.current_enterprise_id', true))"
            ))
        op.execute(sa.text("DROP VIEW IF EXISTS certified.sales"))
        op.execute(sa.text("DROP VIEW IF EXISTS certified_sales"))
        projection = "enterprise_id, store_id, period_start, grain, row_count, order_count, revenue, refund, platform_fee, advertising_fee, shipping_fee, product_cost, fees, profit"
        op.execute(sa.text(f"CREATE VIEW certified_sales AS SELECT {projection} FROM certified_aggregates"))
        op.execute(sa.text(f"CREATE VIEW certified.sales AS SELECT {projection} FROM certified_aggregates"))
        op.execute(sa.text("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='analytics_reader') THEN GRANT SELECT ON certified.sales TO analytics_reader; END IF; END $$"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP VIEW IF EXISTS certified.sales"))
        op.execute(sa.text("DROP VIEW IF EXISTS certified_sales"))
    op.drop_index("ix_review_queue_enterprise_id", table_name="review_queue")
    op.drop_table("review_queue")
    op.drop_index("ix_problem_tenant_status", table_name="problems")
    op.drop_index("ix_problems_ingestion_run_id", table_name="problems")
    op.drop_index("ix_problems_enterprise_id", table_name="problems")
    op.drop_table("problems")
    for column in ["product_cost", "shipping_fee", "advertising_fee", "platform_fee"]:
        op.drop_column("certified_aggregates", column)
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.drop_constraint("uq_ingestion_file_global", type_="unique")
        batch.create_unique_constraint("uq_ingestion_file", ["enterprise_id", "source_definition_id", "source_sha256"])
        batch.drop_column("semantic_model_id")
        batch.drop_column("rule_config_id")
        batch.drop_column("source_config_id")
    with op.batch_alter_table("upload_sessions") as batch:
        batch.alter_column("source_definition_id", nullable=False)
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    with op.batch_alter_table("user_accounts") as batch:
        batch.drop_constraint("uq_user_email_global", type_="unique")
        batch.create_unique_constraint("uq_user_email_version", ["enterprise_id", "email", "version"])
        batch.drop_column("last_login_at")
        batch.drop_column("password_changed_at")
        batch.drop_column("must_change_password")
        batch.drop_column("password_hash")
    with op.batch_alter_table("stores") as batch:
        batch.drop_index("ix_stores_logical_effective")
        batch.drop_column("logical_id")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE user_accounts ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(
            "CREATE POLICY user_accounts_tenant_isolation ON user_accounts "
            "USING (enterprise_id = current_setting('app.current_enterprise_id', true)) "
            "WITH CHECK (enterprise_id = current_setting('app.current_enterprise_id', true))"
        ))
