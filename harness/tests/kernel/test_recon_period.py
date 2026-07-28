from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from commerce_harness.kernel.contract import (
    ReconciliationMode,
    ReconciliationSide,
    taobao_three_way_contract,
    taobao_wallet_contract,
)
from commerce_harness.kernel.period import (
    AccountingPeriod,
    AdjustmentEntry,
    InputRevision,
    InvalidPeriodTransition,
    PeriodLockedError,
    PeriodState,
    latest_revisions,
)
from commerce_harness.kernel.recon import (
    BalanceScope,
    BalanceStatus,
    CashBridgeStatus,
    EvidenceRef,
    LinkKind,
    SettlementCashBridge,
    UnresolvedKind,
    make_item,
    reconcile_items,
)

NOW = datetime(2026, 2, 28, 12, tzinfo=UTC)


def _evidence(file_id: str, row_no: int) -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(file_id, row_no, "amount", "normalize-v1"),)


def _item(
    source_type: str,
    value: str,
    row_no: int,
    key: str = "ORDER-1",
    *,
    settlement_batch_key: str | None = None,
    cash_bridge_key: str | None = None,
):
    contract = taobao_three_way_contract()
    return make_item(
        contract=contract,
        source_type=source_type,
        business_key=key,
        value=value,
        occurred_at=NOW,
        evidence=_evidence(source_type, row_no),
        settlement_batch_key=settlement_batch_key,
        cash_bridge_key=cash_bridge_key,
    )


def _bridge(
    settlement_batch_key: str = "SETTLEMENT-1",
    cash_bridge_key: str = "BANK-REF-1",
) -> SettlementCashBridge:
    return SettlementCashBridge(
        bridge_id=f"bridge-{settlement_batch_key}-{cash_bridge_key}",
        settlement_batch_key=settlement_batch_key,
        cash_bridge_key=cash_bridge_key,
        rule_version="cash-bridge-v1",
        evidence=_evidence("bridge-source", 9),
    )


def test_three_way_reconciliation_supports_one_to_many_and_is_reproducible() -> None:
    contract = taobao_three_way_contract()
    items = [
        _item("baobei_order", "100", 2),
        _item(
            "alipay_ledger",
            "40",
            3,
            settlement_batch_key="SETTLEMENT-1",
        ),
        _item(
            "alipay_ledger",
            "60",
            4,
            settlement_batch_key="SETTLEMENT-1",
        ),
        _item(
            "bank_statement",
            "100",
            5,
            key="BANK-TRANSACTION-9",
            cash_bridge_key="BANK-REF-1",
        ),
    ]
    bridges = [_bridge()]

    first = reconcile_items(
        items,
        contract,
        link_rule_version="taobao-link-v1",
        cash_bridges=bridges,
    )
    second = reconcile_items(
        reversed(items),
        contract,
        link_rule_version="taobao-link-v1",
        cash_bridges=reversed(bridges),
    )

    assert first.checksum() == second.checksum()
    assert first.links[0].kind == LinkKind.ONE_TO_MANY
    order_balance = next(
        balance
        for balance in first.balances
        if balance.scope == BalanceScope.ORDER_PLATFORM
    )
    cash_balance = next(
        balance
        for balance in first.balances
        if balance.scope == BalanceScope.PLATFORM_CASH
    )
    assert order_balance.status == BalanceStatus.BALANCED
    assert order_balance.order_amount == Decimal("100.0000")
    assert cash_balance.status == BalanceStatus.BALANCED
    assert cash_balance.platform_amount == Decimal("100.0000")
    assert cash_balance.cash_amount == Decimal("100.0000")
    assert first.cash_bridge_outcomes[0].status == CashBridgeStatus.LINKED
    assert first.unresolved == ()
    assert len(cash_balance.evidence) == 4


