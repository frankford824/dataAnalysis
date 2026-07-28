"""independent reconciliation and external-agent control domain

Revision ID: b8f0c2d4e6a1
Revises: a7d3c9e41f20
Create Date: 2026-07-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8f0c2d4e6a1"
down_revision: Union[str, None] = "a7d3c9e41f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = [
    "recon_contracts",
    "recon_contract_versions",
    "recon_contract_source_roles",
    "recon_rule_packages",
    "recon_rule_package_versions",
    "recon_rule_items",
    "recon_rule_compile_artifacts",
    "recon_accounting_periods",
    "recon_external_agents",
    "recon_agent_enrollment_tokens",
    "recon_source_connectors",
    "recon_agent_jobs",
    "recon_agent_job_events",
    "recon_discovered_files",
    "recon_source_files",
    "recon_source_rows",
    "recon_rule_decisions",
    "recon_links",
    "recon_differences",
    "recon_review_items",
    "recon_review_decisions",
    "recon_certification_runs",
    "recon_certification_gate_results",
    "recon_certification_versions",
    "recon_certification_heads",
    "recon_adjustment_entries",
    "recon_restatement_versions",
    "recon_evidence_edges",
]


def _tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade() -> None:
    op.create_table(
        "recon_contracts",
        *_tenant_columns(),
        sa.Column("logical_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('active','archived')", name="ck_recon_contract_status"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_contract_logical_key"
        ),
    )
    op.create_table(
        "recon_contract_versions",
        *_tenant_columns(),
        sa.Column("contract_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reporting_currency", sa.String(length=3), nullable=False),
        sa.Column("business_timezone", sa.String(length=64), nullable=False),
        sa.Column("amount_scale", sa.Integer(), nullable=False),
        sa.Column("rounding_mode", sa.String(length=32), nullable=False),
        sa.Column("period_cutoff_policy", sa.String(length=32), nullable=False),
        sa.Column("refund_direction", sa.String(length=16), nullable=False),
        sa.Column("fee_direction", sa.String(length=16), nullable=False),
        sa.Column("tax_inclusion_policy", sa.String(length=32), nullable=False),
        sa.Column("matching_strategy", sa.String(length=32), nullable=False),
        sa.Column("matching_keys", sa.JSON(), nullable=False),
        sa.Column("matching_window_seconds", sa.Integer(), nullable=False),
        sa.Column("max_candidates_per_row", sa.Integer(), nullable=False),
        sa.Column("amount_tolerance", sa.Numeric(20, 4), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("checksum", sa.String(length=64)),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("published_by", sa.String(length=36)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["contract_id"], ["recon_contracts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["published_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "contract_id", "version", name="uq_recon_contract_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_recon_contract_version_status",
        ),
        sa.CheckConstraint(
            "amount_scale BETWEEN 0 AND 4", name="ck_recon_contract_amount_scale"
        ),
        sa.CheckConstraint(
            "rounding_mode IN ('half_even','half_up','down','up')",
            name="ck_recon_contract_rounding",
        ),
        sa.CheckConstraint(
            "period_cutoff_policy IN ('business_day','calendar_day','explicit')",
            name="ck_recon_contract_cutoff",
        ),
        sa.CheckConstraint(
            "refund_direction IN ('negative','positive')",
            name="ck_recon_contract_refund_direction",
        ),
        sa.CheckConstraint(
            "fee_direction IN ('negative','positive')",
            name="ck_recon_contract_fee_direction",
        ),
        sa.CheckConstraint(
            "tax_inclusion_policy IN ('inclusive','exclusive','source_defined')",
            name="ck_recon_contract_tax_policy",
        ),
        sa.CheckConstraint(
            "matching_strategy IN ('exact_key','bounded_window','ordered_stages')",
            name="ck_recon_contract_matching_strategy",
        ),
        sa.CheckConstraint(
            "matching_window_seconds BETWEEN 0 AND 2678400",
            name="ck_recon_contract_matching_window",
        ),
        sa.CheckConstraint(
            "max_candidates_per_row BETWEEN 1 AND 100",
            name="ck_recon_contract_max_candidates",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_recon_contract_effective_range",
        ),
    )
    op.create_index(
        "ix_recon_contract_version_current",
        "recon_contract_versions",
        ["enterprise_id", "contract_id", "status", "effective_from"],
    )
    op.create_table(
        "recon_contract_source_roles",
        *_tenant_columns(),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("role_code", sa.String(length=48), nullable=False),
        sa.Column("source_kind", sa.String(length=48), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("min_files", sa.Integer(), nullable=False),
        sa.Column("business_key_fields", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "contract_version_id",
            "role_code",
            name="uq_recon_contract_source_role",
        ),
        sa.CheckConstraint("min_files >= 0", name="ck_recon_source_role_min_files"),
    )
    op.create_table(
        "recon_rule_packages",
        *_tenant_columns(),
        sa.Column("logical_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_rule_package_logical_key"
        ),
        sa.CheckConstraint(
            "status IN ('active','archived')", name="ck_recon_rule_package_status"
        ),
    )
    op.create_table(
        "recon_rule_package_versions",
        *_tenant_columns(),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("compiled_checksum", sa.String(length=64)),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("published_by", sa.String(length=36)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["package_id"], ["recon_rule_packages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["published_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "package_id", "version", name="uq_recon_rule_package_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft','compiled','published','retired')",
            name="ck_recon_rule_package_version_status",
        ),
    )
    op.create_table(
        "recon_rule_items",
        *_tenant_columns(),
        sa.Column("rule_version_id", sa.String(length=36), nullable=False),
        sa.Column("rule_key", sa.String(length=128), nullable=False),
        sa.Column("rule_type", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["recon_rule_package_versions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "rule_version_id", "rule_key", name="uq_recon_rule_item_key"
        ),
        sa.UniqueConstraint(
            "rule_version_id", "priority", name="uq_recon_rule_item_priority"
        ),
        sa.CheckConstraint(
            "rule_type IN "
            "('exact','contains','prefix','suffix','bounded_regex','field_mapping',"
            "'order_id_extract','amount_direction','bounded_window_link')",
            name="ck_recon_rule_item_type",
        ),
        sa.CheckConstraint(
            "priority BETWEEN 0 AND 100000", name="ck_recon_rule_item_priority"
        ),
    )
    op.create_table(
        "recon_rule_compile_artifacts",
        *_tenant_columns(),
        sa.Column("rule_version_id", sa.String(length=36), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("canonical_rules", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["recon_rule_package_versions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "rule_version_id", "checksum", name="uq_recon_compile_artifact"
        ),
    )
    op.create_table(
        "recon_accounting_periods",
        *_tenant_columns(),
        sa.Column("period_key", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("preclosed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("changed_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "period_key", name="uq_recon_accounting_period"
        ),
        sa.CheckConstraint(
            "state IN ('open','preclosed','closed')",
            name="ck_recon_accounting_period_state",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_recon_period_range"),
    )
    op.create_index(
        "ix_recon_period_state",
        "recon_accounting_periods",
        ["enterprise_id", "state", "starts_at", "ends_at"],
    )

    op.create_table(
        "recon_external_agents",
        *_tenant_columns(),
        sa.Column("machine_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("agent_key_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=64)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_key_hash"),
        sa.UniqueConstraint(
            "enterprise_id", "machine_key", name="uq_recon_external_agent_machine"
        ),
        sa.CheckConstraint(
            "status IN ('offline','online','disabled')",
            name="ck_recon_external_agent_status",
        ),
    )
    op.create_table(
        "recon_agent_enrollment_tokens",
        *_tenant_columns(),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("used_by_agent_id", sa.String(length=36)),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["used_by_agent_id"], ["recon_external_agents.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("token_hash"),
        sa.CheckConstraint(
            "(used_by_agent_id IS NULL AND used_at IS NULL) OR "
            "(used_by_agent_id IS NOT NULL AND used_at IS NOT NULL)",
            name="ck_recon_enrollment_token_use",
        ),
    )
    op.create_index(
        "ix_recon_enrollment_expiry",
        "recon_agent_enrollment_tokens",
        ["enterprise_id", "expires_at"],
    )
    op.create_table(
        "recon_source_connectors",
        *_tenant_columns(),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("logical_key", sa.String(length=128), nullable=False),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=48), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("read_policy", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["recon_external_agents.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_source_connector_key"
        ),
        sa.CheckConstraint(
            "connector_type IN ('directory','pbix_inventory','bi_activity')",
            name="ck_recon_source_connector_type",
        ),
    )
    op.create_table(
        "recon_agent_jobs",
        *_tenant_columns(),
        sa.Column("agent_id", sa.String(length=36)),
        sa.Column("connector_id", sa.String(length=36)),
        sa.Column("job_type", sa.String(length=48), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer()),
        sa.Column("lease_owner", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["recon_external_agents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["connector_id"], ["recon_source_connectors.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "state IN ('queued','leased','running','waiting_review','succeeded','failed','cancelled')",
            name="ck_recon_agent_job_state",
        ),
        sa.CheckConstraint(
            "progress_current >= 0 AND "
            "(progress_total IS NULL OR progress_total >= progress_current)",
            name="ck_recon_agent_job_progress",
        ),
    )
    op.create_index(
        "ix_recon_agent_job_claim",
        "recon_agent_jobs",
        ["enterprise_id", "state", "priority", "created_at"],
    )
    op.create_table(
        "recon_agent_job_events",
        *_tenant_columns(),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["recon_agent_jobs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("job_id", "sequence", name="uq_recon_agent_job_event"),
        sa.CheckConstraint(
            "sequence >= 0", name="ck_recon_agent_job_event_sequence"
        ),
    )
    op.create_table(
        "recon_discovered_files",
        *_tenant_columns(),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("path_key", sa.String(length=64), nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("observed_mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("safety_flags", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_id"], ["recon_source_connectors.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "connector_id", "path_key", name="uq_recon_discovered_file_path"
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name="ck_recon_discovered_file_size"
        ),
        sa.CheckConstraint(
            "status IN ('discovered','stable','skipped','queued','processed','failed','missing')",
            name="ck_recon_discovered_file_status",
        ),
    )
    op.create_index(
        "ix_recon_discovered_file_status",
        "recon_discovered_files",
        ["enterprise_id", "connector_id", "status", "last_seen_at"],
    )

    op.create_table(
        "recon_source_files",
        *_tenant_columns(),
        sa.Column("connector_id", sa.String(length=36)),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("observed_mtime", sa.DateTime(timezone=True)),
        sa.Column("source_kind", sa.String(length=48), nullable=False),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_id"], ["recon_source_connectors.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "sha256", name="uq_recon_source_file_sha256"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_recon_source_file_size"),
    )
    op.create_table(
        "recon_source_rows",
        *_tenant_columns(),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("member_name", sa.Text()),
        sa.Column("sheet_name", sa.Text()),
        sa.Column("row_locator", sa.String(length=256), nullable=False),
        sa.Column("row_number", sa.Integer()),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_amount_text", sa.Text()),
        sa.Column("raw_currency", sa.String(length=3)),
        sa.Column("normalized_amount", sa.Numeric(20, 4)),
        sa.Column("normalization_rule_version_id", sa.String(length=36)),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("business_key", sa.String(length=256)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["recon_source_files.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["normalization_rule_version_id"],
            ["recon_rule_package_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_file_id", "row_locator", name="uq_recon_source_row_locator"
        ),
    )
    op.create_index(
        "ix_recon_source_row_business_key",
        "recon_source_rows",
        ["enterprise_id", "business_key", "occurred_at"],
    )
    op.create_table(
        "recon_rule_decisions",
        *_tenant_columns(),
        sa.Column("source_row_id", sa.String(length=36), nullable=False),
        sa.Column("rule_item_id", sa.String(length=36), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_row_id"], ["recon_source_rows.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rule_item_id"], ["recon_rule_items.id"], ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "recon_links",
        *_tenant_columns(),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("left_row_id", sa.String(length=36), nullable=False),
        sa.Column("right_row_id", sa.String(length=36), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column("match_key", sa.String(length=256), nullable=False),
        sa.Column("amount_difference", sa.Numeric(20, 4), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["left_row_id"], ["recon_source_rows.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["right_row_id"], ["recon_source_rows.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "contract_version_id",
            "period_id",
            "left_row_id",
            "right_row_id",
            name="uq_recon_link_pair",
        ),
        sa.CheckConstraint(
            "state IN ('matched','ambiguous','rejected')", name="ck_recon_link_state"
        ),
    )
    op.create_index(
        "ix_recon_link_period_state",
        "recon_links",
        ["enterprise_id", "period_id", "state"],
    )
    op.create_table(
        "recon_differences",
        *_tenant_columns(),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("source_row_id", sa.String(length=36)),
        sa.Column("difference_type", sa.String(length=48), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("explanation_code", sa.String(length=64)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_row_id"], ["recon_source_rows.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('open','explained','adjusted','rejected')",
            name="ck_recon_difference_status",
        ),
    )
    op.create_index(
        "ix_recon_difference_period_status",
        "recon_differences",
        ["enterprise_id", "period_id", "status"],
    )

    op.create_table(
        "recon_review_items",
        *_tenant_columns(),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("assigned_to", sa.String(length=36)),
        sa.Column("requested_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["assigned_to"], ["user_accounts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id",
            "subject_type",
            "subject_id",
            name="uq_recon_review_subject",
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','decided','cancelled')",
            name="ck_recon_review_item_status",
        ),
        sa.CheckConstraint(
            "risk_level IN ('normal','high','critical')",
            name="ck_recon_review_risk",
        ),
    )
    op.create_index(
        "ix_recon_review_queue",
        "recon_review_items",
        ["enterprise_id", "status", "risk_level", "created_at"],
    )
    op.create_table(
        "recon_review_decisions",
        *_tenant_columns(),
        sa.Column("review_item_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text()),
        sa.Column("decided_by", sa.String(length=36), nullable=False),
        sa.Column("disposition", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_item_id"], ["recon_review_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "decision IN ('accept','reject','escalate')",
            name="ck_recon_review_decision",
        ),
    )

    op.create_table(
        "recon_certification_runs",
        *_tenant_columns(),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("rule_version_id", sa.String(length=36), nullable=False),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("source_set_checksum", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("proposed_by", sa.String(length=36), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["recon_rule_package_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "state IN ('draft','submitted','eligible','certified','rejected')",
            name="ck_recon_certification_run_state",
        ),
    )
    op.create_index(
        "ix_recon_cert_run_scope",
        "recon_certification_runs",
        [
            "enterprise_id",
            "contract_version_id",
            "period_id",
            "scope_key",
            "state",
        ],
    )
    op.create_table(
        "recon_certification_gate_results",
        *_tenant_columns(),
        sa.Column("certification_run_id", sa.String(length=36), nullable=False),
        sa.Column("gate_code", sa.String(length=64), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actual_value", sa.Text()),
        sa.Column("expected_value", sa.Text()),
        sa.Column("difference", sa.Text()),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["certification_run_id"],
            ["recon_certification_runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "certification_run_id",
            "gate_code",
            name="uq_recon_certification_gate",
        ),
        sa.CheckConstraint(
            "status IN ('pending','passed','failed','not_applicable')",
            name="ck_recon_certification_gate_status",
        ),
    )
    op.create_table(
        "recon_certification_versions",
        *_tenant_columns(),
        sa.Column("certification_run_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("rule_version_id", sa.String(length=36), nullable=False),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["certification_run_id"],
            ["recon_certification_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["recon_rule_package_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("certification_run_id"),
        sa.UniqueConstraint(
            "enterprise_id",
            "contract_version_id",
            "period_id",
            "scope_key",
            "version",
            name="uq_recon_certification_scope_version",
        ),
    )
    op.create_table(
        "recon_certification_heads",
        *_tenant_columns(),
        sa.Column("contract_id", sa.String(length=36), nullable=False),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"], ["recon_contracts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["current_version_id"],
            ["recon_certification_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("current_version_id"),
        sa.UniqueConstraint(
            "enterprise_id",
            "contract_id",
            "period_id",
            "scope_key",
            name="uq_recon_certification_head_scope",
        ),
    )
    op.create_index(
        "ix_recon_certification_head_lookup",
        "recon_certification_heads",
        ["enterprise_id", "period_id", "scope_key"],
    )
    op.create_table(
        "recon_adjustment_entries",
        *_tenant_columns(),
        sa.Column("period_id", sa.String(length=36), nullable=False),
        sa.Column("contract_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36)),
        sa.Column("base_certification_version_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("submitted_by", sa.String(length=36)),
        sa.Column("approved_by", sa.String(length=36)),
        sa.ForeignKeyConstraint(
            ["period_id"], ["recon_accounting_periods.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["recon_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["recon_source_files.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["base_certification_version_id"],
            ["recon_certification_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "state IN ('draft','submitted','approved','rejected')",
            name="ck_recon_adjustment_state",
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR submitted_by IS NULL OR approved_by <> submitted_by",
            name="ck_recon_adjustment_maker_checker",
        ),
    )
    op.create_index(
        "ix_recon_adjustment_period_state",
        "recon_adjustment_entries",
        ["enterprise_id", "period_id", "state"],
    )
    op.create_table(
        "recon_restatement_versions",
        *_tenant_columns(),
        sa.Column("adjustment_entry_id", sa.String(length=36), nullable=False),
        sa.Column("base_certification_version_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["adjustment_entry_id"],
            ["recon_adjustment_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_certification_version_id"],
            ["recon_certification_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["user_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("adjustment_entry_id"),
        sa.UniqueConstraint(
            "base_certification_version_id",
            "version",
            name="uq_recon_restatement_version",
        ),
    )
    op.create_table(
        "recon_evidence_edges",
        *_tenant_columns(),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=48), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "enterprise_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "relation",
            name="uq_recon_evidence_edge",
        ),
        sa.CheckConstraint(
            "source_type IN "
            "('source_file','source_row','rule_decision','recon_link',"
            "'recon_difference','review_decision','certification_version',"
            "'adjustment_entry','restatement_version')",
            name="ck_recon_evidence_source_type",
        ),
        sa.CheckConstraint(
            "target_type IN "
            "('source_file','source_row','rule_decision','recon_link',"
            "'recon_difference','review_decision','certification_version',"
            "'adjustment_entry','restatement_version')",
            name="ck_recon_evidence_target_type",
        ),
    )
    op.create_index(
        "ix_recon_evidence_source",
        "recon_evidence_edges",
        ["enterprise_id", "source_type", "source_id"],
    )
    op.create_index(
        "ix_recon_evidence_target",
        "recon_evidence_edges",
        ["enterprise_id", "target_type", "target_id"],
    )

    # PostgreSQL does not create indexes for foreign keys. Add the principal
    # join/retention indexes explicitly; composite indexes above cover the rest.
    fk_indexes = {
        "recon_contract_versions": ["contract_id"],
        "recon_contract_source_roles": ["contract_version_id"],
        "recon_rule_package_versions": ["package_id"],
        "recon_rule_items": ["rule_version_id"],
        "recon_rule_compile_artifacts": ["rule_version_id"],
        "recon_source_connectors": ["agent_id"],
        "recon_agent_jobs": ["agent_id", "connector_id"],
        "recon_agent_job_events": ["job_id"],
        "recon_discovered_files": ["connector_id"],
        "recon_source_files": ["connector_id"],
        "recon_source_rows": ["source_file_id", "normalization_rule_version_id"],
        "recon_rule_decisions": ["source_row_id", "rule_item_id"],
        "recon_links": ["contract_version_id", "period_id"],
        "recon_differences": ["contract_version_id", "period_id", "source_row_id"],
        "recon_review_items": ["assigned_to"],
        "recon_review_decisions": ["review_item_id"],
        "recon_certification_runs": [
            "contract_version_id",
            "rule_version_id",
            "period_id",
        ],
        "recon_certification_gate_results": ["certification_run_id"],
        "recon_certification_versions": [
            "contract_version_id",
            "rule_version_id",
            "period_id",
        ],
        "recon_adjustment_entries": ["period_id", "contract_version_id"],
    }
    for table, columns in fk_indexes.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])

    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            op.execute(
                sa.text(
                    f"CREATE POLICY {table}_tenant_isolation ON {table} "
                    "USING (enterprise_id = current_setting('app.current_enterprise_id', true)) "
                    "WITH CHECK (enterprise_id = current_setting('app.current_enterprise_id', true))"
                )
            )

        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_immutable() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
                END;
                $$;
                """
            )
        )
        for table in [
            "recon_certification_versions",
            "recon_restatement_versions",
            "recon_rule_compile_artifacts",
            "recon_evidence_edges",
            "recon_agent_job_events",
        ]:
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_immutable "
                    f"BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION recon_guard_immutable()"
                )
            )

        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_contract_version() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  IF OLD.status IN ('published','retired') THEN
                    RAISE EXCEPTION 'published contract versions are immutable';
                  END IF;
                  IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_contract_version_immutable
                BEFORE UPDATE OR DELETE ON recon_contract_versions
                FOR EACH ROW EXECUTE FUNCTION recon_guard_contract_version();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_rule_version() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  IF OLD.status IN ('published','retired') THEN
                    RAISE EXCEPTION 'published rule versions are immutable';
                  END IF;
                  IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_rule_version_immutable
                BEFORE UPDATE OR DELETE ON recon_rule_package_versions
                FOR EACH ROW EXECUTE FUNCTION recon_guard_rule_version();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_rule_item() RETURNS trigger
                LANGUAGE plpgsql AS $$
                DECLARE parent_status text;
                BEGIN
                  SELECT status INTO parent_status
                  FROM recon_rule_package_versions
                  WHERE id = CASE WHEN TG_OP = 'INSERT'
                                  THEN NEW.rule_version_id
                                  ELSE OLD.rule_version_id END;
                  IF parent_status IN ('published','retired') THEN
                    RAISE EXCEPTION 'items of published rule versions are immutable';
                  END IF;
                  IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_rule_item_immutable
                BEFORE INSERT OR UPDATE OR DELETE ON recon_rule_items
                FOR EACH ROW EXECUTE FUNCTION recon_guard_rule_item();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_period_transition() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'accounting periods cannot be deleted';
                  END IF;
                  IF OLD.state = 'closed' THEN
                    RAISE EXCEPTION 'closed accounting periods are immutable';
                  END IF;
                  IF OLD.state = 'open' AND NEW.state <> 'preclosed' THEN
                    RAISE EXCEPTION 'period must transition open to preclosed';
                  END IF;
                  IF OLD.state = 'preclosed' AND NEW.state <> 'closed' THEN
                    RAISE EXCEPTION 'period must transition preclosed to closed';
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_accounting_period_transition
                BEFORE UPDATE OR DELETE ON recon_accounting_periods
                FOR EACH ROW EXECUTE FUNCTION recon_guard_period_transition();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_certification_insert() RETURNS trigger
                LANGUAGE plpgsql AS $$
                DECLARE
                  period_state text;
                  proposer text;
                  run_state text;
                  run_enterprise text;
                  contract_status text;
                  rule_status text;
                  missing_gates integer;
                BEGIN
                  SELECT state INTO period_state
                  FROM recon_accounting_periods WHERE id = NEW.period_id;
                  IF period_state = 'closed' THEN
                    RAISE EXCEPTION 'closed periods accept adjustments only';
                  END IF;
                  SELECT proposed_by, state, enterprise_id
                    INTO proposer, run_state, run_enterprise
                  FROM recon_certification_runs WHERE id = NEW.certification_run_id;
                  IF run_enterprise <> NEW.enterprise_id THEN
                    RAISE EXCEPTION 'certification run enterprise mismatch';
                  END IF;
                  IF run_state <> 'eligible' THEN
                    RAISE EXCEPTION 'certification run is not eligible';
                  END IF;
                  IF proposer = NEW.approved_by THEN
                    RAISE EXCEPTION 'maker and checker must be different users';
                  END IF;
                  SELECT status INTO contract_status
                  FROM recon_contract_versions WHERE id = NEW.contract_version_id;
                  SELECT status INTO rule_status
                  FROM recon_rule_package_versions WHERE id = NEW.rule_version_id;
                  IF contract_status <> 'published' OR rule_status <> 'published' THEN
                    RAISE EXCEPTION 'contract and rules must be published';
                  END IF;
                  SELECT count(*) INTO missing_gates
                  FROM recon_certification_gate_results
                  WHERE certification_run_id = NEW.certification_run_id
                    AND required
                    AND status <> 'passed';
                  IF missing_gates > 0 OR NOT EXISTS (
                    SELECT 1 FROM recon_certification_gate_results
                    WHERE certification_run_id = NEW.certification_run_id AND required
                  ) THEN
                    RAISE EXCEPTION 'required certification gates are not satisfied';
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_certification_insert_guard
                BEFORE INSERT ON recon_certification_versions
                FOR EACH ROW EXECUTE FUNCTION recon_guard_certification_insert();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_certification_head() RETURNS trigger
                LANGUAGE plpgsql AS $$
                DECLARE period_state text;
                BEGIN
                  SELECT state INTO period_state FROM recon_accounting_periods
                  WHERE id = CASE WHEN TG_OP = 'INSERT'
                                  THEN NEW.period_id
                                  ELSE OLD.period_id END;
                  IF period_state = 'closed' THEN
                    RAISE EXCEPTION 'closed certification heads cannot change';
                  END IF;
                  IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_certification_head_guard
                BEFORE INSERT OR UPDATE OR DELETE ON recon_certification_heads
                FOR EACH ROW EXECUTE FUNCTION recon_guard_certification_head();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE FUNCTION recon_guard_adjustment() RETURNS trigger
                LANGUAGE plpgsql AS $$
                DECLARE period_state text;
                BEGIN
                  SELECT state INTO period_state FROM recon_accounting_periods
                  WHERE id = CASE WHEN TG_OP = 'INSERT'
                                  THEN NEW.period_id
                                  ELSE OLD.period_id END;
                  IF period_state <> 'closed' THEN
                    RAISE EXCEPTION 'adjustments are reserved for closed periods';
                  END IF;
                  IF TG_OP <> 'INSERT' AND OLD.state IN ('approved','rejected') THEN
                    RAISE EXCEPTION 'final adjustment entries are immutable';
                  END IF;
                  IF TG_OP <> 'DELETE' AND NEW.approved_by IS NOT NULL
                     AND NEW.submitted_by = NEW.approved_by THEN
                    RAISE EXCEPTION 'maker and checker must be different users';
                  END IF;
                  IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                  END IF;
                  RETURN NEW;
                END;
                $$;
                CREATE TRIGGER recon_adjustment_guard
                BEFORE INSERT OR UPDATE OR DELETE ON recon_adjustment_entries
                FOR EACH ROW EXECUTE FUNCTION recon_guard_adjustment();
                """
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for function in [
            "recon_guard_adjustment",
            "recon_guard_certification_head",
            "recon_guard_certification_insert",
            "recon_guard_period_transition",
            "recon_guard_rule_item",
            "recon_guard_rule_version",
            "recon_guard_contract_version",
            "recon_guard_immutable",
        ]:
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}() CASCADE"))

    for table in reversed(TABLES):
        op.drop_table(table)
