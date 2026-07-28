from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy import JSON as SAJSON
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Enterprise(Base):
    __tablename__ = "enterprises"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    activation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VersionedTenantMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BusinessEntity(VersionedTenantMixin, Base):
    __tablename__ = "business_entities"
    legal_name: Mapped[str | None] = mapped_column(Text)


class PlatformAccount(VersionedTenantMixin, Base):
    __tablename__ = "platform_accounts"
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text)
    logical_id: Mapped[str] = mapped_column(String(36), default=uuid4_str, nullable=False)
    __table_args__ = (Index("ix_platforms_logical_effective", "enterprise_id", "logical_id", "effective_from"),)


class Store(VersionedTenantMixin, Base):
    __tablename__ = "stores"
    platform_account_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id", ondelete="RESTRICT"), index=True)
    business_entity_id: Mapped[str | None] = mapped_column(ForeignKey("business_entities.id", ondelete="RESTRICT"), index=True)
    activation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_store_id: Mapped[str | None] = mapped_column(Text)
    logical_id: Mapped[str] = mapped_column(String(36), default=uuid4_str, nullable=False)
    __table_args__ = (
        Index("ix_stores_enterprise_status", "enterprise_id", "status"),
        Index("ix_stores_logical_effective", "enterprise_id", "logical_id", "effective_from"),
    )


class UserAccount(VersionedTenantMixin, Base):
    __tablename__ = "user_accounts"
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    store_ids: Mapped[list[str]] = mapped_column(SAJSON, default=list, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("email", name="uq_user_email_global"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SourceDefinition(VersionedTenantMixin, Base):
    __tablename__ = "source_definitions"
    file_types: Mapped[list[str]] = mapped_column(SAJSON, default=list, nullable=False)
    recognition: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    field_aliases: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    coverage_time_field: Mapped[str] = mapped_column(Text, nullable=False)
    data_granularity: Mapped[str] = mapped_column(String(16), nullable=False)
    arrival_frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_rows: Mapped[int | None] = mapped_column(Integer)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dedupe_keys: Mapped[list[str]] = mapped_column(SAJSON, default=list, nullable=False)
    validations: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list, nullable=False)
    activation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    store_field: Mapped[str | None] = mapped_column(Text)
    logical_id: Mapped[str] = mapped_column(String(36), default=uuid4_str, nullable=False)
    import_mode: Mapped[str] = mapped_column(String(32), default="monthly_snapshot", nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), default="orders", nullable=False)
    amount_directions: Mapped[dict[str, str]] = mapped_column(SAJSON, default=dict, nullable=False)
    __table_args__ = (
        Index("ix_sources_enterprise_status", "enterprise_id", "status"),
        Index("ix_sources_logical_effective", "enterprise_id", "logical_id", "effective_from"),
    )


class SourceBinding(VersionedTenantMixin, Base):
    __tablename__ = "source_bindings"
    source_definition_id: Mapped[str] = mapped_column(ForeignKey("source_definitions.id", ondelete="RESTRICT"), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    __table_args__ = (
        UniqueConstraint("enterprise_id", "source_definition_id", "scope_type", "scope_id", "version", name="uq_source_binding_version"),
        Index("ix_source_bindings_scope", "enterprise_id", "scope_type", "scope_id"),
    )


class SourceSchedule(VersionedTenantMixin, Base):
    __tablename__ = "source_schedules"
    source_definition_id: Mapped[str] = mapped_column(ForeignKey("source_definitions.id", ondelete="RESTRICT"), nullable=False, index=True)
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_definition_id: Mapped[str] = mapped_column(ForeignKey("source_definitions.id", ondelete="RESTRICT"), nullable=False, index=True)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    raw_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_object_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    coverage_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_config_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rule_config_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_version: Mapped[int | None] = mapped_column(Integer)
    semantic_model_id: Mapped[str | None] = mapped_column(String(36))
    quality_result: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_run_ids: Mapped[list[str]] = mapped_column(SAJSON, default=list, nullable=False)
    correction_of_run_id: Mapped[str | None] = mapped_column(String(36))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    correction_approved_by: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("enterprise_id", "source_sha256", name="uq_ingestion_file_global"),
        Index("ix_ingestion_tenant_status", "enterprise_id", "status", "created_at"),
    )


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_definition_id: Mapped[str | None] = mapped_column(ForeignKey("source_definitions.id", ondelete="RESTRICT"), index=True)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size: Mapped[int] = mapped_column(Integer, nullable=False)
    part_size: Mapped[int] = mapped_column(Integer, nullable=False)
    received_parts: Mapped[list[int]] = mapped_column(SAJSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="uploading", nullable=False)
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_upload_tenant_status", "enterprise_id", "status"),)


