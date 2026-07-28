from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobKind(StrEnum):
    SCAN = "scan"
    PROFILE = "profile"
    RECOMPUTE = "recompute"


class JobStage(StrEnum):
    CLAIMED = "claimed"
    SCANNING = "scanning"
    MATERIALIZING = "materializing"
    PROFILING = "profiling"
    RECOMPUTING = "recomputing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class FileRecord:
    source_id: str
    path: str
    purpose: str
    extension: str
    size: int
    mtime_utc: str
    attributes: tuple[str, ...] = ()
    sha256: str | None = None
    sheet: str | None = None
    recent_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attributes"] = list(self.attributes)
        return data


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    inferred_type: str
    non_null_count: int
    sample_values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sample_values"] = list(self.sample_values)
        return data


@dataclass(frozen=True)
class StructureProfile:
    path: str
    file_type: str
    sheet: str | None
    header_row: int
    row_count_sampled: int
    columns: tuple[ColumnProfile, ...]
    fingerprint: str
    classification: str
    classification_confidence: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "columns": [column.to_dict() for column in self.columns],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AgentJob:
    id: str
    kind: JobKind
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AgentJob:
        return cls(
            id=str(payload["id"]),
            kind=JobKind(payload["kind"]),
            payload=dict(payload.get("payload") or {}),
            idempotency_key=str(payload.get("idempotency_key") or payload["id"]),
        )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def safe_relative_output(base: Path, job_id: str, suffix: str) -> Path:
    cleaned = "".join(char for char in job_id if char.isalnum() or char in "-_")
    if not cleaned:
        raise ValueError("job id 不能生成安全输出路径")
    return base / f"{cleaned}{suffix}"
