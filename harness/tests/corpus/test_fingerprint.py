"""Tests for situation fingerprinting."""

from __future__ import annotations

from commerce_harness.corpus.fingerprint import (
    SERVICE_FEE_TERMS,
    situation_fingerprint,
)


class TestSituationFingerprint:
    def test_deterministic(self):
        fp1 = situation_fingerprint(
            source_kinds=["orders", "alipay_ledger"],
            amounts=["100.00", "-5.00"],
            business_description="技术服务费",
            invariant_family="equality",
        )
        fp2 = situation_fingerprint(
            source_kinds=["alipay_ledger", "orders"],
            amounts=["100.00", "-5.00"],
            business_description="技术服务费",
            invariant_family="equality",
        )
        assert fp1 == fp2

    def test_amount_order_does_not_change_fingerprint(self):
        forward = situation_fingerprint(
            source_kinds=["orders", "alipay_ledger"],
            amounts=["100.00", "-5.00"],
        )
        reversed_order = situation_fingerprint(
            source_kinds=["orders", "alipay_ledger"],
            amounts=["-5.00", "100.00"],
        )
        assert forward == reversed_order

    def test_bucket_edges_are_exact(self):
        # 100 belongs to 1e2, not to 1e1: binary rounding must not shift it.
        assert situation_fingerprint(
            source_kinds=["orders"], amounts=["100"],
        ) == situation_fingerprint(source_kinds=["orders"], amounts=["999.99"])
        assert situation_fingerprint(
            source_kinds=["orders"], amounts=["100"],
        ) != situation_fingerprint(source_kinds=["orders"], amounts=["99.99"])

    def test_different_kinds_different_fingerprint(self):
        fp1 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["100.00"],
        )
        fp2 = situation_fingerprint(
            source_kinds=["wechat_ledger"],
            amounts=["100.00"],
        )
        assert fp1 != fp2

    def test_different_sign_different_fingerprint(self):
        fp1 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["100.00"],
        )
        fp2 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["-100.00"],
        )
        assert fp1 != fp2

    def test_no_raw_amount_in_fingerprint(self):
        fp1 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["123.45"],
        )
        fp2 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["678.90"],
        )
        assert fp1 == fp2

    def test_different_magnitude_different_fingerprint(self):
        fp1 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["1.00"],
        )
        fp2 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["100000.00"],
        )
        assert fp1 != fp2

    def test_service_fee_classification(self):
        fp1 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["-5.00"],
            business_description="软件服务费",
        )
        fp2 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["-5.00"],
            business_description="技术服务费",
        )
        assert fp1 == fp2

    def test_description_class_affects_fingerprint(self):
        fp1 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["-5.00"],
            business_description="退款",
        )
        fp2 = situation_fingerprint(
            source_kinds=["alipay"],
            amounts=["-5.00"],
            business_description="软件服务费",
        )
        assert fp1 != fp2

    def test_field_shapes_from_row(self):
        fp = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["100.00"],
            row={"business_key": "1234567890123456789", "source_type": "orders"},
        )
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_timing_pattern(self):
        fp1 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["100.00"],
            timing_pattern="same_period",
        )
        fp2 = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["100.00"],
            timing_pattern="cross_period",
        )
        assert fp1 != fp2

    def test_service_fee_terms_coverage(self):
        assert "软件服务费" in SERVICE_FEE_TERMS
        assert "佣金" in SERVICE_FEE_TERMS
        assert "退款" in SERVICE_FEE_TERMS

    def test_zero_amount(self):
        fp = situation_fingerprint(
            source_kinds=["orders"],
            amounts=["0"],
        )
        assert isinstance(fp, str)
