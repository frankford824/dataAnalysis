from __future__ import annotations

from decimal import Decimal

import pytest

from commerce_harness.kernel.diff import (
    ComparableCell,
    DiffKind,
    compare_cells,
)
from commerce_harness.kernel.invariants import (
    InvariantViolation,
    assert_amount_conserved,
    assert_detail_matches_summary,
    assert_same_checksum,
)
from commerce_harness.kernel.pnl_view import PnlInput, PnlMetric, build_pnl_view
from commerce_harness.kernel.recon import EvidenceRef


def _evidence(file_id: str, row: int) -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(file_id, row, "amount", "rule-v1"),)


def _pnl(metric: PnlMetric, value: str, row: int, *, certified: bool = True):
    return PnlInput(
        sku="SKU-1",
        sku_name="脱敏商品",
        period_key="2026-02",
        metric=metric,
        amount=Decimal(value),
        balance_id=f"balance-{row}",
        rule_version="pnl-v1",
        evidence=_evidence("ledger", row),
        certified=certified,
    )


def test_pnl_is_a_certified_downstream_view_with_exact_formulas() -> None:
    entries = [
        _pnl(PnlMetric.TRANSACTION_RECEIPT, "1000", 1),
        _pnl(PnlMetric.TRANSACTION_REFUND, "-100", 2),
        _pnl(PnlMetric.TRANSACTION_COMPENSATION, "20", 3),
        _pnl(PnlMetric.SOFTWARE_SERVICE_FEE, "-10", 4),
        _pnl(PnlMetric.MARKETING_EXPENSE, "-30", 5),
        _pnl(PnlMetric.SHIPPING_EXPENSE, "-40", 6),
        _pnl(PnlMetric.ORDER_COST, "-300", 7),
        _pnl(PnlMetric.RESHIP_COST, "-5", 8),
        _pnl(PnlMetric.COMMISSION, "-20", 9),
        _pnl(PnlMetric.PROCUREMENT, "-15", 10),
        _pnl(PnlMetric.ADVERTISING, "-50", 11),
    ]
    row = build_pnl_view(reversed(entries))[0]

    assert row.gross_profit == Decimal("500.0000")
    assert row.gross_margin == Decimal("0.5000")
    assert row.store_profit == Decimal("450.0000")
    assert row.profit_margin == Decimal("0.4500")
    assert len(row.balance_ids) == 11
    assert row.evidence_by_metric[PnlMetric.ORDER_COST][0].row_no == 7
    assert_same_checksum(build_pnl_view(entries), build_pnl_view(reversed(entries)))

    with pytest.raises(ValueError, match="uncertified"):
        build_pnl_view([_pnl(PnlMetric.TRANSACTION_RECEIPT, "1", 12, certified=False)])


def test_conservation_invariants_fail_closed() -> None:
    assert_amount_conserved(["0.3333", "0.6667"], ["1.0000"])
    assert_detail_matches_summary(["10", "-2.5"], "7.5")

    with pytest.raises(InvariantViolation, match="not conserved"):
        assert_amount_conserved(["1"], ["0.99"])
    with pytest.raises(InvariantViolation, match="does not equal"):
        assert_detail_matches_summary(["1", "2"], "4")


def _cell(
    metric: str,
    entity: str,
    value: str,
    row: int,
    rule: str,
) -> ComparableCell:
    return ComparableCell(
        metric=metric,
        entity_key=entity,
        amount=Decimal(value),
        rule_version=rule,
        evidence=_evidence(f"file-{entity}", row),
    )


def test_diff_classifies_amounts_and_keeps_three_level_attribution() -> None:
    current = [
        _cell("profit", "equal", "10", 1, "new-v1"),
        _cell("profit", "tail", "10.0001", 2, "new-v1"),
        _cell("profit", "round", "10.0050", 3, "new-v1"),
        _cell("profit", "true", "10.0200", 4, "new-v2"),
        _cell("profit", "new", "5", 5, "new-v2"),
    ]
    historical = [
        _cell("profit", "equal", "10", 11, "old-v1"),
        _cell("profit", "tail", "10", 12, "old-v1"),
        _cell("profit", "round", "10", 13, "old-v1"),
        _cell("profit", "true", "10", 14, "old-v1"),
        _cell("profit", "old", "7", 15, "old-v1"),
    ]

    findings = {
        finding.entity_key: finding
        for finding in compare_cells(current, historical)
    }
    assert findings["equal"].kind == DiffKind.EQUAL
    assert findings["tail"].kind == DiffKind.FLOAT_TAIL
    assert findings["round"].kind == DiffKind.ROUNDING
    assert findings["true"].kind == DiffKind.TRUE_DIFFERENCE
    assert findings["new"].kind == DiffKind.CURRENT_ONLY
    assert findings["old"].kind == DiffKind.HISTORICAL_ONLY

    attribution = findings["true"].attribution
    assert attribution.metric == "profit"
    assert attribution.rule_versions == ("new-v2", "old-v1")
    assert attribution.source_rows == ("file-true:14", "file-true:4")

