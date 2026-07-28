from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RULE_TYPES = (
    "exact",
    "contains",
    "prefix",
    "suffix",
    "bounded_regex",
    "field_mapping",
    "order_id_extract",
    "amount_direction",
    "bounded_window_link",
)

EVIDENCE_TYPES = (
    "source_file",
    "source_row",
    "rule_decision",
    "recon_link",
    "recon_difference",
    "review_decision",
    "certification_version",
    "adjustment_entry",
    "restatement_version",
)


class TenantRecord:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(
        ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ReconContract(TenantRecord, Base):
    __tablename__ = "recon_contracts"

    logical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_contract_logical_key"
        ),
        CheckConstraint(
            "status IN ('active','archived')", name="ck_recon_contract_status"
        ),
    )


class ReconContractVersion(TenantRecord, Base):
    __tablename__ = "recon_contract_versions"

    contract_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contracts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    reporting_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    business_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_scale: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    rounding_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    period_cutoff_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    refund_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    fee_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    tax_inclusion_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    matching_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    matching_keys: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matching_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_candidates_per_row: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_tolerance: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), default=Decimal("0"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "contract_id", "version", name="uq_recon_contract_version"
        ),
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_recon_contract_version_status",
        ),
        CheckConstraint(
            "amount_scale BETWEEN 0 AND 4", name="ck_recon_contract_amount_scale"
        ),
        CheckConstraint(
            "rounding_mode IN ('half_even','half_up','down','up')",
            name="ck_recon_contract_rounding",
        ),
        CheckConstraint(
            "period_cutoff_policy IN ('business_day','calendar_day','explicit')",
            name="ck_recon_contract_cutoff",
        ),
        CheckConstraint(
            "refund_direction IN ('negative','positive')",
            name="ck_recon_contract_refund_direction",
        ),
        CheckConstraint(
            "fee_direction IN ('negative','positive')",
            name="ck_recon_contract_fee_direction",
        ),
        CheckConstraint(
            "tax_inclusion_policy IN ('inclusive','exclusive','source_defined')",
            name="ck_recon_contract_tax_policy",
        ),
        CheckConstraint(
            "matching_strategy IN ('exact_key','bounded_window','ordered_stages')",
            name="ck_recon_contract_matching_strategy",
        ),
        CheckConstraint(
            "matching_window_seconds BETWEEN 0 AND 2678400",
            name="ck_recon_contract_matching_window",
        ),
        CheckConstraint(
            "max_candidates_per_row BETWEEN 1 AND 100",
            name="ck_recon_contract_max_candidates",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_recon_contract_effective_range",
        ),
        Index(
            "ix_recon_contract_version_current",
            "enterprise_id",
            "contract_id",
            "status",
            "effective_from",
        ),
    )


class ContractSourceRole(TenantRecord, Base):
    __tablename__ = "recon_contract_source_roles"

    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_code: Mapped[str] = mapped_column(String(48), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_files: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    business_key_fields: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "role_code",
            name="uq_recon_contract_source_role",
        ),
        CheckConstraint("min_files >= 0", name="ck_recon_source_role_min_files"),
    )


class RulePackage(TenantRecord, Base):
    __tablename__ = "recon_rule_packages"

    logical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_rule_package_logical_key"
        ),
        CheckConstraint(
            "status IN ('active','archived')", name="ck_recon_rule_package_status"
        ),
    )


class RulePackageVersion(TenantRecord, Base):
    __tablename__ = "recon_rule_package_versions"

    package_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    compiled_checksum: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "package_id", "version", name="uq_recon_rule_package_version"
        ),
        CheckConstraint(
            "status IN ('draft','compiled','published','retired')",
            name="ck_recon_rule_package_version_status",
        ),
    )


