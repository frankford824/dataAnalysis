"""Versioned trust boundary for deterministic source-row evidence.

Bindings created before this version are retained for audit, but they cannot
authorize model citations or certified personnel calculations.  Reprocessing
the immutable snapshot is the only way to obtain a current binding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

NORMALIZATION_RULE_VERSION = "finite-normalization-v5"
LEARNING_POLICY_VERSION = "autonomy-learning-v2"
PERFORMANCE_ENGINE_VERSION = "certified-person-performance-v2"

_DIGEST_FIELDS = (
    "ordinal",
    "snapshot_id",
    "artifact_id",
    "source_member",
    "source_sheet",
    "row_no",
    "field",
    "source_value",
    "normalization_version",
    "rule_version_id",
)


def evidence_binding_digest(
    bindings: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the complete ordered binding set used by a model suggestion."""

    payload = [
        {
            key: (
                int(binding.get(key) or 0)
                if key in {"ordinal", "row_no"}
                else str(binding.get(key) or "")
            )
            for key in _DIGEST_FIELDS
        }
        for binding in bindings
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
