from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class EnrollmentTokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    expires_in_minutes: int = Field(default=30, ge=5, le=1440)


class AgentRegisterRequest(BaseModel):
    enrollment_token: str = Field(min_length=32, max_length=512)
    name: str = Field(min_length=1, max_length=120)
    machine_name: str = Field(min_length=1, max_length=255)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)


class HostAgentRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    connector: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    protocol_version: str = Field(default="1", min_length=1, max_length=32)


class HostAgentHeartbeatRequest(BaseModel):
    agent_id: str | None = None
    status: Literal["online", "busy", "degraded"] = "online"
    at: datetime | None = None


class HostAgentClaimRequest(BaseModel):
    agent_id: str | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=64)


class HostAgentProgressRequest(BaseModel):
    sequence: int = Field(ge=0)
    stage: Literal[
        "claimed",
        "scanning",
        "materializing",
        "profiling",
        "recomputing",
        "uploading",
        "completed",
        "failed",
    ]
    message: str = Field(min_length=1, max_length=500)
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class AgentHeartbeatRequest(BaseModel):
    status: Literal["online", "busy", "degraded"] = "online"
    capabilities: list[str] | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class AgentEventCreate(BaseModel):
    external_event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["started", "progress", "waiting_review", "resumed", "warning", "completed", "failed"]
    stage: str = Field(min_length=1, max_length=64)
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)


class DiscoveredFileInput(BaseModel):
    fingerprint: str = Field(min_length=16, max_length=256)
    source_path: str = Field(min_length=1, max_length=4096)
    file_name: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class DiscoveredFileBatch(BaseModel):
    files: list[DiscoveredFileInput] = Field(min_length=1, max_length=1000)


class AgentJobResult(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=500)
    replayed: bool = False


class AgentJobFailure(BaseModel):
    error_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    connector_type: Literal["finance_win", "filesystem", "pbix_inventory", "powerbi_activity"]
    agent_id: str | None = None
    store_ids: list[str] = Field(default_factory=list, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("store_ids")
    @classmethod
    def unique_store_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("店铺范围不能重复")
        return value


class ConnectorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    agent_id: str | None = None
    store_ids: list[str] | None = Field(default=None, max_length=500)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class ScanRequest(BaseModel):
    reason: str = Field(default="manual", max_length=200)
    full_scan: bool = False


class ReviewDecisionCreate(BaseModel):
    action: Literal["confirm", "reject", "request_approval"]
    note: str = Field(min_length=2, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)


class LlmProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    mode: Literal["disabled", "cloud", "local"] = "disabled"
    api_base: HttpUrl | None = None
    api_key: str | None = Field(default=None, min_length=8, max_length=4096)
    clear_api_key: bool = False


class LlmModelInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    model_name: str = Field(min_length=1, max_length=255)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=1, ge=0, le=5)
    budget_cents: int | None = Field(default=None, ge=0)


class LlmTaskPolicyInput(BaseModel):
    task: Literal[
        "file_classification",
        "rule_proposal",
        "difference_explanation",
        "business_explanation",
    ]
    primary_model: str | None = None
    fallback_model: str | None = None
    enabled: bool = False
    redaction_policy: dict[str, Any] = Field(default_factory=dict)


class LlmConfigUpdate(BaseModel):
    provider: LlmProviderInput
    models: list[LlmModelInput] = Field(default_factory=list, max_length=50)
    task_policies: list[LlmTaskPolicyInput] = Field(default_factory=list, max_length=50)
