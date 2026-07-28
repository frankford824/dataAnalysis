"""Pure evaluation of invariant contracts against row data.

Compiles invariant families to existing kernel assertions where possible,
produces ``InvariantEvaluation`` result tuples.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum
from commerce_harness.kernel.money import amount, subtract_money, sum_money

from .invariant import InvariantDefinition
from .predicate import evaluate_predicate, parse_predicate

EVALUATION_STATUSES = frozenset({
    "passed", "violated", "not_applicable", "insufficient_input",
})


@dataclass(frozen=True, slots=True)
class InvariantEvaluation:
    evaluation_id: str
    invariant_id: str
    status: str
    left_total: Decimal
    right_total: Decimal
    gap_amount: Decimal
    participating_rows: int
    is_material: bool
    evidence_json: str

    def __post_init__(self) -> None:
        if self.status not in EVALUATION_STATUSES:
            raise ValueError(f"invalid evaluation status: {self.status!r}")


def _select_rows(
    rows: Sequence[dict[str, Any]],
    side: Any,
) -> list[dict[str, Any]]:
    # Parse once per side, not once per row: this runs over every row of every
    # period for every invariant.
    predicate = parse_predicate(side.select) if side.select is not None else None
    kinds = frozenset(side.kinds)
    result: list[dict[str, Any]] = []
    for row in rows:
        source_type = row.get("source_type") or row.get("dataset_kind") or ""
        if source_type not in kinds:
            continue
        if predicate is not None and not evaluate_predicate(predicate, row):
            continue
        result.append(row)
    return result


def _row_amount(row: dict[str, Any], sign_mode: str) -> Decimal:
    raw = row.get("amount")
    if raw is None:
        return Decimal("0")
    value = amount(raw)
    if sign_mode in ("invert", "invert_expense"):
        return value.copy_negate()
    if sign_mode == "absolute":
        return abs(value)
    return value


def _materiality_reasons(
    gap: Decimal,
    inv: InvariantDefinition,
    participating_rows: Sequence[dict[str, Any]],
    *,
    revenue_basis: Decimal,
    violated: bool,
) -> list[str]:
    """Which materiality legs a violation trips.

    Materiality describes a violation, so a balanced invariant is never
    material. The three legs are independent by design: a hundred rows of 100
    each never reaches a 500 single-item threshold, yet their 10,000 of
    cumulative drift is material, and a small absolute gap can still be
    material relative to period revenue.
    """
    if not violated:
        return []

    reasons: list[str] = []
    abs_gap = abs(gap)
    materiality = inv.materiality

    largest_row = max(
        (
            abs(amount(row.get("amount")))
            for row in participating_rows
            if row.get("amount") is not None
        ),
        default=Decimal("0"),
    )
    if materiality.single_item > 0 and largest_row >= materiality.single_item:
        reasons.append("single_item")
    if materiality.category_cumulative > 0 and abs_gap >= materiality.category_cumulative:
        reasons.append("category_cumulative")
    if (
        materiality.period_revenue_ratio > 0
        and revenue_basis > 0
        and abs_gap / revenue_basis >= materiality.period_revenue_ratio
    ):
        reasons.append("period_revenue_ratio")
    return reasons


def _revenue_basis(rows: Sequence[dict[str, Any]]) -> Decimal:
    """Scale of the largest single side, used as the period revenue proxy."""
    per_kind: dict[str, Decimal] = {}
    for row in rows:
        raw = row.get("amount")
        if raw is None:
            continue
        key = str(row.get("source_type") or row.get("dataset_kind") or "")
        per_kind[key] = per_kind.get(key, Decimal("0")) + abs(amount(raw))
    if not per_kind:
        return Decimal("0")
    return max(per_kind.values())


def _effective_tolerance(inv: InvariantDefinition, base: Decimal) -> Decimal:
    abs_tol = inv.tolerance.absolute
    rel_tol = abs(base) * inv.tolerance.relative
    return max(abs_tol, rel_tol)


def _evaluate_equality(
    rows: Sequence[dict[str, Any]],
    inv: InvariantDefinition,
    *,
    revenue_basis: Decimal = Decimal("0"),
) -> InvariantEvaluation:
    sides = inv.sides
    if "left" not in sides or "right" not in sides:
        return _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "equality requires left and right sides"})

    left_rows = _select_rows(rows, sides["left"])
    right_rows = _select_rows(rows, sides["right"])

    if not left_rows and not right_rows:
        return _make_eval(inv, "insufficient_input", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "no rows matched either side"})

    left_total = sum_money(
        _row_amount(r, sides["left"].sign) for r in left_rows
    ) if left_rows else Decimal("0.0000")
    right_total = sum_money(
        _row_amount(r, sides["right"].sign) for r in right_rows
    ) if right_rows else Decimal("0.0000")

    gap = subtract_money(left_total, right_total)
    participating = len(left_rows) + len(right_rows)
    tol = _effective_tolerance(inv, max(abs(left_total), abs(right_total)))
    passed = abs(gap) <= tol
    reasons = _materiality_reasons(
        gap, inv, left_rows + right_rows,
        revenue_basis=revenue_basis, violated=not passed,
    )

    status = "passed" if passed else "violated"
    return _make_eval(inv, status, left_total, right_total,
                      participating, bool(reasons),
                      {"left_count": len(left_rows), "right_count": len(right_rows),
                       "tolerance": str(tol), "materiality_reasons": reasons})


def _evaluate_conservation(
    rows: Sequence[dict[str, Any]],
    inv: InvariantDefinition,
    *,
    revenue_basis: Decimal = Decimal("0"),
) -> InvariantEvaluation:
    sides = inv.sides
    input_side = sides.get("inputs") or sides.get("left")
    output_side = sides.get("outputs") or sides.get("right")
    if input_side is None or output_side is None:
        return _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "conservation requires inputs and outputs"})

    in_rows = _select_rows(rows, input_side)
    out_rows = _select_rows(rows, output_side)

    if not in_rows and not out_rows:
        return _make_eval(inv, "insufficient_input", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "no rows matched"})

    in_total = sum_money(
        _row_amount(r, input_side.sign) for r in in_rows
    ) if in_rows else Decimal("0.0000")
    out_total = sum_money(
        _row_amount(r, output_side.sign) for r in out_rows
    ) if out_rows else Decimal("0.0000")

    gap = subtract_money(in_total, out_total)
    tol = _effective_tolerance(inv, max(abs(in_total), abs(out_total)))
    passed = abs(gap) <= tol
    participating = len(in_rows) + len(out_rows)
    reasons = _materiality_reasons(
        gap, inv, in_rows + out_rows,
        revenue_basis=revenue_basis, violated=not passed,
    )

    return _make_eval(inv, "passed" if passed else "violated",
                      in_total, out_total, participating, bool(reasons),
                      {"input_count": len(in_rows), "output_count": len(out_rows),
                       "materiality_reasons": reasons})


def _evaluate_proportionality(
    rows: Sequence[dict[str, Any]],
    inv: InvariantDefinition,
    *,
    revenue_basis: Decimal = Decimal("0"),
) -> InvariantEvaluation:
    sides = inv.sides
    base_side = sides.get("base") or sides.get("left")
    target_side = sides.get("target") or sides.get("right")
    if base_side is None or target_side is None:
        return _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "proportionality requires base and target"})

    base_rows = _select_rows(rows, base_side)
    target_rows = _select_rows(rows, target_side)

    if not base_rows or not target_rows:
        return _make_eval(inv, "insufficient_input", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "no rows matched"})

    base_total = sum_money(_row_amount(r, base_side.sign) for r in base_rows)
    target_total = sum_money(_row_amount(r, target_side.sign) for r in target_rows)

    rate = inv.scope.get("rate")
    if rate is not None:
        expected = amount(base_total * Decimal(str(rate)))
        gap = subtract_money(target_total, expected)
    else:
        gap = subtract_money(target_total, base_total)

    tol = _effective_tolerance(inv, abs(base_total))
    passed = abs(gap) <= tol
    participating = len(base_rows) + len(target_rows)
    reasons = _materiality_reasons(
        gap, inv, base_rows + target_rows,
        revenue_basis=revenue_basis, violated=not passed,
    )

    return _make_eval(inv, "passed" if passed else "violated",
                      base_total, target_total, participating, bool(reasons),
                      {"base_count": len(base_rows), "target_count": len(target_rows),
                       "materiality_reasons": reasons})


def _evaluate_uniqueness(
    rows: Sequence[dict[str, Any]],
    inv: InvariantDefinition,
    *,
    revenue_basis: Decimal = Decimal("0"),
) -> InvariantEvaluation:
    side = next(iter(inv.sides.values()), None)
    if side is None:
        return _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "uniqueness requires a side"})

    selected = _select_rows(rows, side)
    if not selected:
        return _make_eval(inv, "insufficient_input", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "no rows matched"})

    key_field = inv.scope.get("key_field", "business_key")
    seen: dict[str, int] = {}
    for row in selected:
        key = str(row.get(key_field, ""))
        seen[key] = seen.get(key, 0) + 1

    duplicates = {k: v for k, v in seen.items() if v > 1}
    passed = len(duplicates) == 0

    return _make_eval(inv, "passed" if passed else "violated",
                      Decimal(str(len(selected))), Decimal(str(len(seen))),
                      len(selected), len(duplicates) > 0,
                      {"duplicate_keys": list(duplicates.keys())[:20],
                       "duplicate_count": len(duplicates)})


def _evaluate_completeness(
    rows: Sequence[dict[str, Any]],
    inv: InvariantDefinition,
    *,
    revenue_basis: Decimal = Decimal("0"),
) -> InvariantEvaluation:
    sides = inv.sides
    if "left" not in sides or "right" not in sides:
        return _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "completeness requires left and right"})

    left_rows = _select_rows(rows, sides["left"])
    right_rows = _select_rows(rows, sides["right"])

    if not left_rows:
        return _make_eval(inv, "insufficient_input", Decimal("0"), Decimal("0"),
                          0, False, {"reason": "no left rows"})

    key_field = inv.scope.get("key_field", "business_key")
    left_keys = {str(r.get(key_field, "")) for r in left_rows}
    right_keys = {str(r.get(key_field, "")) for r in right_rows}

    missing = left_keys - right_keys
    passed = len(missing) == 0
    participating = len(left_rows) + len(right_rows)

    return _make_eval(inv, "passed" if passed else "violated",
                      Decimal(str(len(left_keys))), Decimal(str(len(right_keys))),
                      participating, len(missing) > 0,
                      {"missing_keys": sorted(missing)[:20],
                       "missing_count": len(missing)})


_EVALUATORS = {
    "equality": _evaluate_equality,
    "conservation": _evaluate_conservation,
    "proportionality": _evaluate_proportionality,
    "uniqueness": _evaluate_uniqueness,
    "completeness": _evaluate_completeness,
}


def _make_eval(
    inv: InvariantDefinition,
    status: str,
    left_total: Decimal,
    right_total: Decimal,
    participating_rows: int,
    is_material: bool,
    evidence: dict[str, Any],
) -> InvariantEvaluation:
    gap = (
        subtract_money(left_total, right_total)
        if status != "not_applicable"
        else Decimal("0.0000")
    )
    evidence_json = json.dumps(evidence, ensure_ascii=False, default=str, sort_keys=True)
    return InvariantEvaluation(
        # Content-addressed so that re-running the same inputs produces the
        # same evaluation identity; callers namespace it per run when storing.
        evaluation_id=deterministic_checksum({
            "invariant_id": inv.invariant_id,
            "status": status,
            "left_total": str(left_total),
            "right_total": str(right_total),
            "gap_amount": str(gap),
            "participating_rows": participating_rows,
            "is_material": is_material,
            "evidence": evidence_json,
        }),
        invariant_id=inv.invariant_id,
        status=status,
        left_total=left_total,
        right_total=right_total,
        gap_amount=gap,
        participating_rows=participating_rows,
        is_material=is_material,
        evidence_json=evidence_json,
    )


def evaluate(
    rows: Sequence[dict[str, Any]],
    invariants: Sequence[InvariantDefinition],
) -> tuple[InvariantEvaluation, ...]:
    """Evaluate all invariants against the given rows.

    Pure function.  Returns one ``InvariantEvaluation`` per invariant.
    """
    revenue_basis = _revenue_basis(rows)
    results: list[InvariantEvaluation] = []
    for inv in invariants:
        evaluator = _EVALUATORS.get(inv.family)
        if evaluator is None:
            results.append(
                _make_eval(inv, "not_applicable", Decimal("0"), Decimal("0"),
                           0, False, {"reason": f"unknown family: {inv.family}"})
            )
        else:
            results.append(evaluator(rows, inv, revenue_basis=revenue_basis))
    return tuple(results)
