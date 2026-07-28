from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from .models import strict_decimal


@dataclass(frozen=True, slots=True)
class ReviewedOutcome:
    category: str
    period: str
    correct: bool
    major_amount_error: bool
    exposure: Decimal | str | int

    def __post_init__(self) -> None:
        if not self.category or not self.period:
            raise ValueError("category and period are required")
        object.__setattr__(self, "exposure", strict_decimal(self.exposure))


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    level: str = "L0"
    minimum_periods: int = 2
    minimum_reviews: int = 20
    required_precision: Decimal | str = Decimal("0.995")
    exposure_cap: Decimal | str = Decimal("2000.00")

    def __post_init__(self) -> None:
        if self.level not in {"L0", "L1", "L2"}:
            raise ValueError("level must be L0, L1 or L2")
        object.__setattr__(self, "required_precision", strict_decimal(self.required_precision))
        object.__setattr__(self, "exposure_cap", strict_decimal(self.exposure_cap))


@dataclass(frozen=True, slots=True)
class AutonomyAssessment:
    category: str
    reviewed_count: int
    period_count: int
    precision: Decimal
    major_error_count: int
    exposure: Decimal
    recommended_level: str
    effective_level: str
    reasons: tuple[str, ...]
    requires_governance_approval: bool = field(default=True, init=False)
    may_write_ledger: bool = field(default=False, init=False)


class AutonomyEvaluator:
    """评估自治资格，但不执行升级，更不提供正式账本写入权。"""

    def __init__(self, policy: AutonomyPolicy | None = None) -> None:
        self.policy = policy or AutonomyPolicy()

    def evaluate(
        self,
        category: str,
        outcomes: Iterable[ReviewedOutcome],
    ) -> AutonomyAssessment:
        items = tuple(item for item in outcomes if item.category == category)
        correct = sum(1 for item in items if item.correct)
        precision = Decimal(correct) / Decimal(len(items)) if items else Decimal("0")
        periods = {item.period for item in items}
        major_errors = sum(1 for item in items if item.major_amount_error)
        exposure = sum(
            (abs(cast(Decimal, item.exposure)) for item in items),
            Decimal("0"),
        )
        reasons: list[str] = []
        eligible = True
        if len(items) < self.policy.minimum_reviews:
            eligible = False
            reasons.append("insufficient reviewed samples")
        if len(periods) < self.policy.minimum_periods:
            eligible = False
            reasons.append("insufficient consecutive accounting periods")
        if precision < cast(Decimal, self.policy.required_precision):
            eligible = False
            reasons.append("precision below policy threshold")
        if major_errors:
            eligible = False
            reasons.append("major amount error observed")
        if exposure > cast(Decimal, self.policy.exposure_cap):
            eligible = False
            reasons.append("reviewed exposure exceeds category cap")
        recommended = "L2" if eligible else ("L1" if items and not major_errors else "L0")
        if eligible:
            reasons.append("eligible for governance review only; no automatic elevation")
        # This release is deliberately pinned to L0 even if configuration asks for more.
        effective = "L0"
        if self.policy.level != "L0":
            reasons.append("runtime is safety-pinned to L0 in this release")
        return AutonomyAssessment(
            category=category,
            reviewed_count=len(items),
            period_count=len(periods),
            precision=precision,
            major_error_count=major_errors,
            exposure=exposure,
            recommended_level=recommended,
            effective_level=effective,
            reasons=tuple(reasons),
        )
