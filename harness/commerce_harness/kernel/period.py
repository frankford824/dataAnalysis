"""Immutable accounting-period state machine and correction trail."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from .money import amount, sum_money
from .recon import EvidenceRef


class PeriodState(StrEnum):
    OPEN = "open"
    PRE_CLOSED = "preclosed"
    FINALIZED = "closed"
    RESTATED = "restated"


class PeriodLockedError(RuntimeError):
    """Raised when a normal revision attempts to change a locked period."""


class InvalidPeriodTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InputRevision:
    revision_id: str
    source_type: str
    file_id: str
    content_checksum: str
    received_at: datetime
    supersedes_revision_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.revision_id,
                self.source_type,
                self.file_id,
                self.content_checksum,
            )
        ):
            raise ValueError("revision identifiers and checksum are required")


@dataclass(frozen=True, slots=True)
class AdjustmentEntry:
    adjustment_id: str
    original_period_key: str
    amount: Decimal
    reason: str
    decided_by: str
    decided_at: datetime
    evidence: tuple[EvidenceRef, ...]
    reverses_adjustment_id: str | None = None

    def __post_init__(self) -> None:
        if not self.adjustment_id or not self.reason.strip() or not self.decided_by.strip():
            raise ValueError("adjustment id, reason, and decision owner are required")
        if not self.evidence:
            raise ValueError("adjustments require source or decision evidence")
        normalized = amount(self.amount)
        if normalized == 0:
            raise ValueError("zero-value adjustments are not allowed")
        object.__setattr__(self, "amount", normalized)


@dataclass(frozen=True, slots=True)
class AccountingPeriod:
    enterprise_id: str
    store_id: str
    period_key: str
    state: PeriodState = PeriodState.OPEN
    version: int = 1
    revisions: tuple[InputRevision, ...] = ()
    adjustments: tuple[AdjustmentEntry, ...] = ()
    finalized_at: datetime | None = None
    restatement_number: int = 0

    def __post_init__(self) -> None:
        if not self.enterprise_id or not self.store_id or not self.period_key:
            raise ValueError("enterprise, store, and period key are required")
        if self.version < 1 or self.restatement_number < 0:
            raise ValueError("period version counters must be non-negative")

    @property
    def locked(self) -> bool:
        return self.state in {PeriodState.FINALIZED, PeriodState.RESTATED}

    @property
    def net_adjustment(self) -> Decimal:
        return sum_money(entry.amount for entry in self.adjustments)

    def register_revision(self, revision: InputRevision) -> AccountingPeriod:
        """Register or supersede an input while the period is not locked.

        Replaying the same source/checksum is idempotent. A changed checksum for
        the same source must point to the previous revision explicitly.
        """

        if self.locked:
            raise PeriodLockedError(
                "finalized periods reject revisions; post an adjustment instead"
            )
        for existing in self.revisions:
            if (
                existing.source_type == revision.source_type
                and existing.content_checksum == revision.content_checksum
            ):
                return self

        prior = [
            entry
            for entry in self.revisions
            if entry.source_type == revision.source_type
        ]
        if prior:
            latest = max(prior, key=lambda entry: entry.received_at)
            if revision.supersedes_revision_id != latest.revision_id:
                raise ValueError(
                    "changed source revision must explicitly supersede the latest revision"
                )
        elif revision.supersedes_revision_id is not None:
            raise ValueError("first source revision cannot supersede another revision")

        return replace(
            self,
            version=self.version + 1,
            revisions=self.revisions + (revision,),
        )

    def preclose(self) -> AccountingPeriod:
        if self.state != PeriodState.OPEN:
            raise InvalidPeriodTransition("only an open period can be pre-closed")
        return replace(self, state=PeriodState.PRE_CLOSED, version=self.version + 1)

    def finalize(self, *, at: datetime | None = None) -> AccountingPeriod:
        if self.state != PeriodState.PRE_CLOSED:
            raise InvalidPeriodTransition("only a pre-closed period can be finalized")
        return replace(
            self,
            state=PeriodState.FINALIZED,
            version=self.version + 1,
            finalized_at=at or datetime.now(UTC),
        )

    def post_adjustment(self, adjustment: AdjustmentEntry) -> AccountingPeriod:
        if not self.locked:
            raise InvalidPeriodTransition(
                "adjustments are only valid after period finalization"
            )
        if adjustment.original_period_key != self.period_key:
            raise ValueError("adjustment must reference its original period")
        if any(
            existing.adjustment_id == adjustment.adjustment_id
            for existing in self.adjustments
        ):
            return self
        if adjustment.reverses_adjustment_id and not any(
            existing.adjustment_id == adjustment.reverses_adjustment_id
            for existing in self.adjustments
        ):
            raise ValueError("reversal must reference an existing adjustment")
        return replace(
            self,
            state=PeriodState.RESTATED,
            version=self.version + 1,
            adjustments=self.adjustments + (adjustment,),
            restatement_number=self.restatement_number + 1,
        )


def latest_revisions(
    revisions: Iterable[InputRevision],
) -> tuple[InputRevision, ...]:
    """Return the current revision per logical source, deterministically."""

    selected: dict[str, InputRevision] = {}
    for revision in revisions:
        current = selected.get(revision.source_type)
        if current is None or (revision.received_at, revision.revision_id) > (
            current.received_at,
            current.revision_id,
        ):
            selected[revision.source_type] = revision
    return tuple(selected[key] for key in sorted(selected))
