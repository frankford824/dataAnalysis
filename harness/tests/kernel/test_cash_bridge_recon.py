from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from commerce_harness.kernel.contract import taobao_three_way_contract
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


def _evidence(
    file_id: str,
    row_no: int,
    *,
    rule_version: str = "normalize-v2",
) -> tuple[EvidenceRef, ...]:
    return (
        EvidenceRef(
            file_id=file_id,
            row_no=row_no,
            field="amount",
            rule_version=rule_version,
            source_value=f"source-{row_no}",
        ),
    )


def _item(
    source_type: str,
    value: str,
    row_no: int,
    business_key: str,
    *,
    settlement_batch_key: str | None = None,
    cash_bridge_key: str | None = None,
):
    return make_item(
        contract=taobao_three_way_contract(),
        source_type=source_type,
        business_key=business_key,
        value=value,
        occurred_at=NOW,
        evidence=_evidence(source_type, row_no),
        settlement_batch_key=settlement_batch_key,
        cash_bridge_key=cash_bridge_key,
    )


def _bridge(
    settlement_batch_key: str,
    cash_bridge_key: str,
    row_no: int,
) -> SettlementCashBridge:
    return SettlementCashBridge(
        bridge_id=f"bridge-{row_no}",
        settlement_batch_key=settlement_batch_key,
        cash_bridge_key=cash_bridge_key,
        rule_version="settlement-bank-bridge-v3",
        evidence=_evidence(
            "settlement-bridge",
            row_no,
            rule_version="settlement-bank-bridge-v3",
        ),
    )


def _cash_balance(result):
    return [
        balance
        for balance in result.balances
        if balance.scope == BalanceScope.PLATFORM_CASH
    ]


def test_real_three_leg_keys_differ_and_preserve_evidence_and_rule_version() -> None:
    items = [
        _item("baobei_order", "100", 1, "ORDER-1001"),
        _item(
            "alipay_ledger",
            "100",
            2,
            "ORDER-1001",
            settlement_batch_key="ALIPAY-BATCH-77",
        ),
        _item(
            "bank_statement",
            "100",
            3,
            "BANK-TX-9008",
            cash_bridge_key="BANK-MEMO-77",
        ),
    ]
    bridge = _bridge("ALIPAY-BATCH-77", "BANK-MEMO-77", 4)

    result = reconcile_items(
        items,
        taobao_three_way_contract(),
        link_rule_version="order-platform-link-v2",
        cash_bridges=[bridge],
    )

    assert result.unresolved == ()
    assert result.cash_bridge_outcomes[0].status == CashBridgeStatus.LINKED
    assert result.cash_bridge_outcomes[0].rule_versions == (
        "settlement-bank-bridge-v3",
    )
    assert result.cash_bridge_outcomes[0].bridge_ids == ("bridge-4",)
    assert {
        (entry.file_id, entry.row_no, entry.rule_version)
        for entry in result.cash_bridge_outcomes[0].evidence
    } == {
        ("alipay_ledger", 2, "normalize-v2"),
        ("bank_statement", 3, "normalize-v2"),
        ("settlement-bridge", 4, "settlement-bank-bridge-v3"),
    }
    assert _cash_balance(result)[0].status == BalanceStatus.BALANCED


def test_cash_bridge_supports_one_to_many_and_many_to_one() -> None:
    contract = taobao_three_way_contract()
    one_to_many = reconcile_items(
        [
            _item("baobei_order", "100", 1, "ORDER-1"),
            _item(
                "alipay_ledger",
                "100",
                2,
                "ORDER-1",
                settlement_batch_key="BATCH-1",
            ),
            _item(
                "bank_statement",
                "40",
                3,
                "BANK-TX-1",
                cash_bridge_key="CASH-1",
            ),
            _item(
                "bank_statement",
                "60",
                4,
                "BANK-TX-2",
                cash_bridge_key="CASH-2",
            ),
        ],
        contract,
        link_rule_version="order-link-v1",
        cash_bridges=[
            _bridge("BATCH-1", "CASH-1", 10),
            _bridge("BATCH-1", "CASH-2", 11),
        ],
    )
    one_to_many_link = next(
        link
        for link in one_to_many.links
        if link.scope == BalanceScope.PLATFORM_CASH
    )
    assert one_to_many_link.kind == LinkKind.ONE_TO_MANY
    assert one_to_many.cash_bridge_outcomes[0].status == CashBridgeStatus.LINKED
    assert one_to_many.cash_bridge_outcomes[0].cash_amount == Decimal("100.0000")

    many_to_one = reconcile_items(
        [
            _item("baobei_order", "40", 20, "ORDER-20"),
            _item(
                "alipay_ledger",
                "40",
                21,
                "ORDER-20",
                settlement_batch_key="BATCH-20",
            ),
            _item("baobei_order", "60", 22, "ORDER-21"),
            _item(
                "alipay_ledger",
                "60",
                23,
                "ORDER-21",
                settlement_batch_key="BATCH-21",
            ),
            _item(
                "bank_statement",
                "100",
                24,
                "BANK-TX-20",
                cash_bridge_key="CASH-20",
            ),
        ],
        contract,
        link_rule_version="order-link-v1",
        cash_bridges=[
            _bridge("BATCH-20", "CASH-20", 30),
            _bridge("BATCH-21", "CASH-20", 31),
        ],
    )
    many_to_one_link = next(
        link
        for link in many_to_one.links
        if link.scope == BalanceScope.PLATFORM_CASH
    )
    assert many_to_one_link.kind == LinkKind.MANY_TO_ONE
    assert many_to_one.cash_bridge_outcomes[0].status == CashBridgeStatus.LINKED
    assert many_to_one.cash_bridge_outcomes[0].platform_amount == Decimal(
        "100.0000"
    )