class RuleItem(TenantRecord, Base):
    __tablename__ = "recon_rule_items"

    rule_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_package_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_key: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "rule_version_id", "rule_key", name="uq_recon_rule_item_key"
        ),
        UniqueConstraint(
            "rule_version_id", "priority", name="uq_recon_rule_item_priority"
        ),
        CheckConstraint(
            "rule_type IN "
            "('exact','contains','prefix','suffix','bounded_regex','field_mapping',"
            "'order_id_extract','amount_direction','bounded_window_link')",
            name="ck_recon_rule_item_type",
        ),
        CheckConstraint(
            "priority BETWEEN 0 AND 100000", name="ck_recon_rule_item_priority"
        ),
    )


class RuleCompileArtifact(TenantRecord, Base):
    __tablename__ = "recon_rule_compile_artifacts"

    rule_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_package_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "rule_version_id", "checksum", name="uq_recon_compile_artifact"
        ),
    )


class AccountingPeriod(TenantRecord, Base):
    __tablename__ = "recon_accounting_periods"

    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    preclosed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "period_key", name="uq_recon_accounting_period"
        ),
        CheckConstraint(
            "state IN ('open','preclosed','closed')",
            name="ck_recon_accounting_period_state",
        ),
        CheckConstraint("ends_at > starts_at", name="ck_recon_period_range"),
        Index(
            "ix_recon_period_state", "enterprise_id", "state", "starts_at", "ends_at"
        ),
    )


class SourceFile(TenantRecord, Base):
    __tablename__ = "recon_source_files"

    connector_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_source_connectors.id", ondelete="RESTRICT"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "sha256", name="uq_recon_source_file_sha256"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_recon_source_file_size"),
    )


class SourceRow(TenantRecord, Base):
    __tablename__ = "recon_source_rows"

    source_file_id: Mapped[str] = mapped_column(
        ForeignKey("recon_source_files.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    member_name: Mapped[str | None] = mapped_column(Text)
    sheet_name: Mapped[str | None] = mapped_column(Text)
    row_locator: Mapped[str] = mapped_column(String(256), nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_amount_text: Mapped[str | None] = mapped_column(Text)
    raw_currency: Mapped[str | None] = mapped_column(String(3))
    normalized_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    normalization_rule_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_rule_package_versions.id", ondelete="RESTRICT"), index=True
    )
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_key: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "source_file_id", "row_locator", name="uq_recon_source_row_locator"
        ),
        Index(
            "ix_recon_source_row_business_key",
            "enterprise_id",
            "business_key",
            "occurred_at",
        ),
    )


class RuleDecision(TenantRecord, Base):
    __tablename__ = "recon_rule_decisions"

    source_row_id: Mapped[str] = mapped_column(
        ForeignKey("recon_source_rows.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rule_item_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)


class ReconLink(TenantRecord, Base):
    __tablename__ = "recon_links"

    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    left_row_id: Mapped[str] = mapped_column(
        ForeignKey("recon_source_rows.id", ondelete="RESTRICT"), nullable=False
    )
    right_row_id: Mapped[str] = mapped_column(
        ForeignKey("recon_source_rows.id", ondelete="RESTRICT"), nullable=False
    )
    link_type: Mapped[str] = mapped_column(String(32), nullable=False)
    match_key: Mapped[str] = mapped_column(String(256), nullable=False)
    amount_difference: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), default=Decimal("0"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "period_id",
            "left_row_id",
            "right_row_id",
            name="uq_recon_link_pair",
        ),
        CheckConstraint(
            "state IN ('matched','ambiguous','rejected')", name="ck_recon_link_state"
        ),
        Index(
            "ix_recon_link_period_state",
            "enterprise_id",
            "period_id",
            "state",
        ),
    )


class ReconDifference(TenantRecord, Base):
    __tablename__ = "recon_differences"

    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_source_rows.id", ondelete="RESTRICT"), index=True
    )
    difference_type: Mapped[str] = mapped_column(String(48), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    explanation_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','explained','adjusted','rejected')",
            name="ck_recon_difference_status",
        ),
        Index(
            "ix_recon_difference_period_status",
            "enterprise_id",
            "period_id",
            "status",
        ),
    )


