"""Redaction scanner: reject packs containing sensitive data.

Scans pack content for:
- Strings found in local snapshot artifacts
- Sensitive shapes (order IDs D{19}, phone numbers, etc.)
- Amounts to the fen without bucketing
- References with export_allowed=false
"""

from __future__ import annotations

import json
import re
from collections.abc import Set
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ORDER_ID = re.compile(r"\d{19}")
_PHONE = re.compile(r"1[3-9]\d{9}")
_ID_CARD = re.compile(r"\d{15,18}[0-9Xx]?")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:\\")
_POSIX_PATH = re.compile(r"^/(?:home|Users|var|tmp)/")

SCANNER_VERSION = "redact_scan-0.2.0"

# Declared thresholds are part of the rule specification, not observed data, so
# their fen-level precision carries no information about any customer.
THRESHOLD_KEYS = frozenset({
    "absolute",
    "relative",
    "single_item",
    "category_cumulative",
    "period_revenue_ratio",
    "n",
    "version",
    "min",
    "max",
})


@dataclass(frozen=True, slots=True)
class RedactViolation:
    file: str
    path: str
    reason: str
    value_preview: str


def _is_sensitive_shape(value: str) -> str | None:
    """Return reason string if value contains a sensitive shape, else None.

    Substring search rather than full match: an order ID embedded in a sentence
    leaks exactly as much as a bare one.
    """
    if _ORDER_ID.search(value):
        return "order_id_shape_D19"
    if _PHONE.fullmatch(value):
        return "phone_number_shape"
    if _ID_CARD.fullmatch(value):
        return "id_card_shape"
    if _WINDOWS_PATH.match(value) or _POSIX_PATH.match(value):
        return "file_path_shape"
    return None


def _is_precise_fen(value: Any) -> bool:
    """Whether a value is an unbucketed amount carrying fen-level precision."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        # A float in a pack is already a violation of the Decimal rule; treat
        # any fractional float as an unbucketed amount.
        return value != int(value)
    if isinstance(value, int):
        return False
    if not isinstance(value, str | Decimal):
        return False
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not decimal_value.is_finite():
        return False
    exponent = decimal_value.as_tuple().exponent
    if not isinstance(exponent, int):
        return False
    return exponent <= -2 and abs(decimal_value) > 0


def _scan_scalar(
    value: Any,
    path: str,
    file_name: str,
    artifact_strings: Set[str],
    violations: list[RedactViolation],
    *,
    current_key: str | None = None,
) -> None:
    if isinstance(value, str):
        if value in artifact_strings and len(value) >= 4:
            violations.append(RedactViolation(
                file=file_name, path=path,
                reason="string_in_snapshot_artifacts",
                value_preview=value[:40],
            ))
        shape_reason = _is_sensitive_shape(value)
        if shape_reason:
            violations.append(RedactViolation(
                file=file_name, path=path,
                reason=shape_reason,
                value_preview=value[:40],
            ))
    if current_key not in THRESHOLD_KEYS and _is_precise_fen(value):
        violations.append(RedactViolation(
            file=file_name, path=path,
            reason="amount_precise_to_fen",
            value_preview=str(value)[:40],
        ))


def _scan_value(
    value: Any,
    path: str,
    file_name: str,
    artifact_strings: Set[str],
    violations: list[RedactViolation],
    *,
    current_key: str | None = None,
) -> None:
    if isinstance(value, dict):
        if value.get("export_allowed") is False:
            violations.append(RedactViolation(
                file=file_name, path=path,
                reason="export_allowed_false",
                value_preview=str(value.get("case_id", ""))[:40],
            ))
        for key, val in value.items():
            key_path = f"{path}.{key}"
            # Keys carry data too: {"13800138000": ...} leaks a phone number.
            _scan_scalar(
                key, f"{key_path}<key>", file_name, artifact_strings, violations,
            )
            _scan_value(
                val, key_path, file_name, artifact_strings, violations,
                current_key=str(key),
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_value(
                item, f"{path}[{index}]", file_name, artifact_strings, violations,
                current_key=current_key,
            )
        return
    _scan_scalar(
        value, path, file_name, artifact_strings, violations,
        current_key=current_key,
    )


def scan_pack(
    pack_dir: str | Path,
    *,
    artifact_strings: Set[str] | None = None,
) -> list[RedactViolation]:
    """Scan every JSON file in the pack tree for redaction violations.

    ``artifact_strings`` is a set of strings found in local snapshot
    artifacts.  Any string in the pack that appears in this set is flagged.
    Unparsable JSON is a violation rather than a skip, because content that the
    scanner cannot read is content it cannot clear.
    """
    pack_path = Path(pack_dir)
    artifacts = artifact_strings or frozenset()
    violations: list[RedactViolation] = []

    json_files = sorted(
        (path for path in pack_path.rglob("*.json") if path.is_file()),
        key=lambda path: path.relative_to(pack_path).as_posix(),
    )
    for json_file in json_files:
        relative_name = json_file.relative_to(pack_path).as_posix()
        try:
            with open(json_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            violations.append(RedactViolation(
                file=relative_name, path="$",
                reason="unreadable_json",
                value_preview=str(exc)[:40],
            ))
            continue
        _scan_value(data, "$", relative_name, artifacts, violations)

    return violations


def redaction_report(violations: list[RedactViolation]) -> dict[str, Any]:
    """Build a redaction.json report from scan results."""
    return {
        "scanner_version": SCANNER_VERSION,
        "violation_count": len(violations),
        "passed": len(violations) == 0,
        "violations": [
            {
                "file": v.file,
                "path": v.path,
                "reason": v.reason,
                "value_preview": v.value_preview,
            }
            for v in violations
        ],
    }
