"""Tests for the closed predicate DSL."""

from __future__ import annotations

import pytest

from commerce_harness.spec.predicate import (
    evaluate_predicate,
    parse_predicate,
    predicate_to_chinese,
)


class TestParseBasicLeaves:
    def test_eq(self):
        pred = parse_predicate({"field": "source_type", "op": "eq", "value": "alipay_ledger"})
        assert pred.kind == "leaf"
        assert pred.payload["op"] == "eq"

    def test_ne(self):
        pred = parse_predicate({"field": "side", "op": "ne", "value": "order"})
        assert pred.kind == "leaf"

    def test_in(self):
        pred = parse_predicate({"field": "source_type", "op": "in", "value": ["a", "b"]})
        assert pred.kind == "leaf"

    def test_not_in(self):
        pred = parse_predicate({"field": "source_type", "op": "not_in", "value": ["x"]})
        assert pred.kind == "leaf"

    def test_prefix(self):
        pred = parse_predicate({"field": "business_key", "op": "prefix", "value": "ORD"})
        assert pred.kind == "leaf"

    def test_suffix(self):
        pred = parse_predicate({"field": "business_key", "op": "suffix", "value": "_CN"})
        assert pred.kind == "leaf"

    def test_contains(self):
        pred = parse_predicate({"field": "source_name", "op": "contains", "value": "支付宝"})
        assert pred.kind == "leaf"

    def test_range(self):
        pred = parse_predicate(
            {"field": "amount", "op": "range", "value": {"min": "0", "max": "100"}}
        )
        assert pred.kind == "leaf"

    def test_sign(self):
        pred = parse_predicate({"field": "amount", "op": "sign", "value": "negative"})
        assert pred.kind == "leaf"

    def test_is_null(self):
        pred = parse_predicate({"field": "cash_bridge_key", "op": "is_null"})
        assert pred.kind == "leaf"

    def test_not_null(self):
        pred = parse_predicate({"field": "business_key", "op": "not_null"})
        assert pred.kind == "leaf"

    def test_matches_shape(self):
        pred = parse_predicate({"field": "business_key", "op": "matches_shape", "value": "D{19}"})
        assert pred.kind == "leaf"


class TestParseCombinators:
    def test_all_of(self):
        raw = {"all_of": [
            {"field": "source_type", "op": "eq", "value": "alipay"},
            {"field": "amount", "op": "sign", "value": "negative"},
        ]}
        pred = parse_predicate(raw)
        assert pred.kind == "all_of"
        assert len(pred.payload) == 2

    def test_any_of(self):
        raw = {"any_of": [
            {"field": "side", "op": "eq", "value": "order"},
            {"field": "side", "op": "eq", "value": "platform"},
        ]}
        pred = parse_predicate(raw)
        assert pred.kind == "any_of"

    def test_none_of(self):
        raw = {"none_of": [
            {"field": "source_type", "op": "eq", "value": "excluded"},
        ]}
        pred = parse_predicate(raw)
        assert pred.kind == "none_of"

    def test_nested_depth_3_ok(self):
        raw = {"all_of": [
            {"any_of": [
                {"none_of": [
                    {"field": "source_type", "op": "eq", "value": "x"},
                ]},
            ]},
        ]}
        pred = parse_predicate(raw)
        assert pred.kind == "all_of"

    def test_nested_depth_4_rejected(self):
        raw = {"all_of": [
            {"any_of": [
                {"none_of": [
                    {"all_of": [
                        {"field": "source_type", "op": "eq", "value": "x"},
                    ]},
                ]},
            ]},
        ]}
        with pytest.raises(ValueError, match="nesting depth"):
            parse_predicate(raw)

    def test_max_leaves_exceeded(self):
        leaves = [{"field": "source_type", "op": "eq", "value": f"v{i}"} for i in range(33)]
        with pytest.raises(ValueError, match="leaves"):
            parse_predicate({"all_of": leaves})


class TestParseValidation:
    def test_invalid_field(self):
        with pytest.raises(ValueError, match="field"):
            parse_predicate({"field": "not_a_real_field", "op": "eq", "value": "x"})

    def test_attributes_field_ok(self):
        pred = parse_predicate({"field": "attributes.description", "op": "eq", "value": "test"})
        assert pred.kind == "leaf"

    def test_invalid_op(self):
        with pytest.raises(ValueError, match="op"):
            parse_predicate({"field": "source_type", "op": "regex", "value": ".*"})

    def test_sign_invalid_value(self):
        with pytest.raises(ValueError, match="sign"):
            parse_predicate({"field": "amount", "op": "sign", "value": "big"})

    def test_in_requires_list(self):
        with pytest.raises(ValueError, match="list"):
            parse_predicate({"field": "source_type", "op": "in", "value": "single"})

    def test_range_requires_dict(self):
        with pytest.raises(ValueError, match="range"):
            parse_predicate({"field": "amount", "op": "range", "value": "100"})

    def test_non_dict_rejected(self):
        with pytest.raises(ValueError):
            parse_predicate("not a dict")  # type: ignore[arg-type]


