from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from commerce_harness.kernel.allocate import (
    AllocationBranch,
    OrderLine,
    allocate_order,
)
from commerce_harness.kernel.cost_asof import (
    CostLookupError,
    CostOverlapError,
    CostVersion,
    cost_as_of,
    validate_cost_intervals,
)
from commerce_harness.kernel.recon import EvidenceRef

EVIDENCE = (EvidenceRef("file-orders", 2, "amount", "rule-v1"),)


def _line(
    line_id: str,
    net: str,
    paid: str,
    *,
    parent: str = "P-1",
) -> OrderLine:
    return OrderLine(
        parent_order_id=parent,
        line_id=line_id,
        net_revenue=Decimal(net),
        buyer_paid=Decimal(paid),
        evidence=EVIDENCE,
    )


def test_order_allocation_uses_all_three_verified_branches_and_conserves_amount() -> None:
    net = allocate_order(
        "10.0000",
        [_line("B", "75", "80"), _line("A", "25", "20")],
    )
    assert [entry.line_id for entry in net] == ["A", "B"]
    assert {entry.branch for entry in net} == {AllocationBranch.NET_REVENUE}
    assert [entry.allocated_amount for entry in net] == [
        Decimal("2.5000"),
        Decimal("7.5000"),
    ]

    paid = allocate_order(
        "9.0000",
        [_line("A", "-20", "20"), _line("B", "0", "10")],
    )
    assert {entry.branch for entry in paid} == {AllocationBranch.BUYER_PAID}
    assert [entry.allocated_amount for entry in paid] == [
        Decimal("6.0000"),
        Decimal("3.0000"),
    ]

    equal = allocate_order(
        "1.0000",
        [_line("C", "0", "0"), _line("A", "0", "0"), _line("B", "0", "0")],
    )
    assert {entry.branch for entry in equal} == {AllocationBranch.EQUAL}
    assert sum((entry.allocated_amount for entry in equal), Decimal()) == Decimal(
        "1.0000"
    )
    assert [entry.allocated_amount for entry in equal] == [
        Decimal("0.3333"),
        Decimal("0.3333"),
        Decimal("0.3334"),
    ]


def test_cost_asof_uses_half_open_intervals_and_rejects_overlap() -> None:
    jan = CostVersion(
        sku="SKU-1",
        unit_cost=Decimal("10.0000"),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=datetime(2026, 2, 1, tzinfo=UTC),
        version="cost-v1",
        evidence=EVIDENCE,
    )
    feb = CostVersion(
        sku="SKU-1",
        unit_cost=Decimal("12.0000"),
        effective_from=datetime(2026, 2, 1, tzinfo=UTC),
        effective_to=None,
        version="cost-v2",
        evidence=EVIDENCE,
    )
    validate_cost_intervals([feb, jan])
    assert cost_as_of(
        "SKU-1",
        datetime(2026, 1, 31, 23, 59, tzinfo=UTC),
        [jan, feb],
    ).cost_version == "cost-v1"
    assert cost_as_of(
        "SKU-1",
        datetime(2026, 2, 1, tzinfo=UTC),
        [jan, feb],
    ).unit_cost == Decimal("12.0000")
    with pytest.raises(CostLookupError, match="no effective cost"):
        cost_as_of(
            "UNKNOWN",
            datetime(2026, 2, 1, tzinfo=UTC),
            [jan, feb],
        )

    overlap = CostVersion(
        sku="SKU-1",
        unit_cost=Decimal("11"),
        effective_from=datetime(2026, 1, 15, tzinfo=UTC),
        effective_to=datetime(2026, 2, 15, tzinfo=UTC),
        version="cost-overlap",
        evidence=EVIDENCE,
    )
    with pytest.raises(CostOverlapError):
        validate_cost_intervals([jan, overlap])

