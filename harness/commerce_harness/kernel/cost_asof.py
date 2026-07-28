"""Versioned SKU cost lookup using half-open effective intervals."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .money import amount
from .recon import EvidenceRef


class CostLookupError(LookupError):
    pass


class CostOverlapError(CostLookupError):
    pass


@dataclass(frozen=True, slots=True)
class CostVersion:
    sku: str
    unit_cost: Decimal
    effective_from: datetime
    effective_to: datetime | None
    version: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.sku or not self.version:
            raise ValueError("sku and version are required")
        if not self.evidence:
            raise ValueError("cost versions require source evidence")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        object.__setattr__(self, "unit_cost", amount(self.unit_cost))

    def applies_at(self, instant: datetime) -> bool:
        return self.effective_from <= instant and (
            self.effective_to is None or instant < self.effective_to
        )


@dataclass(frozen=True, slots=True)
class CostMatch:
    sku: str
    order_at: datetime
    unit_cost: Decimal
    cost_version: str
    evidence: tuple[EvidenceRef, ...]


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def validate_cost_intervals(versions: Iterable[CostVersion]) -> None:
    by_sku: dict[str, list[CostVersion]] = {}
    for version in versions:
        by_sku.setdefault(version.sku, []).append(version)
    for sku, entries in by_sku.items():
        ordered = sorted(entries, key=lambda entry: (entry.effective_from, entry.version))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.effective_to is None or previous.effective_to > current.effective_from:
                raise CostOverlapError(
                    f"overlapping cost versions for {sku}: "
                    f"{previous.version} and {current.version}"
                )


def cost_as_of(
    sku: str,
    order_at: datetime | date,
    versions: Iterable[CostVersion],
) -> CostMatch:
    instant = _as_datetime(order_at)
    candidates = [
        version
        for version in versions
        if version.sku == sku and version.applies_at(instant)
    ]
    if not candidates:
        raise CostLookupError(f"no effective cost for {sku} at {instant.isoformat()}")
    if len(candidates) > 1:
        raise CostOverlapError(
            f"multiple effective costs for {sku} at {instant.isoformat()}"
        )
    selected = candidates[0]
    return CostMatch(
        sku=sku,
        order_at=instant,
        unit_cost=selected.unit_cost,
        cost_version=selected.version,
        evidence=selected.evidence,
    )