class TestEvaluate:
    def test_eq_match(self):
        pred = parse_predicate({"field": "source_type", "op": "eq", "value": "alipay"})
        assert evaluate_predicate(pred, {"source_type": "alipay"}) is True
        assert evaluate_predicate(pred, {"source_type": "wechat"}) is False

    def test_in_match(self):
        pred = parse_predicate({"field": "side", "op": "in", "value": ["order", "platform"]})
        assert evaluate_predicate(pred, {"side": "order"}) is True
        assert evaluate_predicate(pred, {"side": "fund"}) is False

    def test_sign_negative(self):
        pred = parse_predicate({"field": "amount", "op": "sign", "value": "negative"})
        assert evaluate_predicate(pred, {"amount": "-10.00"}) is True
        assert evaluate_predicate(pred, {"amount": "10.00"}) is False
        assert evaluate_predicate(pred, {"amount": "0"}) is False

    def test_range(self):
        pred = parse_predicate(
            {"field": "amount", "op": "range", "value": {"min": "0", "max": "100"}}
        )
        assert evaluate_predicate(pred, {"amount": "50"}) is True
        assert evaluate_predicate(pred, {"amount": "150"}) is False

    def test_is_null(self):
        pred = parse_predicate({"field": "cash_bridge_key", "op": "is_null"})
        assert evaluate_predicate(pred, {"cash_bridge_key": None}) is True
        assert evaluate_predicate(pred, {}) is True
        assert evaluate_predicate(pred, {"cash_bridge_key": "abc"}) is False

    def test_not_null(self):
        pred = parse_predicate({"field": "business_key", "op": "not_null"})
        assert evaluate_predicate(pred, {"business_key": "xyz"}) is True
        assert evaluate_predicate(pred, {"business_key": None}) is False

    def test_prefix(self):
        pred = parse_predicate({"field": "business_key", "op": "prefix", "value": "ORD"})
        assert evaluate_predicate(pred, {"business_key": "ORD12345"}) is True
        assert evaluate_predicate(pred, {"business_key": "INV12345"}) is False

    def test_matches_shape_d19(self):
        pred = parse_predicate({"field": "business_key", "op": "matches_shape", "value": "D{19}"})
        assert evaluate_predicate(pred, {"business_key": "1234567890123456789"}) is True
        assert evaluate_predicate(pred, {"business_key": "123456"}) is False

    def test_matches_shape_mixed(self):
        pred = parse_predicate({"field": "period_key", "op": "matches_shape", "value": "D{4}-D{2}"})
        assert evaluate_predicate(pred, {"period_key": "2602-08"}) is True
        assert evaluate_predicate(pred, {"period_key": "260208"}) is False

    def test_all_of(self):
        pred = parse_predicate({"all_of": [
            {"field": "source_type", "op": "eq", "value": "alipay"},
            {"field": "amount", "op": "sign", "value": "negative"},
        ]})
        assert evaluate_predicate(pred, {"source_type": "alipay", "amount": "-5.00"}) is True
        assert evaluate_predicate(pred, {"source_type": "alipay", "amount": "5.00"}) is False

    def test_none_of(self):
        pred = parse_predicate({"none_of": [
            {"field": "source_type", "op": "eq", "value": "excluded"},
        ]})
        assert evaluate_predicate(pred, {"source_type": "alipay"}) is True
        assert evaluate_predicate(pred, {"source_type": "excluded"}) is False

    def test_attributes_field(self):
        pred = parse_predicate({"field": "attributes.desc", "op": "contains", "value": "服务费"})
        assert evaluate_predicate(pred, {"attributes": {"desc": "技术服务费"}}) is True
        assert evaluate_predicate(pred, {"attributes": {"desc": "退款"}}) is False


class TestToChinese:
    def test_eq_leaf(self):
        pred = parse_predicate({"field": "source_type", "op": "eq", "value": "alipay"})
        cn = predicate_to_chinese(pred)
        assert "source_type" in cn
        assert "等于" in cn

    def test_sign_leaf(self):
        pred = parse_predicate({"field": "amount", "op": "sign", "value": "negative"})
        cn = predicate_to_chinese(pred)
        assert "负" in cn

    def test_all_of_combinator(self):
        pred = parse_predicate({"all_of": [
            {"field": "source_type", "op": "eq", "value": "x"},
            {"field": "side", "op": "eq", "value": "y"},
        ]})
        cn = predicate_to_chinese(pred)
        assert "且" in cn

    def test_any_of_combinator(self):
        pred = parse_predicate({"any_of": [
            {"field": "source_type", "op": "eq", "value": "a"},
            {"field": "source_type", "op": "eq", "value": "b"},
        ]})
        cn = predicate_to_chinese(pred)
        assert "或" in cn

    def test_is_null_leaf(self):
        pred = parse_predicate({"field": "cash_bridge_key", "op": "is_null"})
        cn = predicate_to_chinese(pred)
        assert "为空" in cn


class TestShapeBacktracking:
    def test_variable_run_before_literal_still_matches(self):
        pred = parse_predicate({
            "field": "business_key",
            "op": "matches_shape",
            "value": "D{1,4}-D{2}",
        })
        assert evaluate_predicate(pred, {"business_key": "1234-56"}) is True
        assert evaluate_predicate(pred, {"business_key": "1-56"}) is True

    def test_overlong_run_is_still_rejected(self):
        pred = parse_predicate({
            "field": "business_key",
            "op": "matches_shape",
            "value": "D{1,4}-D{2}",
        })
        assert evaluate_predicate(pred, {"business_key": "12345-56"}) is False
