"""Pack structure validation.

A valid pack directory must contain ``pack.json`` with required fields,
and all referenced files must exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_MANIFEST_FIELDS = frozenset({
    "pack_id",
    "domain",
    "version",
    "publisher",
    "engine_compat",
})

OPTIONAL_FILES = frozenset({
    "invariants.json",
    "rules.json",
    "knowledge.json",
    "attacks.json",
    "redaction.json",
    "provenance.json",
    "pack.sig",
})


class PackFormatError(ValueError):
    """Raised when a pack fails structural validation."""


def validate_pack(pack_dir: str | Path) -> dict[str, Any]:
    """Validate a pack directory structure.

    Returns the parsed ``pack.json`` manifest on success.
    Raises ``PackFormatError`` on any structural violation.
    """
    pack_path = Path(pack_dir)
    if not pack_path.is_dir():
        raise PackFormatError(f"pack directory does not exist: {pack_path}")

    manifest_path = pack_path / "pack.json"
    if not manifest_path.exists():
        raise PackFormatError(f"missing pack.json in {pack_path}")

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise PackFormatError(f"invalid pack.json: {exc}") from exc

    if not isinstance(manifest, dict):
        raise PackFormatError("pack.json must be a JSON object")

    missing = REQUIRED_MANIFEST_FIELDS - manifest.keys()
    if missing:
        raise PackFormatError(f"pack.json missing required fields: {sorted(missing)}")

    engine_compat = manifest.get("engine_compat", {})
    if not isinstance(engine_compat, dict):
        raise PackFormatError("engine_compat must be a dict")
    if "min" not in engine_compat:
        raise PackFormatError("engine_compat must specify 'min'")

    return manifest
