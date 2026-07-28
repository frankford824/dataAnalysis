"""Core verification logic for certification reports.

ZERO imports from commerce_harness — the verifier must be independently
runnable against a JSON report file without any project dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class VerificationResult:
    """Holds the outcome of verifying a certification report."""

    __slots__ = ("passed", "checks", "errors")

    def __init__(self) -> None:
        self.passed: bool = True
        self.checks: list[str] = []
        self.errors: list[str] = []

    def ok(self, description: str) -> None:
        self.checks.append(f"PASS: {description}")

    def fail(self, description: str) -> None:
        self.passed = False
        self.errors.append(description)
        self.checks.append(f"FAIL: {description}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "errors": self.errors,
        }


_REQUIRED_FIELDS = {
    "reportKind",
    "periodId",
    "runId",
    "storeId",
    "status",
    "disclaimer",
    "trustDistribution",
    "partial",
    "blocked",
    "baseline",
    "hidesIncomplete",
    "reportSha256",
    "reportId",
    "generatedAt",
}

_EXPECTED_DISCLAIMER_FRAGMENT = "非法定审计"
_LEGACY_DISCLAIMER_FRAGMENT = "不是法定审计报告"
_KNOWN_TRUST_TIERS = {"certified", "partial", "blocked"}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _check_hash(report: dict[str, Any], result: VerificationResult) -> None:
    """Verify that reportSha256 matches a recomputed hash of the body."""
    stored_hash = report.get("reportSha256", "")

    body = dict(report)
    body.pop("reportSha256", None)
    body.pop("reportId", None)
    body.pop("path", None)

    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    if recomputed == stored_hash:
        result.ok("report hash matches recomputed hash")
    else:
        result.fail(
            f"report hash mismatch: stored={stored_hash}, recomputed={recomputed}"
        )


def _check_evidence_refs(report: dict[str, Any], result: VerificationResult) -> None:
    """Every baseline reference must be present and well formed.

    A report whose baseline hashes are absent cannot be traced back to inputs,
    rules and code, so it is not a certification at all.
    """
    baseline = report.get("baseline")
    if not isinstance(baseline, dict):
        result.fail("baseline section missing from report")
        return

    expected_keys = ("inputManifestSha256", "ruleSetSha256", "codeSha", "outputSha256")
    missing = [
        key
        for key in expected_keys
        if not isinstance(baseline.get(key), str) or not baseline[key].strip()
    ]
    if missing:
        result.fail(f"baseline evidence refs missing: {sorted(missing)}")
        return

    # codeSha carries a git revision (optionally with a dirty marker), the rest
    # are plain sha256 digests.
    malformed = [
        key
        for key in ("inputManifestSha256", "ruleSetSha256", "outputSha256")
        if not _SHA256_HEX.match(baseline[key])
    ]
    if malformed:
        result.fail(f"baseline evidence refs are not sha256 digests: {malformed}")
        return
    result.ok("all baseline evidence refs present and well formed")


def _check_trust_tier_consistency(
    report: dict[str, Any], result: VerificationResult
) -> None:
    """Verify trust_tier distribution consistency."""
    trust = report.get("trustDistribution", {})
    if trust is None or not isinstance(trust, dict):
        result.fail("trustDistribution is missing or not an object")
        return
    if not trust:
        result.fail("trustDistribution is empty: report certifies no numbers")
    else:
        unknown = sorted(set(trust) - _KNOWN_TRUST_TIERS)
        if unknown:
            result.fail(f"trustDistribution has unknown tiers: {unknown}")
        for tier_name, tier_data in trust.items():
            if not isinstance(tier_data, dict):
                result.fail(f"trustDistribution[{tier_name}] is not a dict")
                continue
            if "count" not in tier_data or "amount" not in tier_data:
                result.fail(f"trustDistribution[{tier_name}] missing count or amount")
                continue
            try:
                count = int(tier_data["count"])
                Decimal(str(tier_data["amount"]))
            except (ValueError, InvalidOperation):
                result.fail(f"trustDistribution[{tier_name}] has non-numeric values")
                continue
            if count < 0:
                result.fail(f"trustDistribution[{tier_name}] has negative count")
            else:
                result.ok(f"trustDistribution[{tier_name}] is valid")

    partial = report.get("partial") or trust.get("partial", {})
    blocked = report.get("blocked") or trust.get("blocked", {})
    hides = report.get("hidesIncomplete", False)

    partial_count = int(partial.get("count", 0)) if isinstance(partial, dict) else 0
    blocked_count = int(blocked.get("count", 0)) if isinstance(blocked, dict) else 0

    if (partial_count > 0 or blocked_count > 0) and hides:
        result.fail("report claims to hide incomplete data but partial/blocked entries exist")
    else:
        result.ok("hidesIncomplete consistency check passed")


def _check_internal_consistency(
    report: dict[str, Any], result: VerificationResult
) -> None:
    """Re-derive every summary field from trustDistribution.

    The verifier only sees the report, so it cannot recompute money from source
    data. What it can do is prove that no summary field was edited away from the
    tier breakdown it claims to summarise.
    """
    trust = report.get("trustDistribution", {})
    if not isinstance(trust, dict):
        return

    for field in ("partial", "blocked"):
        reported = report.get(field, {})
        derived = trust.get(field, {"count": 0, "amount": "0"})
        if not isinstance(reported, dict) or not isinstance(derived, dict):
            result.fail(f"{field} section is not an object")
            continue
        if int(reported.get("count", 0)) != int(derived.get("count", 0)):
            result.fail(f"top-level {field}.count does not match trustDistribution")
            continue
        try:
            reported_amount = Decimal(str(reported.get("amount", "0")))
            derived_amount = Decimal(str(derived.get("amount", "0")))
        except InvalidOperation:
            result.fail(f"{field}.amount is not a number")
            continue
        if reported_amount != derived_amount:
            result.fail(f"top-level {field}.amount does not match trustDistribution")
        else:
            result.ok(f"{field} matches trustDistribution")

    if report.get("status") != "closed":
        result.fail(
            f"certification is only valid for a closed period, got {report.get('status')!r}"
        )
    else:
        result.ok("period status is closed")

    incomplete = 0
    for field in ("partial", "blocked"):
        section = report.get(field)
        if isinstance(section, dict):
            incomplete += int(section.get("count", 0) or 0)
    if incomplete > 0 and not str(report.get("completenessNote", "")).strip():
        result.fail("partial/blocked entries exist but completenessNote is missing")
    elif incomplete > 0:
        result.ok("completenessNote discloses the incomplete part")

    unresolved = report.get("openUnresolved")
    if isinstance(unresolved, dict):
        try:
            amount = Decimal(str(unresolved.get("amount", "0")))
        except InvalidOperation:
            result.fail("openUnresolved.amount is not a number")
        else:
            count = int(unresolved.get("count", 0) or 0)
            if count == 0 and amount != 0:
                result.fail("openUnresolved reports an amount without any item")
            else:
                result.ok("openUnresolved is internally consistent")


def _check_disclaimer(report: dict[str, Any], result: VerificationResult) -> None:
    """Verify the disclaimer contains the required fragment."""
    disclaimer = report.get("disclaimer", "")
    if (
        _EXPECTED_DISCLAIMER_FRAGMENT in disclaimer
        or _LEGACY_DISCLAIMER_FRAGMENT in disclaimer
    ):
        result.ok("disclaimer contains required audit-exclusion statement")
    else:
        result.fail(
            "disclaimer missing required statement: "
            f"expected '{_EXPECTED_DISCLAIMER_FRAGMENT}' or equivalent"
        )


def verify(report_path: str | Path) -> VerificationResult:
    """Verify a certification report JSON file.

    Returns a VerificationResult with check details and pass/fail status.
    """
    result = VerificationResult()
    path = Path(report_path)

    if not path.is_file():
        result.fail(f"report file not found: {path}")
        return result

    try:
        text = path.read_text(encoding="utf-8")
        report = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.fail(f"report is not valid JSON: {exc}")
        return result

    missing = _REQUIRED_FIELDS - set(report)
    if missing:
        result.fail(f"required fields missing: {sorted(missing)}")
    else:
        result.ok("all required fields present")

    _check_hash(report, result)
    _check_evidence_refs(report, result)
    _check_trust_tier_consistency(report, result)
    _check_internal_consistency(report, result)
    _check_disclaimer(report, result)

    return result
