from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from commerce_harness.judgment.models import strict_decimal


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    file_id: str
    row_no: int
    metric: str
    period: str
    shop: str
    definition_id: str
    value: Decimal | str | int

    def __post_init__(self) -> None:
        for name in ("file_id", "metric", "period", "shop", "definition_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.row_no < 1:
            raise ValueError("row_no must be positive")
        object.__setattr__(self, "value", strict_decimal(self.value))

    @property
    def identity(self) -> tuple[str, int, str, str, str, str, Decimal]:
        return (
            self.file_id,
            self.row_no,
            self.metric,
            self.period,
            self.shop,
            self.definition_id,
            cast(Decimal, self.value),
        )


@dataclass(frozen=True, slots=True)
class MetricBinding:
    binding_id: str
    metric: str
    period: str
    shop: str
    definition_id: str
    value: Decimal | str | int
    evidence: tuple[EvidenceBinding, ...]
    display_unit: str = "元"

    def __post_init__(self) -> None:
        for name in ("binding_id", "metric", "period", "shop", "definition_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "value", strict_decimal(self.value))
        if not self.evidence:
            raise ValueError("metric binding requires evidence")
        for item in self.evidence:
            if (
                item.metric,
                item.period,
                item.shop,
                item.definition_id,
            ) != (
                self.metric,
                self.period,
                self.shop,
                self.definition_id,
            ):
                raise ValueError("evidence dimensions must match the bound metric dimensions")

    @property
    def dimension_key(self) -> tuple[str, str, str, str]:
        return (self.metric, self.period, self.shop, self.definition_id)


@dataclass(frozen=True, slots=True)
class ReportMetricSlot:
    slot_id: str
    label: str
    binding_id: str
    metric: str
    period: str
    shop: str
    definition_id: str
    value: Decimal | str | int
    evidence: tuple[EvidenceBinding, ...]

    def __post_init__(self) -> None:
        for name in (
            "slot_id",
            "label",
            "binding_id",
            "metric",
            "period",
            "shop",
            "definition_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "value", strict_decimal(self.value))


@dataclass(frozen=True, slots=True)
class ReportSection:
    heading: str
    narrative: str
    metrics: tuple[ReportMetricSlot, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.heading.strip():
            raise ValueError("section heading is required")


@dataclass(frozen=True, slots=True)
class StructuredReport:
    report_id: str
    kind: str
    title: str
    period: str
    shop: str
    sections: tuple[ReportSection, ...]
    status: str = field(default="draft", init=False)

    def __post_init__(self) -> None:
        for name in ("report_id", "kind", "title", "period", "shop"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if not self.sections:
            raise ValueError("report requires at least one section")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    suggestion_id: str
    residual_id: str
    decision: str
    final_action: str
    human_reason: str
    decided_by: str
    candidate_hash: str
    decided_by_human: bool = field(default=True, init=False)
    may_write_ledger: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.decision not in {"approve_suggestion", "reject", "replace", "defer"}:
            raise ValueError(f"unsupported human decision: {self.decision}")
        for name in (
            "suggestion_id",
            "residual_id",
            "human_reason",
            "decided_by",
            "candidate_hash",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.decision in {"approve_suggestion", "replace"} and not self.final_action.strip():
            raise ValueError("approved or replaced decisions require final_action")


def as_tuple(items: Iterable[EvidenceBinding]) -> tuple[EvidenceBinding, ...]:
    return tuple(items)
