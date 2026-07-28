"""Tests for the redaction scanner."""

from __future__ import annotations

import json
from pathlib import Path

from commerce_harness.packs.redact_scan import (
    RedactViolation,
    redaction_report,
    scan_pack,
)


def _make_pack(tmp_path: Path, data: dict, filename: str = "knowledge.json") -> Path:
    pack_dir = tmp_path / "test_pack"
    pack_dir.mkdir(exist_ok=True)
    manifest = pack_dir / "pack.json"
    manifest.write_text(json.dumps({
        "pack_id": "test", "domain": "test", "version": "0.1.0",
        "publisher": "test", "engine_compat": {"min": "0.1.0"},
    }), encoding="utf-8")
    data_file = pack_dir / filename
    data_file.write_text(json.dumps(data), encoding="utf-8")
    return pack_dir


class TestSensitiveShapes:
    def test_order_id_d19(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"id": "1234567890123456789"}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "order_id_shape_D19" for v in violations)

    def test_phone_number(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"contact": "13800138000"}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "phone_number_shape" for v in violations)

    def test_file_path_unix(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"path": "/home/user/data.csv"}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "file_path_shape" for v in violations)

    def test_file_path_windows(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"path": "C:\\Users\\data"}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "file_path_shape" for v in violations)


class TestAmountPrecision:
    def test_precise_fen_detected(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"amount": "123.45"}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "amount_precise_to_fen" for v in violations)

    def test_bucketed_amount_ok(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"bucket": "1e2"}])
        violations = scan_pack(pack_dir)
        fen_violations = [v for v in violations if v.reason == "amount_precise_to_fen"]
        assert len(fen_violations) == 0


class TestExportAllowed:
    def test_export_false_detected(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"case_id": "c1", "export_allowed": False}])
        violations = scan_pack(pack_dir)
        assert any(v.reason == "export_allowed_false" for v in violations)

    def test_export_true_ok(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"case_id": "c1", "export_allowed": True}])
        violations = scan_pack(pack_dir)
        export_violations = [v for v in violations if v.reason == "export_allowed_false"]
        assert len(export_violations) == 0


class TestArtifactStrings:
    def test_matching_artifact_string(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"description": "客户公司名称ABC"}])
        violations = scan_pack(pack_dir, artifact_strings={"客户公司名称ABC"})
        assert any(v.reason == "string_in_snapshot_artifacts" for v in violations)

    def test_short_string_ignored(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"x": "abc"}])
        violations = scan_pack(pack_dir, artifact_strings={"abc"})
        art_violations = [v for v in violations if v.reason == "string_in_snapshot_artifacts"]
        assert len(art_violations) == 0


class TestCleanPack:
    def test_clean_pack_passes(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [
            {"fingerprint": "abc123def456", "disposition": "rule_candidate",
             "bucket": "1e2", "export_allowed": True},
        ])
        violations = scan_pack(pack_dir)
        assert len(violations) == 0

    def test_manifest_is_scanned_too(self, tmp_path):
        pack_dir = _make_pack(tmp_path, {})
        (pack_dir / "pack.json").write_text(json.dumps({
            "pack_id": "test", "domain": "test", "version": "0.1.0",
            "publisher": "test", "engine_compat": {"min": "0.1.0"},
            "phone": "13800138000",
        }), encoding="utf-8")
        violations = scan_pack(pack_dir)
        phone_v = [v for v in violations if v.reason == "phone_number_shape"]
        assert [v.file for v in phone_v] == ["pack.json"]

    def test_nested_rule_files_are_scanned(self, tmp_path):
        pack_dir = _make_pack(tmp_path, {})
        rules_dir = pack_dir / "rules"
        rules_dir.mkdir()
        (rules_dir / "leaky.json").write_text(
            json.dumps({"note": "订单 1234567890123456789 需人工处理"}),
            encoding="utf-8",
        )
        violations = scan_pack(pack_dir)
        assert [
            (v.file, v.reason)
            for v in violations
            if v.reason == "order_id_shape_D19"
        ] == [("rules/leaky.json", "order_id_shape_D19")]

    def test_numeric_amount_is_not_a_bypass(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"observed": 435.40}])
        violations = scan_pack(pack_dir)
        assert [v.reason for v in violations] == ["amount_precise_to_fen"]

    def test_declared_thresholds_are_not_flagged(self, tmp_path):
        pack_dir = _make_pack(
            tmp_path,
            [{"materiality": {"single_item": "500.00"}}],
        )
        assert scan_pack(pack_dir) == []

    def test_sensitive_dict_key_is_flagged(self, tmp_path):
        pack_dir = _make_pack(tmp_path, [{"13800138000": "counterparty"}])
        violations = scan_pack(pack_dir)
        assert [v.reason for v in violations] == ["phone_number_shape"]

    def test_builtin_pack_passes_scan(self):
        pack_dir = (
            Path(__file__).resolve().parents[2]
            / "packs"
            / "builtin"
            / "ecommerce_settlement"
        )
        assert scan_pack(pack_dir) == []


class TestRedactionReport:
    def test_clean_report(self):
        report = redaction_report([])
        assert report["passed"] is True
        assert report["violation_count"] == 0

    def test_violation_report(self):
        v = RedactViolation("test.json", "$.x", "phone_number_shape", "138001")
        report = redaction_report([v])
        assert report["passed"] is False
        assert report["violation_count"] == 1
        assert report["violations"][0]["reason"] == "phone_number_shape"