class ReviewItem(TenantRecord, Base):
    __tablename__ = "recon_review_items"

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="SET NULL"), index=True
    )
    requested_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "subject_type",
            "subject_id",
            name="uq_recon_review_subject",
        ),
        CheckConstraint(
            "status IN ('pending','claimed','decided','cancelled')",
            name="ck_recon_review_item_status",
        ),
        CheckConstraint(
            "risk_level IN ('normal','high','critical')",
            name="ck_recon_review_risk",
        ),
        Index(
            "ix_recon_review_queue",
            "enterprise_id",
            "status",
            "risk_level",
            "created_at",
        ),
    )


class ReviewDecision(TenantRecord, Base):
    __tablename__ = "recon_review_decisions"

    review_item_id: Mapped[str] = mapped_column(
        ForeignKey("recon_review_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    disposition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('accept','reject','escalate')",
            name="ck_recon_review_decision",
        ),
    )


class CertificationRun(TenantRecord, Base):
    __tablename__ = "recon_certification_runs"

    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rule_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_package_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    source_set_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    proposed_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','submitted','eligible','certified','rejected')",
            name="ck_recon_certification_run_state",
        ),
        Index(
            "ix_recon_cert_run_scope",
            "enterprise_id",
            "contract_version_id",
            "period_id",
            "scope_key",
            "state",
        ),
    )


class CertificationGateResult(TenantRecord, Base):
    __tablename__ = "recon_certification_gate_results"

    certification_run_id: Mapped[str] = mapped_column(
        ForeignKey("recon_certification_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gate_code: Mapped[str] = mapped_column(String(64), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actual_value: Mapped[str | None] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(Text)
    difference: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "certification_run_id",
            "gate_code",
            name="uq_recon_certification_gate",
        ),
        CheckConstraint(
            "status IN ('pending','passed','failed','not_applicable')",
            name="ck_recon_certification_gate_status",
        ),
    )


class CertificationVersion(TenantRecord, Base):
    __tablename__ = "recon_certification_versions"

    certification_run_id: Mapped[str] = mapped_column(
        ForeignKey("recon_certification_runs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rule_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_rule_package_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "contract_version_id",
            "period_id",
            "scope_key",
            "version",
            name="uq_recon_certification_scope_version",
        ),
    )


class CertificationHead(TenantRecord, Base):
    __tablename__ = "recon_certification_heads"

    contract_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contracts.id", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    current_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_certification_versions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "contract_id",
            "period_id",
            "scope_key",
            name="uq_recon_certification_head_scope",
        ),
        Index(
            "ix_recon_certification_head_lookup",
            "enterprise_id",
            "period_id",
            "scope_key",
        ),
    )


class AdjustmentEntry(TenantRecord, Base):
    __tablename__ = "recon_adjustment_entries"

    period_id: Mapped[str] = mapped_column(
        ForeignKey("recon_accounting_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_source_files.id", ondelete="RESTRICT")
    )
    base_certification_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_certification_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_by: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT")
    )
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','submitted','approved','rejected')",
            name="ck_recon_adjustment_state",
        ),
        CheckConstraint(
            "approved_by IS NULL OR submitted_by IS NULL OR approved_by <> submitted_by",
            name="ck_recon_adjustment_maker_checker",
        ),
        Index(
            "ix_recon_adjustment_period_state",
            "enterprise_id",
            "period_id",
            "state",
        ),
    )


