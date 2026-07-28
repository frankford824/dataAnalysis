"""Compute the metric set from before/after reconciliation summaries.

All amounts are ``Decimal``; ``float`` is forbidden.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from commerce_harness.kernel.money import amount, subtract_money

_ZERO = Decimal("0.0000")

METRIC_NAMES = frozenset({
    "unresolved_count",
    "unresolved_amount_abs",
    "line_auto_rate",
    "amount_weighted_auto_rate",
    "explained_amount_ratio",
    "control_total_gap",
    "invariant_pass_delta",
    "major_reversal_count",
    "newly_unresolved_count",
    "baseline_regression_count",
    "evidence_integrity_failures",
    "certifiable",
    "profit_completeness",
    "claim_acceptance_rate",
})


def _get(summary: dict[str, Any], key: str, default: Any = _ZERO) -> Decimal:
    val = summary.get(key, default)
    if isinstance(val, float):
        raise TypeError(f"float forbidden for metric {key!r}")
    if val is None:
        return _ZERO
    return amount(val)


def compute_metrics(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, dict[str, Decimal]]:
    """Compute the metric set from before/after summary dicts.

    Returns a dict keyed by metric name, each with ``before``, ``after``,
    and ``delta`` values.
    """
    metrics: dict[str, dict[str, Decimal]] = {}

    for key in ("unresolved_count", "unresolved_amount_abs"):
        b = _get(before, key)
        a = _get(after, key)
        metrics[key] = {"before": b, "after": a, "delta": subtract_money(a, b)}

    for key in ("line_auto_rate", "amount_weighted_auto_rate", "explained_amount_ratio"):
        b = _get(before, key)
        a = _get(after, key)
        metrics[key] = {"before": b, "after": a, "delta": subtract_money(a, b)}

    b_gap = _get(before, "control_total_gap")
    a_gap = _get(after, "control_total_gap")
    metrics["control_total_gap"] = {
        "before": b_gap, "after": a_gap,
        "delta": subtract_money(abs(a_gap), abs(b_gap)),
    }

    b_pass = _get(before, "invariant_pass_delta")
    a_pass = _get(after, "invariant_pass_delta")
    metrics["invariant_pass_delta"] = {
        "before": b_pass, "after": a_pass,
        "delta": subtract_money(a_pass, b_pass),
    }

    for key in (
        "major_reversal_count",
        "newly_unresolved_count",
        "baseline_regression_count",
        "evidence_integrity_failures",
    ):
        b = _get(before, key, _ZERO)
        a = _get(after, key, _ZERO)
        metrics[key] = {"before": b, "after": a, "delta": subtract_money(a, b)}

    b_cert = _get(before, "certifiable", _ZERO)
    a_cert = _get(after, "certifiable", _ZERO)
    metrics["certifiable"] = {
        "before": b_cert, "after": a_cert, "delta": subtract_money(a_cert, b_cert),
    }

    b_pc = _get(before, "profit_completeness", _ZERO)
    a_pc = _get(after, "profit_completeness", _ZERO)
    metrics["profit_completeness"] = {
        "before": b_pc, "after": a_pc, "delta": subtract_money(a_pc, b_pc),
    }

    b_car = _get(before, "claim_acceptance_rate", _ZERO)
    a_car = _get(after, "claim_acceptance_rate", _ZERO)
    metrics["claim_acceptance_rate"] = {
        "before": b_car, "after": a_car, "delta": subtract_money(a_car, b_car),
    }

    return metrics