def test_wallet_mode_reconciles_without_inventing_a_bank_cash_leg() -> None:
    contract = taobao_wallet_contract()
    items = [
        make_item(
            contract=contract,
            source_type="baobei_order",
            business_key="ORDER-WALLET-1",
            value="130",
            occurred_at=NOW,
            evidence=_evidence("orders", 2),
        ),
        make_item(
            contract=contract,
            source_type="alipay_ledger",
            business_key="ORDER-WALLET-1",
            value="100",
            occurred_at=NOW,
            evidence=_evidence("alipay", 3),
        ),
        make_item(
            contract=contract,
            source_type="wechat_ledger",
            business_key="ORDER-WALLET-1",
            value="30",
            occurred_at=NOW,
            evidence=_evidence("wechat", 4),
        ),
    ]

    result = reconcile_items(
        items,
        contract,
        link_rule_version="wallet-order-key-v1",
    )

    assert contract.mode == ReconciliationMode.PLATFORM_WALLET
    assert contract.required_sides == {
        ReconciliationSide.ORDER,
        ReconciliationSide.PLATFORM,
    }
    assert contract.requires_cash_bridge is False
    assert result.unresolved == ()
    assert result.cash_bridge_outcomes == ()
    assert len(result.balances) == 1
    assert result.balances[0].status == BalanceStatus.BALANCED
    assert result.balances[0].cash_amount == Decimal("0.0000")
    assert result.balances[0].platform_to_cash_difference == Decimal("0.0000")


def test_reconciliation_exposes_missing_sides_and_amount_mismatch() -> None:
    contract = taobao_three_way_contract()
    missing = reconcile_items(
        [_item("baobei_order", "100", 2)],
        contract,
        link_rule_version="taobao-link-v1",
    )
    assert missing.balances[0].status == BalanceStatus.UNRESOLVED
    assert missing.unresolved[0].kind == UnresolvedKind.MISSING_SIDE
    assert [side.value for side in missing.unresolved[0].missing_sides] == [
        "platform"
    ]

    mismatch = reconcile_items(
        [
            _item("baobei_order", "100", 2),
            _item(
                "alipay_ledger",
                "98",
                3,
                settlement_batch_key="SETTLEMENT-1",
            ),
        ],
        contract,
        link_rule_version="taobao-link-v1",
    )
    assert mismatch.balances[0].status == BalanceStatus.UNRESOLVED
    assert mismatch.unresolved[0].kind == UnresolvedKind.AMOUNT_MISMATCH
    assert mismatch.unresolved[0].absolute_exposure == Decimal("2.0000")


def test_finalized_period_rejects_revisions_and_accepts_audited_adjustments() -> None:
    period = AccountingPeriod("enterprise-1", "store-1", "2026-02")
    original = InputRevision(
        revision_id="orders-r1",
        source_type="orders",
        file_id="file-v1",
        content_checksum="sha-v1",
        received_at=NOW,
    )
    period = period.register_revision(original)
    assert period.register_revision(original) is period

    changed = InputRevision(
        revision_id="orders-r2",
        source_type="orders",
        file_id="file-v2",
        content_checksum="sha-v2",
        received_at=datetime(2026, 3, 1, tzinfo=UTC),
        supersedes_revision_id="orders-r1",
    )
    period = period.register_revision(changed)
    assert latest_revisions(period.revisions) == (changed,)

    locked = period.preclose().finalize(at=NOW)
    assert locked.state == PeriodState.FINALIZED
    with pytest.raises(PeriodLockedError):
        locked.register_revision(
            InputRevision(
                revision_id="orders-r3",
                source_type="orders",
                file_id="file-v3",
                content_checksum="sha-v3",
                received_at=NOW,
                supersedes_revision_id="orders-r2",
            )
        )

    adjustment = AdjustmentEntry(
        adjustment_id="adj-1",
        original_period_key="2026-02",
        amount=Decimal("-12.3400"),
        reason="迟到的平台退款",
        decided_by="finance-owner",
        decided_at=datetime(2026, 3, 5, tzinfo=UTC),
        evidence=_evidence("late-refund", 12),
    )
    restated = locked.post_adjustment(adjustment)
    assert restated.state == PeriodState.RESTATED
    assert restated.net_adjustment == Decimal("-12.3400")
    assert restated.restatement_number == 1
    assert restated.post_adjustment(adjustment) is restated


def test_period_transitions_are_not_skippable() -> None:
    period = AccountingPeriod("enterprise-1", "store-1", "2026-02")
    with pytest.raises(InvalidPeriodTransition):
        period.finalize()
    with pytest.raises(InvalidPeriodTransition):
        period.post_adjustment(
            AdjustmentEntry(
                adjustment_id="adj-early",
                original_period_key="2026-02",
                amount=Decimal("1"),
                reason="not locked",
                decided_by="finance-owner",
                decided_at=NOW,
                evidence=_evidence("decision", 1),
            )
        )
