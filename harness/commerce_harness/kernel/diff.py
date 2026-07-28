"""Historical comparison with metric → rule → source-row attribution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .money import amount, negate_money, subtract_money
from .recon import EvidenceRef


class DiffKind(StrEnum):
    EQUAL = "equal"
    FLOAT_TAIL = "float_tail"
    ROUNDING = "rounding"
    TRUE_DIFFERENCE = "true_difference"
    CURRENT_ONLY = "current_only"
    HISTORICAL_ONLY = "historical_only"


@dataclass(frozen=True, slots=True)
class ComparableCell:
    metric: str
    entity_key: str
    amount: Decimal
    rule_version: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.metric or not self.entity_key or not self.rule_version:
            raise ValueError("metric, entity key, and rule version are required")
        if not self.evidence:
            raise ValueError("comparable cells require row-level evidence")
        object.__setattr__(self, "amount", amount(self.amount))


@dataclass(frozen=True, slots=True)
class DiffAttribution:
    metric: str
    rule_versions: tuple[str, ...]
    source_rows: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.metric or not self.rule_versions or not self.source_rows:
            raise ValueError(
                "diff attribution requires metric, rule, and source-row levels"
            )


@dataclass(frozen=True, slots=True)
class DiffFinding:
    metric: str
    entity_key: str
    current_amount: Decimal | None
    historical_amount: Decimal | None
    difference: Decimal
    kind: DiffKind
    attribution: DiffAttribution


def _index(cells: Iterable[ComparableCell]) -> dict[tuple[str, str], ComparableCell]:
    result: dict[tuple[str, str], ComparableCell] = {}
    for cell in cells:
        key = (cell.metric, cell.entity_key)
        if key in result:
            raise ValueError(f"duplicate comparable cell: {key}")
        result[key] = cell
    return result


def _attribution(
    metric: str,
    *cells: ComparableCell | None,
) -> DiffAttribution:
    present = [cell for cell in cells if cell is not None]
    return DiffAttribution(
        metric=metric,
        rule_versions=tuple(sorted({cell.rule_version for cell in present})),
        source_rows=tuple(
            sorted(
                {
                    f"{evidence.file_id}:{evidence.row_no}"
                    for cell in present
                    for evidence in cell.evidence
                }
            )
        ),
    )


def compare_cells(
    current: Iterable[ComparableCell],
    historical: Iterable[ComparableCell],
    *,
    tolerance: str | Decimal = "0.0100",
    float_tail_tolerance: str | Decimal = "0.0001",
) -> tuple[DiffFinding, ...]:
    """Compare cells and retain the required three attribution levels."""

    current_index = _index(current)
    historical_index = _index(historical)
    rounding_tolerance = abs(amount(tolerance))
    tail_tolerance = abs(amount(float_tail_tolerance))
    findings: list[DiffFinding] = []

    for metric, entity_key in sorted(set(current_index) | set(historical_index)):
        current_cell = current_index.get((metric, entity_key))
        historical_cell = historical_index.get((metric, entity_key))
        if current_cell is None:
            difference = negate_money(historical_cell.amount)  # type: ignore[union-attr]
            kind = DiffKind.HISTORICAL_ONLY
        elif historical_cell is None:
            difference = current_cell.amount
            kind = DiffKind.CURRENT_ONLY
        else:
            difference = subtract_money(current_cell.amount, historical_cell.amount)
            absolute = abs(difference)
            if difference == 0:
                kind = DiffKind.EQUAL
            elif absolute <= tail_tolerance:
                kind = DiffKind.FLOAT_TAIL
            elif absolute <= rounding_tolerance:
                kind = DiffKind.ROUNDING
            else:
                kind = DiffKind.TRUE_DIFFERENCE
        findings.append(
            DiffFinding(
                metric=metric,
                entity_key=entity_key,
                current_amount=current_cell.amount if current_cell else None,
                historical_amount=(
                    historical_cell.amount if historical_cell else None
                ),
                difference=difference,
                kind=kind,
                attribution=_attribution(metric, current_cell, historical_cell),
            )
        )
    return tuple(findings)
