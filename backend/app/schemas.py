from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class EnterpriseCreate(APIModel):
    name: str = Field(min_length=1)
    activation_at: datetime


class ResourceCreate(APIModel):
    name: str = Field(min_length=1)
    status: str = "draft"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    legal_name: str | None = None
    platform: str | None = None
    external_account_id: str | None = None
    platform_account_id: str | None = None
    business_entity_id: str | None = None
    activation_at: datetime | None = None
    external_store_id: str | None = None
    email: str | None = None
    role: Literal["admin", "implementer", "analyst", "viewer"] | None = None
    store_ids: list[str] | None = None
    file_types: list[str] | None = None
    recognition: dict[str, Any] | None = None
    field_aliases: dict[str, Any] | None = None
    coverage_time_field: str | None = None
    data_granularity: Literal["event", "hour", "day", "month", "custom"] | None = None
    arrival_frequency: Literal["hourly", "daily", "monthly", "adhoc"] | None = None
    expected_rows: int | None = None
    required: bool | None = None
    dedupe_keys: list[str] | None = None
    validations: list[dict[str, Any]] | None = None
    store_field: str | None = None
    source_definition_id: str | None = None
    scope_type: Literal["enterprise", "business_entity", "platform_account", "store", "source"] | None = None
    scope_id: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    model_asset_id: str | None = None
    asset_type: Literal["pbix", "pbip", "builtin"] | None = None
    input_contract: dict[str, Any] | None = None
    metadata_payload: dict[str, Any] | None = None
    validation_status: str | None = None
    industry_template: str | None = None
    definition: dict[str, Any] | None = None
    quality_gates: list[dict[str, Any]] | None = None
    semantic_model_id: str | None = None
    key: str | None = None
    expression: str | None = None
    direction: str | None = None
    format: str | None = None
    bi_adapter: Literal["builtin", "superset", "powerbi"] | None = None
    external_id: str | None = None
    embed_url: str | None = None
    mode: Literal["cloud", "local", "disabled"] | None = None
    api_base: str | None = None
    api_key: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    budget_cents: int | None = Field(default=None, ge=0)
    task: str | None = None
    primary_model_id: str | None = None
    fallback_model_id: str | None = None
    redaction_policy: dict[str, Any] | None = None


class ResourcePatch(APIModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    status: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    approved_by: str | None = None
    platform_account_id: str | None = None
    business_entity_id: str | None = None
    activation_at: datetime | None = None
    email: str | None = None
    role: Literal["admin", "implementer", "analyst", "viewer"] | None = None
    store_ids: list[str] | None = None
    field_aliases: dict[str, Any] | None = None
    recognition: dict[str, Any] | None = None
    validations: list[dict[str, Any]] | None = None
    expected_rows: int | None = None
    definition: dict[str, Any] | None = None
    quality_gates: list[dict[str, Any]] | None = None
    expression: str | None = None
    metadata_payload: dict[str, Any] | None = None
    validation_status: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    mode: Literal["cloud", "local", "disabled"] | None = None
    enabled: bool | None = None
    primary_model_id: str | None = None
    fallback_model_id: str | None = None
    redaction_policy: dict[str, Any] | None = None


class BusinessConfirmation(APIModel):
    accepted: bool
    note: str | None = Field(default=None, max_length=1000)


class UploadInitiate(APIModel):
    source_definition_id: str
    store_id: str | None = None
    filename: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size: int = Field(gt=0, le=10_000_000_000)
    part_size: int = Field(default=8 * 1024 * 1024, ge=256 * 1024, le=64 * 1024 * 1024)
    backfill: bool = False


class ConfigurationImport(APIModel):
    resources: dict[str, list[dict[str, Any]]]
    dry_run: bool = False


class CertifiedQuery(APIModel):
    sql: str = Field(min_length=1, max_length=20_000)


class ManualPBIXMetadata(APIModel):
    tables: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    expected_inputs: list[str] = Field(default_factory=list)
    note: str | None = None


class NaturalLanguageQuestion(APIModel):
    question: str = Field(min_length=3, max_length=2000)
    store_ids: list[str] = Field(default_factory=list, max_length=50)
    date_from: datetime | None = None
    date_to: datetime | None = None

    @model_validator(mode="after")
    def validate_context(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self
