from __future__ import annotations

from decimal import Decimal

from commerce_harness.experiment.first_hypothesis import (
    build_first_hypothesis,
    first_hypothesis_scope,
    load_platform_fee_route_rule,
)
from commerce_harness.spec.rule import apply_route_rules, parse_rule


def test_first_hypothesis_is_route_rule() -> None:
    hypothesis = build_first_hypothesis()
    assert hypothesis["kind"] == "rule_add"
    assert hypothesis["period_token"] == "2602"
    rule = parse_rule(hypothesis["rule"])
    assert rule.action == "route"
    assert rule.participation == "legal_single_sided"
    assert rule.posting_target == "platform_fee"


def test_scope_locates_the_frozen_inputs() -> None:
    scope = first_hypothesis_scope()
    assert scope == {"period_token": "2602", "store_id": "store_xibishun"}


def test_platform_fee_rule_routes_only_service_fee_rows() -> None:
    rule = parse_rule(load_platform_fee_route_rule())
    rows = [
        {
            "source_type": "alipay_ledger",
            "amount": "-1.8300",
            "attributes": {"business_description": "软件服务费"},
        },
        {
            "source_type": "alipay_ledger",
            "amount": "100.0000",
            "attributes": {"business_description": "交易收款"},
        },
        {
            "source_type": "baobei_order",
            "amount": "100.0000",
            "attributes": {},
        },
    ]
    two_sided, routed = apply_route_rules(rows, [rule])
    assert len(two_sided) == 2
    assert [entry.posting_target for entry in routed] == ["platform_fee"]
    assert Decimal(str(routed[0].row["amount"])) == Decimal("-1.8300")
