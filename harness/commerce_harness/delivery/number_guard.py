from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import MetricBinding, ReportMetricSlot, StructuredReport


@dataclass(frozen=True, slots=True)
class NumberGuardResult:
    valid: bool
    checked_slots: int
    violations: tuple[str, ...] = ()


class MetricLedger:
    def __init__(self, bindings: Iterable[MetricBinding]) -> None:
        self.bindings = tuple(bindings)
        self.by_id = {binding.binding_id: binding for binding in self.bindings}
        self.by_dimensions = {binding.dimension_key: binding for binding in self.bindings}
        if len(self.by_id) != len(self.bindings):
            raise ValueError("duplicate binding_id")
        if len(self.by_dimensions) != len(self.bindings):
            raise ValueError("duplicate metric×period×shop×definition binding")


class NumberGuard:
    """校验数字与指标、期间、店铺、口径和证据的完整绑定。"""

    _DIGIT = re.compile(r"\d")

    def validate_slot(self, slot: ReportMetricSlot, ledger: MetricLedger) -> tuple[str, ...]:
        violations: list[str] = []
        expected = ledger.by_id.get(slot.binding_id)
        dimensions = (slot.metric, slot.period, slot.shop, slot.definition_id)
        by_dimensions = ledger.by_dimensions.get(dimensions)
        if expected is None:
            return (f"{slot.slot_id}: unknown binding_id",)
        if by_dimensions is not expected:
            violations.append(f"{slot.slot_id}: metric×period×shop×definition mismatch")
        if expected.value != slot.value:
            violations.append(f"{slot.slot_id}: value does not match the exact binding")
        actual_evidence = {item.identity for item in slot.evidence}
        expected_evidence = {item.identity for item in expected.evidence}
        if actual_evidence != expected_evidence:
            violations.append(f"{slot.slot_id}: evidence set does not match the binding")
        return tuple(violations)

    def validate_report(self, report: StructuredReport, ledger: MetricLedger) -> NumberGuardResult:
        violations: list[str] = []
        checked = 0
        for section in report.sections:
            # Narrative is intentionally number-free. All report numbers must use typed slots.
            if self._DIGIT.search(section.narrative):
                violations.append(
                    f"{section.heading}: free-text narrative contains an unbound number"
                )
            for note in section.notes:
                if self._DIGIT.search(note):
                    violations.append(f"{section.heading}: note contains an unbound number")
            for slot in section.metrics:
                checked += 1
                if slot.period != report.period or slot.shop != report.shop:
                    violations.append(f"{slot.slot_id}: slot is outside the report scope")
                violations.extend(self.validate_slot(slot, ledger))
        return NumberGuardResult(
            valid=not violations,
            checked_slots=checked,
            violations=tuple(violations),
        )

    def assert_valid(self, report: StructuredReport, ledger: MetricLedger) -> None:
        result = self.validate_report(report, ledger)
        if not result.valid:
            raise ValueError("number guard rejected report: " + "; ".join(result.violations))
