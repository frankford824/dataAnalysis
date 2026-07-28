"""Pure verdict function for counterfactual experiments.

Verdict is ``rejected``, ``supported``, or ``inconclusive``.
Deterministic — no model participation.
"""

from __future__ import annotations

from decimal import Decimal

_ZERO = Decimal("0")
_NEWLY_UNRESOLVED_THRESHOLD = Decimal("5")
_CONTROL_GAP_TOLERANCE = Decimal("0.0100")


def decide_verdict(
    metrics: dict[str, dict[str, Decimal]],
    *,
    period_locked: bool = False,
    checksum_stable: bool = True,
) -> tuple[str, list[str]]:
    """Decide experiment verdict from metrics.

    Returns ``(verdict, reasons)`` where verdict is one of
    ``rejected`` / ``supported`` / ``inconclusive``.

    An experiment is *rejected* when it makes the books worse or cannot be
    reproduced, *supported* when it improves at least one primary measure while
    making nothing worse, and *inconclusive* when it is merely neutral.
    """
    reasons: list[str] = []

    def _after(key: str) -> Decimal:
        value = metrics.get(key, {}).get("after", _ZERO)
        return value if isinstance(value, Decimal) else Decimal(str(value))

    def _delta(key: str) -> Decimal:
        value = metrics.get(key, {}).get("delta", _ZERO)
        return value if isinstance(value, Decimal) else Decimal(str(value))

    if _after("major_reversal_count") > _ZERO:
        reasons.append("存在重大金额反转")
        return "rejected", reasons

    if period_locked and _after("baseline_regression_count") > _ZERO:
        reasons.append("已终结账期出现基线回归")
        return "rejected", reasons

    if _after("evidence_integrity_failures") > _ZERO:
        reasons.append("证据完整性校验失败")
        return "rejected", reasons

    if not checksum_stable:
        reasons.append("重复执行输出哈希不一致")
        return "rejected", reasons

    unresolved_amount_delta = _delta("unresolved_amount_abs")
    newly_unresolved = _after("newly_unresolved_count")
    control_gap_delta = _delta("control_total_gap")
    amount_auto_delta = _delta("amount_weighted_auto_rate")
    explained_delta = _delta("explained_amount_ratio")

    # Regressions are rejections, not "inconclusive": a hypothesis that adds
    # unexplained money or widens a control gap has been falsified.
    if unresolved_amount_delta > _ZERO:
        reasons.append("未决金额上升")
        return "rejected", reasons

    if control_gap_delta > _CONTROL_GAP_TOLERANCE:
        reasons.append("控制总额差扩大")
        return "rejected", reasons

    if newly_unresolved > _NEWLY_UNRESOLVED_THRESHOLD:
        reasons.append(f"新引入未决数 {newly_unresolved} 超过阈值")
        return "rejected", reasons

    if amount_auto_delta < _ZERO:
        reasons.append("金额加权自动率下降")
        return "rejected", reasons

    claim_acceptance_delta = _delta("claim_acceptance_rate")
    if "claim_acceptance_rate" in metrics and claim_acceptance_delta < _ZERO:
        reasons.append("申诉接受率下降")
        return "rejected", reasons

    # Support needs a measured improvement on at least one primary measure.
    # Amount-weighted rates lead, because a line-count improvement can hide a
    # large-amount regression.
    improvements: list[str] = []
    if unresolved_amount_delta < _ZERO:
        improvements.append("未决金额下降")
    if amount_auto_delta > _ZERO:
        improvements.append("金额加权自动率提升")
    if explained_delta > _ZERO:
        improvements.append("已解释金额占比提升")
    if control_gap_delta < _ZERO:
        improvements.append("控制总额差收敛")

    if improvements:
        reasons.extend(improvements)
        if newly_unresolved > _ZERO:
            reasons.append(f"新引入未决数 {newly_unresolved} 在阈值内")
        return "supported", reasons

    reasons.append("各项主口径均无可测量变化")
    return "inconclusive", reasons