def test_missing_bridge_is_deterministic_unresolved_item() -> None:
    result = reconcile_items(
        [
            _item("baobei_order", "88", 1, "ORDER-88"),
            _item(
                "alipay_ledger",
                "88",
                2,
                "ORDER-88",
                settlement_batch_key="UNMAPPED-BATCH",
            ),
        ],
        taobao_three_way_contract(),
        link_rule_version="order-link-v1",
    )

    assert result.cash_bridge_outcomes[0].status == CashBridgeStatus.MISSING
    unresolved = next(
        item
        for item in result.unresolved
        if item.kind == UnresolvedKind.MISSING_CASH_BRIDGE
    )
    assert unresolved.settlement_batch_keys == ("UNMAPPED-BATCH",)
    assert unresolved.missing_sides == (
        taobao_three_way_contract().requirement("bank_statement").side,
    )
    assert unresolved.absolute_exposure == Decimal("88.0000")


def test_missing_bridge_groups_repeated_platform_rows_by_business_key() -> None:
    result = reconcile_items(
        [
            _item("alipay_ledger", "70", 1, "ORDER-REPEATED"),
            _item("alipay_ledger", "30", 2, "ORDER-REPEATED"),
        ],
        taobao_three_way_contract(),
        link_rule_version="order-link-v1",
    )
    platform_cash = _cash_balance(result)

    assert len(platform_cash) == 1
    assert platform_cash[0].business_key == "ORDER-REPEATED"
    assert platform_cash[0].platform_amount == Decimal("100.0000")
    assert len(platform_cash[0].item_ids) == 2


def test_many_to_many_bridge_is_ambiguous_and_never_auto_linked() -> None:
    result = reconcile_items(
        [
            _item(
                "alipay_ledger",
                "40",
                1,
                "ORDER-A",
                settlement_batch_key="BATCH-A",
            ),
            _item(
                "alipay_ledger",
                "60",
                2,
                "ORDER-B",
                settlement_batch_key="BATCH-B",
            ),
            _item(
                "bank_statement",
                "40",
                3,
                "BANK-A",
                cash_bridge_key="CASH-A",
            ),
            _item(
                "bank_statement",
                "60",
                4,
                "BANK-B",
                cash_bridge_key="CASH-B",
            ),
        ],
        taobao_three_way_contract(),
        link_rule_version="order-link-v1",
        cash_bridges=[
            _bridge("BATCH-A", "CASH-A", 10),
            _bridge("BATCH-A", "CASH-B", 11),
            _bridge("BATCH-B", "CASH-B", 12),
        ],
    )

    assert result.cash_bridge_outcomes[0].status == CashBridgeStatus.AMBIGUOUS
    assert any(
        item.kind == UnresolvedKind.AMBIGUOUS_CASH_BRIDGE
        for item in result.unresolved
    )
    assert not any(
        link.scope == BalanceScope.PLATFORM_CASH for link in result.links
    )


def test_cash_amount_difference_is_decimal_and_unresolved() -> None:
    result = reconcile_items(
        [
            _item("baobei_order", "100.0000", 1, "ORDER-1"),
            _item(
                "alipay_ledger",
                "100.0000",
                2,
                "ORDER-1",
                settlement_batch_key="BATCH-1",
            ),
            _item(
                "bank_statement",
                "99.9900",
                3,
                "BANK-TX-1",
                cash_bridge_key="CASH-1",
            ),
        ],
        taobao_three_way_contract(),
        link_rule_version="order-link-v1",
        cash_bridges=[_bridge("BATCH-1", "CASH-1", 10)],
        tolerance="0.0000",
    )

    outcome = result.cash_bridge_outcomes[0]
    assert outcome.status == CashBridgeStatus.AMOUNT_MISMATCH
    assert outcome.difference == Decimal("-0.0100")
    unresolved = next(
        item
        for item in result.unresolved
        if item.kind == UnresolvedKind.CASH_AMOUNT_MISMATCH
    )
    assert unresolved.absolute_exposure == Decimal("0.0100")
    assert unresolved.rule_versions == ("settlement-bank-bridge-v3",)