class RestatementVersion(TenantRecord, Base):
    __tablename__ = "recon_restatement_versions"

    adjustment_entry_id: Mapped[str] = mapped_column(
        ForeignKey("recon_adjustment_entries.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    base_certification_version_id: Mapped[str] = mapped_column(
        ForeignKey("recon_certification_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "base_certification_version_id",
            "version",
            name="uq_recon_restatement_version",
        ),
    )


class EvidenceEdge(TenantRecord, Base):
    __tablename__ = "recon_evidence_edges"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relation: Mapped[str] = mapped_column(String(48), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "relation",
            name="uq_recon_evidence_edge",
        ),
        CheckConstraint(
            "source_type IN "
            "('source_file','source_row','rule_decision','recon_link',"
            "'recon_difference','review_decision','certification_version',"
            "'adjustment_entry','restatement_version')",
            name="ck_recon_evidence_source_type",
        ),
        CheckConstraint(
            "target_type IN "
            "('source_file','source_row','rule_decision','recon_link',"
            "'recon_difference','review_decision','certification_version',"
            "'adjustment_entry','restatement_version')",
            name="ck_recon_evidence_target_type",
        ),
        Index(
            "ix_recon_evidence_source",
            "enterprise_id",
            "source_type",
            "source_id",
        ),
        Index(
            "ix_recon_evidence_target",
            "enterprise_id",
            "target_type",
            "target_id",
        ),
    )


class ExternalAgent(TenantRecord, Base):
    __tablename__ = "recon_external_agents"

    machine_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), default="offline", nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "machine_key", name="uq_recon_external_agent_machine"
        ),
        CheckConstraint(
            "status IN ('offline','online','disabled')",
            name="ck_recon_external_agent_status",
        ),
    )


class AgentEnrollmentToken(TenantRecord, Base):
    __tablename__ = "recon_agent_enrollment_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    used_by_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_external_agents.id", ondelete="RESTRICT")
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(used_by_agent_id IS NULL AND used_at IS NULL) OR "
            "(used_by_agent_id IS NOT NULL AND used_at IS NOT NULL)",
            name="ck_recon_enrollment_token_use",
        ),
        Index("ix_recon_enrollment_expiry", "enterprise_id", "expires_at"),
    )


class SourceConnector(TenantRecord, Base):
    __tablename__ = "recon_source_connectors"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("recon_external_agents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    logical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(48), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    read_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "logical_key", name="uq_recon_source_connector_key"
        ),
        CheckConstraint(
            "connector_type IN ('directory','pbix_inventory','bi_activity')",
            name="ck_recon_source_connector_type",
        ),
    )


class AgentJob(TenantRecord, Base):
    __tablename__ = "recon_agent_jobs"

    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_external_agents.id", ondelete="RESTRICT"), index=True
    )
    connector_id: Mapped[str | None] = mapped_column(
        ForeignKey("recon_source_connectors.id", ondelete="RESTRICT"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','leased','running','waiting_review','succeeded','failed','cancelled')",
            name="ck_recon_agent_job_state",
        ),
        CheckConstraint(
            "progress_current >= 0 AND "
            "(progress_total IS NULL OR progress_total >= progress_current)",
            name="ck_recon_agent_job_progress",
        ),
        Index(
            "ix_recon_agent_job_claim",
            "enterprise_id",
            "state",
            "priority",
            "created_at",
        ),
    )


class AgentJobEvent(TenantRecord, Base):
    __tablename__ = "recon_agent_job_events"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("recon_agent_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_recon_agent_job_event"),
        CheckConstraint("sequence >= 0", name="ck_recon_agent_job_event_sequence"),
    )


class DiscoveredFile(TenantRecord, Base):
    __tablename__ = "recon_discovered_files"

    connector_id: Mapped[str] = mapped_column(
        ForeignKey("recon_source_connectors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    path_key: Mapped[str] = mapped_column(String(64), nullable=False)
    full_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_mtime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="discovered", nullable=False)
    safety_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "connector_id", "path_key", name="uq_recon_discovered_file_path"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_recon_discovered_file_size"),
        CheckConstraint(
            "status IN ('discovered','stable','skipped','queued','processed','failed','missing')",
            name="ck_recon_discovered_file_status",
        ),
        Index(
            "ix_recon_discovered_file_status",
            "enterprise_id",
            "connector_id",
            "status",
            "last_seen_at",
        ),
    )
