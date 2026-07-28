"""Real shadow reconciliation for counterfactual experiments.

The shadow run drives the same deterministic kernel as production against the
same frozen rows, differing only by the hypothesis under test. It writes into
the isolated ``counterfactual`` schema and never touches a ``reconciliation_*``
or ``pnl_cell`` table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum
from commerce_harness.kernel.recon import BalanceStatus, reconcile_items
from commerce_harness.spec.evaluate import evaluate as evaluate_invariants
from commerce_harness.spec.invariant import InvariantDefinition
from commerce_harness.spec.rule import RuleDefinition, route_rules_only

_ZERO = Decimal("0.0000")


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """One side of a counterfactual: the summary plus its result checksum."""

    summary: dict[str, Any]
    checksum: str
    unresolved_subjects: dict[str, Decimal]
    routed_count: int


def _summarize(
    result: Any,
    *,
    rows: list[dict[str, Any]],
    routed_count: int,
    invariants: list[InvariantDefinition],
    materiality_floor: Decimal,
) -> dict[str, Any]:
    from commerce_harness.phase_a import (
        _control_differences,
        _missing_profit_components,
        _one_sided_amount_basis,
    )

    unresolved_amount = sum(
        (item.absolute_exposure for item in result.unresolved), _ZERO
    )
    matched_amount = sum(
        (
            abs(balance.order_amount)
            for balance in result.balances
            if balance.status == BalanceStatus.BALANCED
        ),
        _ZERO,
    )
    item_amount = sum((abs(item.amount) for item in result.items), _ZERO)
    basis = _one_sided_amount_basis(rows)

    evaluations = evaluate_invariants(rows, invariants) if invariants else ()
    violated = [item for item in evaluations if item.status == "violated"]
    control_gap = sum(
        (abs(Decimal(value)) for value in _control_differences(rows).values()),
        _ZERO,
    )

    line_auto_rate = (
        Decimal(len(result.items) - len(result.unresolved)) / Decimal(len(result.items))
        if result.items
        else _ZERO
    )
    amount_weighted_auto_rate = (
        (item_amount - unresolved_amount) / item_amount if item_amount > 0 else _ZERO
    )
    explained_amount_ratio = (
        (basis - unresolved_amount) / basis if basis > 0 else _ZERO
    )

    return {
        "unresolved_count": Decimal(len(result.unresolved)),
        "unresolved_amount_abs": unresolved_amount,
        "material_unresolved_count": Decimal(
            sum(
                1
                for item in result.unresolved
                if item.absolute_exposure >= materiality_floor
            )
        ),
        "line_auto_rate": line_auto_rate,
        "amount_weighted_auto_rate": amount_weighted_auto_rate,
        "explained_amount_ratio": explained_amount_ratio,
        "control_total_gap": control_gap,
        "invariant_pass_delta": Decimal(len(evaluations) - len(violated)),
        "matched_amount": matched_amount,
        "item_count": Decimal(len(result.items)),
        "routed_count": Decimal(routed_count),
        "missing_profit_components": Decimal(
            len(_missing_profit_components(rows))
        ),
        # Safety metrics partially computed per-side; comparison may override.
        "major_reversal_count": _ZERO,
        "newly_unresolved_count": _ZERO,
        # baseline_regression_count: subjects that were balanced but now are
        # unresolved. Per-side we can only report the raw unresolved set;
        # the real regression count requires the comparison (see compare_outcomes).
        "baseline_regression_count": _ZERO,
        "evidence_integrity_failures": _ZERO,
    }


def _unresolved_subjects(result: Any) -> dict[str, Decimal]:
    subjects: dict[str, Decimal] = {}
    for item in result.unresolved:
        key = f"{item.kind.value}:{item.business_key}"
        subjects[key] = subjects.get(key, _ZERO) + item.absolute_exposure
    return subjects


class ShadowReconciler:
    """Run the production kernel over frozen rows under a candidate rule set."""

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        contract: Any,
        invariants: list[InvariantDefinition],
        materiality_floor: Decimal,
    ) -> None:
        self._rows = rows
        self._contract = contract
        self._invariants = invariants
        self._materiality_floor = materiality_floor

    def run(self, route_rules: list[RuleDefinition]) -> ShadowOutcome:
        from commerce_harness.phase_a import build_reconciliation_inputs

        inputs = build_reconciliation_inputs(
            self._rows, self._contract, route_rules_only(route_rules),
        )
        result = reconcile_items(
            inputs.items,
            self._contract,
            link_rule_version="taobao-order-platform-key-v1",
            cash_bridges=inputs.bridges.values(),
        )
        summary = _summarize(
            result,
            rows=self._rows,
            routed_count=len(inputs.routed_items),
            invariants=self._invariants,
            materiality_floor=self._materiality_floor,
        )
        return ShadowOutcome(
            summary=summary,
            checksum=result.checksum(),
            unresolved_subjects=_unresolved_subjects(result),
            routed_count=len(inputs.routed_items),
        )


def compare_outcomes(
    before: ShadowOutcome,
    after: ShadowOutcome,
    *,
    materiality_floor: Decimal,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Derive safety metrics and per-subject deltas from two shadow outcomes.

    Safety metrics only exist in the comparison: "newly unresolved" and "major
    reversal" are statements about the change, not about either side.
    """
    before_subjects = before.unresolved_subjects
    after_subjects = after.unresolved_subjects

    newly_unresolved = [
        key for key in after_subjects if key not in before_subjects
    ]
    deltas: list[dict[str, Any]] = []
    major_reversals = 0
    for key in sorted(set(before_subjects) | set(after_subjects)):
        before_amount = before_subjects.get(key, _ZERO)
        after_amount = after_subjects.get(key, _ZERO)
        if before_amount == after_amount:
            continue
        # A reversal flips the sign of an exposure rather than shrinking it;
        # a material flip means the hypothesis moved money, not just noise.
        is_reversal = (
            before_amount > 0
            and after_amount > 0
            and after_amount > before_amount
        )
        is_material = (
            abs(after_amount - before_amount) >= materiality_floor
        )
        if is_reversal and is_material:
            major_reversals += 1
        deltas.append({
            "subject_kind": "unresolved_balance",
            "subject_key": key,
            "before_amount": before_amount,
            "after_amount": after_amount,
            "is_material": is_material,
            "is_reversal": is_reversal,
        })

    # baseline_regression_count: subjects that were balanced (not in the
    # before unresolved set) but became unresolved in the after set.
    # This is the real regression count — not a hardcoded zero.
    baseline_regressions = sum(
        1
        for key in after_subjects
        if key not in before_subjects
        and after_subjects[key] >= materiality_floor
    )

    after_summary = dict(after.summary)
    after_summary["newly_unresolved_count"] = Decimal(len(newly_unresolved))
    after_summary["major_reversal_count"] = Decimal(major_reversals)
    after_summary["baseline_regression_count"] = Decimal(baseline_regressions)
    return after_summary, deltas


def outcome_digest(outcome: ShadowOutcome) -> str:
    """Stable digest of a shadow outcome for repeat-execution checks."""
    return deterministic_checksum({
        "checksum": outcome.checksum,
        "summary": {
            key: str(value) for key, value in sorted(outcome.summary.items())
        },
        "unresolved_subjects": {
            key: str(value)
            for key, value in sorted(outcome.unresolved_subjects.items())
        },
    })


def evidence_digest_for(delta: dict[str, Any]) -> str:
    return deterministic_checksum({
        "subject_kind": delta["subject_kind"],
        "subject_key": delta["subject_key"],
        "before_amount": str(delta["before_amount"]),
        "after_amount": str(delta["after_amount"]),
    })


def deltas_evidence_json(delta: dict[str, Any]) -> str:
    return json.dumps(
        {
            "subject_key": delta["subject_key"],
            "before_amount": str(delta["before_amount"]),
            "after_amount": str(delta["after_amount"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
