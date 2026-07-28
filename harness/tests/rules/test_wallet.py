from __future__ import annotations

from decimal import Decimal

from commerce_harness.rules.wallet import WalletRecord, WalletRuleSet

TAOBAO_ORDER_ID = "1234567890123456789"
SECOND_ORDER_ID = "9876543210987654321"


def test_ruleset_is_versioned_and_checksum_is_deterministic() -> None:
    first = WalletRuleSet()
    second = WalletRuleSet()

    assert first.version == "1.0.0"
    assert first.checksum == second.checksum
    assert len(first.checksum) == 64
    assert all(rule.version == first.version for rule in first.classification_rules)
    assert all(rule.version == first.version for rule in first.order_key_rules)


def test_extracts_plain_taobao_order_id() -> None:
    result = WalletRuleSet().extract_order_key(
        merchant_order_id=TAOBAO_ORDER_ID,
        remark=None,
        occurred_at_text=None,
        classification_value=None,
    )

    assert result.matched is True
    assert result.value == TAOBAO_ORDER_ID
    assert result.value_kind == "order_key"
    assert result.rule_id == "wallet.order_key.taobao_19"
    assert result.rule_version == "1.0.0"
    assert result.evidence_fields == ("merchant_order_id",)


def test_t200p_prefix_and_embedded_reference_normalize_to_same_key() -> None:
    rules = WalletRuleSet()
    prefixed = rules.extract_order_key(
        merchant_order_id=f"T200P{TAOBAO_ORDER_ID}",
        remark=None,
        occurred_at_text=None,
        classification_value=None,
    )
    embedded = rules.extract_order_key(
        merchant_order_id=None,
        remark=f"平台参考 T200P{TAOBAO_ORDER_ID}",
        occurred_at_text=None,
        classification_value=None,
    )

    assert prefixed.value == TAOBAO_ORDER_ID
    assert embedded.value == TAOBAO_ORDER_ID
    assert prefixed.rule_id == embedded.rule_id == "wallet.order_key.t200p"
    assert embedded.evidence_fields == ("remark",)


def test_multiple_t200p_references_remain_unmatched() -> None:
    result = WalletRuleSet().extract_order_key(
        merchant_order_id=None,
        remark=f"T200P{TAOBAO_ORDER_ID} T200P{SECOND_ORDER_ID}",
        occurred_at_text=None,
        classification_value=None,
    )

    assert result.matched is False
    assert result.unmatched_reason == "ambiguous_t200p_order_ids"


def test_deposit_recharge_extracts_formula_proven_order_number() -> None:
    rules = WalletRuleSet()
    remark = f"平台保证金充值，订单编号：{TAOBAO_ORDER_ID}"
    classification = rules.classify_business_description(remark)
    result = rules.extract_order_key(
        merchant_order_id=None,
        remark=remark,
        occurred_at_text=None,
        classification_value=classification.value,
    )

    assert classification.value == "保证金充值"
    assert result.value == TAOBAO_ORDER_ID
    assert result.rule_id == "wallet.order_key.deposit_remark"
    assert result.evidence_fields == ("remark", "classification")


def test_other_income_and_deposit_shortfall_use_audited_qt_grouping_key() -> None:
    rules = WalletRuleSet()
    for remark in ("淘宝平台提现完成", "保证金额度不足充值"):
        classification = rules.classify_business_description(remark)
        result = rules.extract_order_key(
            merchant_order_id=None,
            remark=remark,
            occurred_at_text="2026-04-30 08:00:00",
            classification_value=classification.value,
        )

        assert result.value == "QT2026-"
        assert result.value_kind == "legacy_grouping_key"
        assert result.rule_id == "wallet.order_key.qt_legacy"


def test_classification_respects_formula_priority() -> None:
    result = WalletRuleSet().classify_business_description(
        "订单保证金充值后打款"
    )

    assert result.value == "0010001|交易收款-交易收款"
    assert result.rule_id == "wallet.classify.order_payment"


def test_every_declared_classification_rule_is_reachable_in_priority_order() -> None:
    rules = WalletRuleSet()
    regex_examples = {
        "wallet.classify.order_payment": "订单完成打款",
        "wallet.classify.order_refund": "订单完成退款",
        "wallet.classify.deposit_control_transfer": "订单触发保证金管控资金使用",
    }

    for rule in rules.classification_rules:
        if rule.rule_id in regex_examples:
            text = regex_examples[rule.rule_id]
        elif rule.operator.value == "starts_with":
            text = f"{rule.pattern}附加说明"
        elif rule.operator.value == "contains":
            text = f"前缀{rule.pattern}后缀"
        else:
            text = rule.pattern

        result = rules.classify_business_description(text)
        assert result.rule_id == rule.rule_id
        assert result.value == rule.output


def test_unknown_business_text_remains_unmatched() -> None:
    result = WalletRuleSet().classify_business_description("未定义渠道说明")

    assert result.matched is False
    assert result.value is None
    assert result.rule_id is None
    assert result.unmatched_reason == "no_classification_rule_matched"
    assert result.evidence_fields == ("remark",)


def test_decimal_amount_is_not_modified_by_rule_evaluation() -> None:
    amount = Decimal("9999999999999999.1234")
    evaluation = WalletRuleSet().evaluate(
        WalletRecord(
            amount=amount,
            merchant_order_id=f"T200P{TAOBAO_ORDER_ID}",
            remark="订单完成打款",
            occurred_at_text="2026-04-01 00:00:00",
        )
    )

    assert evaluation.amount is amount
    assert evaluation.amount.as_tuple() == amount.as_tuple()
    assert evaluation.classification.matched is True
    assert evaluation.order_key.value == TAOBAO_ORDER_ID
