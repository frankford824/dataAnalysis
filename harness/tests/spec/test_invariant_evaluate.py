"""Tests for invariant definitions and evaluation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness.spec.evaluate import evaluate
from commerce_harness.spec.invariant import (
    FAMILIES,
    load_invariants_from_json_path,
    parse_invariant,
)


def _make_equality_def(**overrides) -> dict:
    base = {
        "family": "equality",
        "scope": {"period": "current", "store": "each", "currency": "CNY"},
        "sides": {
            "left": {
                "kinds": ["orders"],
                "select": {"field": "source_type", "op": "eq", "value": "orders"},
                "sign": "as_declared",
            },
            "right": {
                "kinds": ["alipay_ledger"],
                "select": {"field": "source_type", "op": "eq", "value": "alipay_ledger"},
                "sign": "as_declared",
            },
        },
        "tolerance": {"absolute": "0.0100", "relative": "0.000000"},
        "materiality": {
            "single_item": "500.00",
            "category_cumulative": "5000.00",
            "period_revenue_ratio": "0.001",
        },
        "on_violation": {
            "legal_dispositions": ["timing_difference"],
            "blocks_certification": True,
        },
        "blocks_certification": True,
    }
    base.update(overrides)
    return base


class TestInvariantParsing:
    def test_parse_equality(self):
        inv = parse_invariant(_make_equality_def())
        assert inv.family == "equality"
        assert inv.blocks_certification is True
        assert inv.invariant_id

    def test_invariant_id_deterministic(self):
        inv1 = parse_invariant(_make_equality_def())
        inv2 = parse_invariant(_make_equality_def())
        assert inv1.invariant_id == inv2.invariant_id

    def test_invariant_id_changes_with_content(self):
        inv1 = parse_invariant(_make_equality_def())
        inv2 = parse_invariant(_make_equality_def(
            tolerance={"absolute": "1.0000", "relative": "0.000000"}
        ))
        assert inv1.invariant_id != inv2.invariant_id

    def test_invalid_family_rejected(self):
        with pytest.raises(ValueError, match="family"):
            parse_invariant(_make_equality_def(family="invalid"))

    def test_all_families_accepted(self):
        for family in FAMILIES:
            inv = parse_invariant(_make_equality_def(family=family))
            assert inv.family == family

    def test_invalid_sign_rejected(self):
        d = _make_equality_def()
        d["sides"]["left"]["sign"] = "bad_sign"
        with pytest.raises(ValueError, match="sign"):
            parse_invariant(d)


class TestLoadFromJson:
    def test_load_builtin_invariants(self):
        pack_dir = (
            Path(__file__).resolve().parents[2]
            / "packs" / "builtin" / "ecommerce_settlement"
        )
        json_path = pack_dir / "invariants.json"
        if not json_path.exists():
            pytest.skip("builtin invariants.json not found")
        invariants = load_invariants_from_json_path(json_path)
        assert len(invariants) == 2
        assert all(inv.family == "equality" for inv in invariants)
        assert all(inv.blocks_certification for inv in invariants)


class TestEvaluateEquality:
    def test_balanced(self):
        inv = parse_invariant(_make_equality_def())
        rows = [
            {"source_type": "orders", "amount": "100.00"},
            {"source_type": "orders", "amount": "200.00"},
            {"source_type": "alipay_ledger", "amount": "300.00"},
        ]
        results = evaluate(rows, [inv])
        assert len(results) == 1
        assert results[0].status == "passed"
        assert results[0].gap_amount == Decimal("0.0000")

    def test_within_tolerance(self):
        inv = parse_invariant(_make_equality_def())
        rows = [
            {"source_type": "orders", "amount": "100.00"},
            {"source_type": "alipay_ledger", "amount": "100.005"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "passed"

    def test_violated(self):
        inv = parse_invariant(_make_equality_def())
        rows = [
            {"source_type": "orders", "amount": "1000.00"},
            {"source_type": "alipay_ledger", "amount": "990.00"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "violated"
        assert results[0].gap_amount == Decimal("10.0000")

    def test_no_rows(self):
        inv = parse_invariant(_make_equality_def())
        results = evaluate([], [inv])
        assert results[0].status == "insufficient_input"

    def test_materiality_flagged(self):
        inv = parse_invariant(_make_equality_def())
        rows = [
            {"source_type": "orders", "amount": "10000.00"},
            {"source_type": "alipay_ledger", "amount": "9000.00"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "violated"
        assert results[0].is_material is True

    def test_invert_expense_sign(self):
        d = _make_equality_def()
        d["sides"]["right"]["sign"] = "invert_expense"
        inv = parse_invariant(d)
        rows = [
            {"source_type": "orders", "amount": "100.00"},
            {"source_type": "alipay_ledger", "amount": "-100.00"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "passed"


class TestEvaluateUniqueness:
    def test_unique_keys(self):
        d = _make_equality_def(family="uniqueness")
        d["scope"]["key_field"] = "business_key"
        inv = parse_invariant(d)
        rows = [
            {"source_type": "orders", "business_key": "A001", "amount": "10"},
            {"source_type": "orders", "business_key": "A002", "amount": "20"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "passed"

    def test_duplicate_keys(self):
        d = _make_equality_def(family="uniqueness")
        d["scope"]["key_field"] = "business_key"
        inv = parse_invariant(d)
        rows = [
            {"source_type": "orders", "business_key": "A001", "amount": "10"},
            {"source_type": "orders", "business_key": "A001", "amount": "20"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "violated"


class TestEvaluateCompleteness:
    def test_complete(self):
        d = _make_equality_def(family="completeness")
        d["scope"]["key_field"] = "business_key"
        inv = parse_invariant(d)
        rows = [
            {"source_type": "orders", "business_key": "K1", "amount": "10"},
            {"source_type": "alipay_ledger", "business_key": "K1", "amount": "10"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "passed"

    def test_missing_key(self):
        d = _make_equality_def(family="completeness")
        d["scope"]["key_field"] = "business_key"
        inv = parse_invariant(d)
        rows = [
            {"source_type": "orders", "business_key": "K1", "amount": "10"},
            {"source_type": "orders", "business_key": "K2", "amount": "20"},
            {"source_type": "alipay_ledger", "business_key": "K1", "amount": "10"},
        ]
        results = evaluate(rows, [inv])
        assert results[0].status == "violated"


class TestMultipleInvariants:
    def test_evaluate_multiple(self):
        inv1 = parse_invariant(_make_equality_def())
        inv2 = parse_invariant(_make_equality_def(family="uniqueness"))
        rows = [
            {"source_type": "orders", "business_key": "K1", "amount": "100.00"},
            {"source_type": "alipay_ledger", "business_key": "K1", "amount": "100.00"},
        ]
        results = evaluate(rows, [inv1, inv2])
        assert len(results) == 2
