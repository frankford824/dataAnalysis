from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

_FREE_TEXT_NUMBER = re.compile(r"\d")


def strict_decimal(value: Decimal | str | int) -> Decimal:
    """解析精确十进制并拒绝二进制浮点输入。"""

    if isinstance(value, float):
        raise TypeError("financial values must never enter through float")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


class SuggestionKind(StrEnum):
    CLASSIFICATION = "classification"
    LINKAGE = "linkage"
    EXPLANATION = "explanation"


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    file_id: str
    row_no: int
    metric: str
    period: str
    shop: str
    value: Decimal | str | int
    definition_id: str = ""

    def __post_init__(self) -> None:
        for name in ("file_id", "metric", "period", "shop"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.row_no < 1:
            raise ValueError("row_no must be positive")
        object.__setattr__(self, "value", strict_decimal(self.value))

    @property
    def identity(self) -> tuple[str, int, str, str, str, Decimal, str]:
        return (
            self.file_id,
            self.row_no,
            self.metric,
            self.period,
            self.shop,
            cast(Decimal, self.value),
            self.definition_id,
        )


@dataclass(frozen=True, slots=True)
class EvidenceRecord(EvidenceCitation):
    source_kind: str = "ledger"


@dataclass(frozen=True, slots=True)
class SuggestionCandidate:
    suggestion_id: str
    residual_id: str
    kind: SuggestionKind | str
    category: str
    action: str
    rationale: str
    confidence: Decimal | str | int
    citations: tuple[EvidenceCitation, ...]
    source_model: str
    status: str = field(default="suggestion", init=False)
    requires_human_review: bool = field(default=True, init=False)
    may_write_ledger: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in (
            "suggestion_id",
            "residual_id",
            "category",
            "action",
            "rationale",
            "source_model",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "kind", SuggestionKind(self.kind))
        confidence = strict_decimal(self.confidence)
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)
        if not self.citations:
            raise ValueError("at least one evidence citation is required")
        if _FREE_TEXT_NUMBER.search(self.action) or _FREE_TEXT_NUMBER.search(self.rationale):
            raise ValueError("numeric claims must be carried by typed citations, not free text")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, source_model: str) -> SuggestionCandidate:
        required = {
            "suggestion_id",
            "residual_id",
            "kind",
            "category",
            "action",
            "rationale",
            "confidence",
            "citations",
        }
        unknown = set(payload) - required
        missing = required - set(payload)
        if unknown or missing:
            raise ValueError(
                f"candidate schema mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        raw_citations = payload["citations"]
        if not isinstance(raw_citations, list):
            raise ValueError("citations must be a list")
        return cls(
            suggestion_id=str(payload["suggestion_id"]),
            residual_id=str(payload["residual_id"]),
            kind=str(payload["kind"]),
            category=str(payload["category"]),
            action=str(payload["action"]),
            rationale=str(payload["rationale"]),
            confidence=payload["confidence"],
            citations=tuple(EvidenceCitation(**item) for item in raw_citations),
            source_model=source_model,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["kind"] = cast(SuggestionKind, self.kind).value
        result["confidence"] = str(self.confidence)
        for citation in result["citations"]:
            citation["value"] = str(citation["value"])
        return result


@dataclass(frozen=True, slots=True)
class GuardFailure:
    citation: EvidenceCitation
    reason: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    valid: bool
    checked_count: int
    failures: tuple[GuardFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class CriticAssessment:
    suggestion_id: str
    accepted_for_review: bool
    reason: str
    status: str = field(default="critic_advice", init=False)
    requires_human_review: bool = field(default=True, init=False)
    may_write_ledger: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class ReviewRecommendation:
    residual_id: str
    outcome: str
    candidates: tuple[SuggestionCandidate, ...]
    recommended_action: str | None
    reasons: tuple[str, ...]
    status: str = field(default="suggestion", init=False)
    requires_human_review: bool = field(default=True, init=False)
    may_write_ledger: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class GatewayResult:
    status: str
    model: str
    content: Mapping[str, Any] | None = None
    reason: str | None = None
    request_id: str | None = None
