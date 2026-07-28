"""Verified three-branch main-order allocation rule."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from .money import MONEY_PRECISION, MONEY_QUANTUM, amount, sum_money
from .recon import EvidenceRef


class AllocationBranch(StrEnum):
    NET_REVENUE = "net_revenue"
    BUYER_PAID = "buyer_paid"
    EQUAL = "equal"


@dataclass(frozen=True, slots=True)
class OrderLine:
    parent_order_id: str
    line_id: str
    net_revenue: Decimal
    buyer_paid: Decimal
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.parent_order_id or not self.line_id:
            raise ValueError("parent_order_id and line_id are required")
        if not self.evidence:
            raise ValueError("allocation lines require source evidence")
        object.__setattr__(self, "net_revenue", amount(self.net_revenue))
        object.__setattr__(self, "buyer_paid", amount(self.buyer_paid))


@dataclass(frozen=True, slots=True)
class LineAllocation:
    parent_order_id: str
    line_id: str
    branch: AllocationBranch
    weight: Decimal
    allocated_amount: Decimal
    rule_version: str
    evidence: tuple[EvidenceRef, ...]


def allocate_order(
    total: str | int | Decimal,
    lines: Iterable[OrderLine],
    *,
    rule_version: str = "order-allocation-v1",
) -> tuple[LineAllocation, ...]:
    """Allocate a main-order amount and preserve the total exactly.

    Branches:
    1. positive main-order net revenue → child net-revenue proportions;
    2. non-positive net revenue but non-zero buyer-paid total → buyer-paid;
    3. zero buyer-paid total → equal allocation.
    """

    if not rule_version:
        raise ValueError("rule_version is required")
    ordered = tuple(sorted(lines, key=lambda line: line.line_id))
    if not ordered:
        raise ValueError("at least one order line is required")
    parent_ids = {line.parent_order_id for line in ordered}
    if len(parent_ids) != 1:
        raise ValueError("all lines must belong to the same parent order")
    if len({line.line_id for line in ordered}) != len(ordered):
        raise ValueError("line_id must be unique within an order")

    target = amount(total)
    net_total = sum_money(line.net_revenue for line in ordered)
    buyer_paid_total = sum_money(line.buyer_paid for line in ordered)
    if net_total > 0:
        branch = AllocationBranch.NET_REVENUE
        raw_weights = tuple(line.net_revenue for line in ordered)
        denominator = net_total
    elif buyer_paid_total != 0:
        branch = AllocationBranch.BUYER_PAID
        raw_weights = tuple(line.buyer_paid for line in ordered)
        denominator = buyer_paid_total
    else:
        branch = AllocationBranch.EQUAL
        raw_weights = tuple(Decimal(1) for _ in ordered)
        denominator = Decimal(len(ordered))

    allocations: list[LineAllocation] = []
    allocated_so_far = Decimal("0.0000")
    with localcontext() as context:
        context.prec = MONEY_PRECISION + 12
        for index, (line, raw_weight) in enumerate(
            zip(ordered, raw_weights, strict=True)
        ):
            weight = raw_weight / denominator
            if index == len(ordered) - 1:
                allocated = amount(target - allocated_so_far)
            else:
                allocated = amount((target * weight).quantize(MONEY_QUANTUM))
                allocated_so_far += allocated
            allocations.append(
                LineAllocation(
                    parent_order_id=line.parent_order_id,
                    line_id=line.line_id,
                    branch=branch,
                    weight=weight,
                    allocated_amount=allocated,
                    rule_version=rule_version,
                    evidence=line.evidence,
                )
            )

    if sum_money(entry.allocated_amount for entry in allocations) != target:
        raise AssertionError("allocation failed amount conservation")
    return tuple(allocations)
