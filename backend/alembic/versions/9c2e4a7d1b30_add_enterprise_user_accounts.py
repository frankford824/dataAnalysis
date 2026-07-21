"""add enterprise user accounts

Revision ID: 9c2e4a7d1b30
Revises: 5a1775288b65
Create Date: 2026-07-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c2e4a7d1b30"
down_revision: Union[str, None] = "5a1775288b65"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("store_ids", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("enterprise_id", "email", "version", name="uq_user_email_version"),
    )
    op.create_index(op.f("ix_user_accounts_enterprise_id"), "user_accounts", ["enterprise_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE user_accounts ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(
            "CREATE POLICY user_accounts_tenant_isolation ON user_accounts "
            "USING (enterprise_id = current_setting('app.current_enterprise_id', true)) "
            "WITH CHECK (enterprise_id = current_setting('app.current_enterprise_id', true))"
        ))


def downgrade() -> None:
    op.drop_index(op.f("ix_user_accounts_enterprise_id"), table_name="user_accounts")
    op.drop_table("user_accounts")
