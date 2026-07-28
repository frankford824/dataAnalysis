"""Tests for the independent verifier package."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "verifier" / ".."))

from verifier.core import verify


def _make_report(tmp_path: Path, **overrides: object) -> Path:
    """Create a minimal valid report file for testing."""
    body: dict[str, object] = {
        "reportKind": "certification",
        "periodId": "p1",
        "runId": "run-1",
        "storeId": "s1",
        "periodStart": "2026-03-01",
        "periodEnd": "2026-03-31",
        "status": "closed",
        "operatorName": "张三",
        "generatedAt": "2026-04-01T00:00:00+00:00",
        "disclaimer": "本报告不是法定审计报告，也不是鉴证业务报告。",
        "trustDistribution": {
            "certified": {"count": 10, "amount": "5000.0000"},
            "partial": {"count": 0, "amount": "0"},
            "blocked": {"count": 0, "amount": "0"},
        },
        "partial": {"count": 0, "amount": "0"},
        "blocked": {"count": 0, "amount": "0"},
        "openUnresolved": {"count": 0, "amount": "0"},
        "baseline": {
            "inputManifestSha256": "a" * 64,
            "ruleSetSha256": "b" * 64,
            "codeSha": "c" * 40,
            "outputSha256": "d" * 64,
        },
        "hidesIncomplete": False,
    }
    body.update(overrides)

    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    report_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    body["reportId"] = "cert_test"
    body["reportSha256"] = report_hash

    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


class TestVerifier:
    def test_valid_report_passes(self, tmp_path: Path) -> None:
        path = _make_report(tmp_path)
        result = verify(path)
        assert result.passed, result.errors

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = verify(tmp_path / "nonexistent.json")
        assert not result.passed
        assert any("not found" in e for e in result.errors)

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json at all")
        result = verify(path)
        assert not result.passed

    def test_tampered_hash_fails(self, tmp_path: Path) -> None:
        path = _make_report(tmp_path)
        data = json.loads(path.read_text())
        data["reportSha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        result = verify(path)
        assert not result.passed
        assert any("hash mismatch" in e for e in result.errors)

    def test_missing_evidence_refs_fails(self, tmp_path: Path) -> None:
        path = _make_report(tmp_path, baseline={
            "inputManifestSha256": "a" * 64,
            "ruleSetSha256": None,
            "codeSha": None,
            "outputSha256": None,
        })
        result = verify(path)
        assert not result.passed
        assert any("evidence refs" in e for e in result.errors)

    def test_baseline_without_any_hash_fails(self, tmp_path: Path) -> None:
        path = _make_report(tmp_path, baseline={
            "inputManifestSha256": None,
            "ruleSetSha256": None,
            "codeSha": None,
            "outputSha256": None,
        })
        result = verify(path)
        assert not result.passed
        assert any("evidence refs missing" in e for e in result.errors)

    def test_missing_required_fields_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "incomplete.json"
        path.write_text(json.dumps({"reportKind": "certification"}))
        result = verify(path)
        assert not result.passed
        assert any("required fields missing" in e for e in result.errors)

    def test_trust_tier_consistency(self, tmp_path: Path) -> None:
        path = _make_report(
            tmp_path,
            trustDistribution={
                "partial": {"count": 5, "amount": "100"},
            },
            partial={"count": 5, "amount": "100"},
            blocked={"count": 0, "amount": "0"},
            hidesIncomplete=True,
        )
        result = verify(path)
        assert not result.passed
        assert any("hide" in e.lower() for e in result.errors)

    def test_disclaimer_check(self, tmp_path: Path) -> None:
        path = _make_report(tmp_path, disclaimer="this is a regular report")
        result = verify(path)
        assert not result.passed
        assert any("disclaimer" in e.lower() for e in result.errors)
