"""Tests for the rule DSL with focus on route action."""

from __future__ import annotations

import pytest

from commerce_harness.spec.rule import (
    apply_route_rules,
    parse_rule,
)


def _route_rule(**overrides) -> dict:
    base = {
        "action": "route",
        "select": {
            "all_of": [
                {"field": "source_type", "op": "eq", "value": "alipay_ledger"},
                {"field": "amount", "op": "sign", "value": "negative"},
                {"field": "attributes.description", "op": "in",
                 "value": ["软件服务费", "技术服务费", "佣金"]},
            ],
        },
        "participation": "legal_single_sided",
        "posting_target": "platform_fee",
        "rationale": "平台单边费用",
    }
    base.update(overrides)
    return base


class TestParseRule:
    def test_parse_route(self):
        rule = parse_rule(_route_rule())
        assert rule.action == "route"
        assert rule.participation == "legal_single_sided"
        assert rule.posting_target == "platform_fee"
        assert rule.rule_id

    def test_route_checksum_deterministic(self):
        r1 = parse_rule(_route_rule())
        r2 = parse_rule(_route_rule())
        assert r1.checksum() == r2.checksum()

    def test_route_checksum_changes(self):
        r1 = parse_rule(_route_rule())
        r2 = parse_rule(_route_rule(posting_target="other_fee"))
        assert r1.checksum() != r2.checksum()

    def test_invalid_participation(self):
        with pytest.raises(ValueError, match="participation"):
            parse_rule(_route_rule(participation="invalid"))

    def test_legal_single_sided_requires_target(self):
        with pytest.raises(ValueError, match="posting_target"):
            parse_rule(_route_rule(participation="legal_single_sided", posting_target=None))

    def test_two_sided_no_target_required(self):
        rule = parse_rule(_route_rule(participation="two_sided", posting_target=None))
        assert rule.participation == "two_sided"

    def test_excluded(self):
        rule = parse_rule(_route_rule(participation="excluded", posting_target=None))
        assert rule.participation == "excluded"

    def test_invalid_action(self):
        with pytest.raises(ValueError, match="action"):
            parse_rule({
                "action": "invalid",
                "select": {"field": "source_type", "op": "eq", "value": "x"},
            })

    def test_missing_select(self):
        with pytest.raises(ValueError, match="select"):
            parse_rule({"action": "route", "participation": "two_sided"})

    def test_parse_classify(self):
        rule = parse_rule({
            "action": "classify",
            "select": {"field": "source_type", "op": "eq", "value": "x"},
            "category": "platform_fee",
        })
        assert rule.action == "classify"

    def test_parse_extract(self):
        rule = parse_rule({
            "action": "extract",
            "select": {"field": "source_type", "op": "eq", "value": "x"},
            "source_field": "raw_text",
            "target_field": "order_id",
            "shape": "D{19}",
        })
        assert rule.action == "extract"

    def test_parse_map(self):
        rule = parse_rule({
            "action": "map",
            "select": {"field": "source_type", "op": "eq", "value": "x"},
            "lookup_table": {"A": "B"},
        })
        assert rule.action == "map"

    def test_parse_derive(self):
        rule = parse_rule({
            "action": "derive",
            "select": {"field": "source_type", "op": "eq", "value": "x"},
            "formula": "cost * rate",
        })
        assert rule.action == "derive"


class TestApplyRouteRules:
    def test_matching_rows_routed(self):
        rule = parse_rule(_route_rule())
        rows = [
            {"source_type": "alipay_ledger", "amount": "-5.00",
             "attributes": {"description": "技术服务费"}},
            {"source_type": "orders", "amount": "100.00", "attributes": {}},
        ]
        two_sided, routed = apply_route_rules(rows, [rule])
        assert len(two_sided) == 1
        assert two_sided[0]["source_type"] == "orders"
        assert len(routed) == 1
        assert routed[0].participation == "legal_single_sided"
        assert routed[0].posting_target == "platform_fee"

    def test_no_match_stays_two_sided(self):
        rule = parse_rule(_route_rule())
        rows = [
            {"source_type": "orders", "amount": "100.00", "attributes": {}},
        ]
        two_sided, routed = apply_route_rules(rows, [rule])
        assert len(two_sided) == 1
        assert len(routed) == 0

    def test_two_sided_participation(self):
        rule = parse_rule(_route_rule(participation="two_sided", posting_target=None))
        rows = [
            {"source_type": "alipay_ledger", "amount": "-5.00",
             "attributes": {"description": "佣金"}},
        ]
        two_sided, routed = apply_route_rules(rows, [rule])
        assert len(two_sided) == 1
        assert len(routed) == 0

    def test_excluded_participation(self):
        rule = parse_rule(_route_rule(participation="excluded", posting_target=None))
        rows = [
            {"source_type": "alipay_ledger", "amount": "-5.00",
             "attributes": {"description": "软件服务费"}},
        ]
        two_sided, routed = apply_route_rules(rows, [rule])
        assert len(two_sided) == 0
        assert len(routed) == 1
        assert routed[0].participation == "excluded"

    def test_multiple_rules_first_match_wins(self):
        r1 = parse_rule(_route_rule(posting_target="fee_a"))
        r2 = parse_rule(_route_rule(posting_target="fee_b"))
        rows = [
            {"source_type": "alipay_ledger", "amount": "-5.00",
             "attributes": {"description": "佣金"}},
        ]
        _, routed = apply_route_rules(rows, [r1, r2])
        assert len(routed) == 1
        assert routed[0].posting_target == "fee_a"

    def test_non_route_rules_ignored(self):
        classify_rule = parse_rule({
            "action": "classify",
            "select": {"field": "source_type", "op": "eq", "value": "alipay_ledger"},
            "category": "fee",
        })
        rows = [
            {"source_type": "alipay_ledger", "amount": "-5.00", "attributes": {}},
        ]
        two_sided, routed = apply_route_rules(rows, [classify_rule])
        assert len(two_sided) == 1
        assert len(routed) == 0

    def test_empty_rows(self):
        rule = parse_rule(_route_rule())
        two_sided, routed = apply_route_rules([], [rule])
        assert two_sided == []
        assert routed == []