class ModelAsset(VersionedTenantMixin, Base):
    __tablename__ = "model_assets"
    asset_type: Mapped[str] = mapped_column(String(32), default="pbix", nullable=False)
    object_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    input_contract: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), default="manual_required", nullable=False)
    parser_message: Mapped[str | None] = mapped_column(Text)


class ModelScopeBinding(VersionedTenantMixin, Base):
    __tablename__ = "model_scope_bindings"
    model_asset_id: Mapped[str] = mapped_column(ForeignKey("model_assets.id", ondelete="RESTRICT"), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    __table_args__ = (Index("ix_model_scope", "enterprise_id", "scope_type", "scope_id"),)


class SemanticModelVersion(VersionedTenantMixin, Base):
    __tablename__ = "semantic_model_versions"
    industry_template: Mapped[str] = mapped_column(Text, default="ecommerce_standard", nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    quality_gates: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list, nullable=False)


class MetricDefinition(VersionedTenantMixin, Base):
    __tablename__ = "metric_definitions"
    semantic_model_id: Mapped[str | None] = mapped_column(ForeignKey("semantic_model_versions.id", ondelete="RESTRICT"), index=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="positive", nullable=False)
    format: Mapped[str] = mapped_column(String(32), default="currency", nullable=False)
    __table_args__ = (UniqueConstraint("enterprise_id", "key", "version", name="uq_metric_key_version"),)


class DashboardAsset(VersionedTenantMixin, Base):
    __tablename__ = "dashboard_assets"
    bi_adapter: Mapped[str] = mapped_column(String(32), default="superset", nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    embed_url: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)


class AIProvider(VersionedTenantMixin, Base):
    __tablename__ = "ai_providers"
    mode: Mapped[str] = mapped_column(String(16), default="disabled", nullable=False)
    api_base: Mapped[str | None] = mapped_column(Text)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)


class AIModelProfile(VersionedTenantMixin, Base):
    __tablename__ = "ai_model_profiles"
    provider_id: Mapped[str] = mapped_column(ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    budget_cents: Mapped[int | None] = mapped_column(Integer)


class AITaskPolicy(VersionedTenantMixin, Base):
    __tablename__ = "ai_task_policies"
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_model_profiles.id", ondelete="RESTRICT"), index=True)
    fallback_model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_model_profiles.id", ondelete="RESTRICT"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    redaction_policy: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)


class NormalizedRecord(Base):
    __tablename__ = "normalized_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False)
    ingestion_run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"))
    store_logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(32), default="sale", nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    refund: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    advertising_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    product_cost: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    source_logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    business_key: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_run_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        Index("ix_records_tenant_time", "enterprise_id", "occurred_at"),
        Index("ix_records_run", "ingestion_run_id"),
        Index("ix_records_store_time", "store_id", "occurred_at"),
        Index("ix_records_business_key", "enterprise_id", "source_logical_id", "store_logical_id", "business_key"),
        Index(
            "uq_records_current_business_key",
            "enterprise_id",
            "source_logical_id",
            "store_logical_id",
            "business_key",
            unique=True,
            sqlite_where=text("is_current = 1 AND business_key IS NOT NULL"),
            postgresql_where=text("is_current AND business_key IS NOT NULL"),
        ),
    )


class CertifiedAggregate(Base):
    __tablename__ = "certified_aggregates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False)
    ingestion_run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"))
    store_logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    refund: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    advertising_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    product_cost: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    profit: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0, nullable=False)
    source_definition_id: Mapped[str] = mapped_column(ForeignKey("source_definitions.id", ondelete="RESTRICT"), nullable=False)
    source_logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_run_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        UniqueConstraint("enterprise_id", "ingestion_run_id", "store_id", "period_start", "grain", name="uq_certified_period"),
        Index("ix_certified_tenant_period", "enterprise_id", "period_start"),
        Index("ix_certified_current_scope", "enterprise_id", "source_logical_id", "store_logical_id", "period_start", "is_current"),
    )


class CrossSourceReconciliation(Base):
    __tablename__ = "cross_source_reconciliations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    ingestion_run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    dependency_run_id: Mapped[str | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), index=True)
    validation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    store_logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_value: Mapped[str | None] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(Text)
    difference: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("ingestion_run_id", "validation_key", "store_logical_id", "period_start", name="uq_cross_source_run_scope"),
        Index("ix_cross_source_scope", "enterprise_id", "store_logical_id", "period_start", "status"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str | None] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), index=True)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_audit_tenant_created", "enterprise_id", "created_at"),)


class Problem(Base):
    __tablename__ = "problems"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    ingestion_run_id: Mapped[str | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    technical_detail: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    resolution: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_problem_tenant_status", "enterprise_id", "status", "created_at"),)


class ReviewQueueItem(Base):
    __tablename__ = "review_queue"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"), nullable=False, index=True)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), nullable=False, unique=True)
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.id", ondelete="SET NULL"))
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
