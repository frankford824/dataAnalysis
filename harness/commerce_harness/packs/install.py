"""Pack installation: layer-order application of rule packs.

Layer priority (upper wins):
    builtin < platform < industry < enterprise

Installation is fail-closed: an unsigned pack, a pack that fails the redaction
scan, or a pack whose layer cannot be determined is rejected rather than
installed with a guess.
"""

from __future__ import annotations

import json
from collections.abc import Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from commerce_harness.spec.invariant import parse_invariant

from .format import PackFormatError, validate_pack
from .redact_scan import RedactViolation, scan_pack
from .sign import verify_pack

LAYER_ORDER = ("builtin", "platform", "industry", "enterprise")
_LAYER_PRIORITY = {layer: idx for idx, layer in enumerate(LAYER_ORDER)}

SIGNATURE_FILENAME = "pack.sig"


class PackInstallError(RuntimeError):
    """Raised when a pack cannot be installed safely."""


@dataclass
class InstalledPack:
    pack_id: str
    domain: str
    version: str
    layer: str
    path: Path
    manifest: dict[str, Any]
    invariants: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    signature_backend: str = "unverified"
    redaction_violations: list[RedactViolation] = field(default_factory=list)


def _detect_layer(pack_path: Path) -> str | None:
    """Infer the layer from the directory structure, or ``None`` if unknown."""
    for part in reversed(pack_path.parts):
        if part in _LAYER_PRIORITY:
            return part
    return None


def _load_json_documents(path: Path) -> list[dict[str, Any]]:
    """Load a JSON file that holds either one object or a list of objects."""
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise PackInstallError(f"unreadable pack document {path.name}: {exc}") from exc
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise PackInstallError(f"pack document must be an object or list: {path.name}")


def _load_collection(pack_path: Path, name: str) -> list[dict[str, Any]]:
    """Collect ``<name>.json`` plus every document under ``<name>/``.

    Packs ship rules as one file per rule so that review and signing operate at
    rule granularity, so a directory has to be part of the contract.
    """
    documents = _load_json_documents(pack_path / f"{name}.json")
    directory = pack_path / name
    if directory.is_dir():
        for path in sorted(
            (item for item in directory.rglob("*.json") if item.is_file()),
            key=lambda item: item.relative_to(directory).as_posix(),
        ):
            documents.extend(_load_json_documents(path))
    return documents


def install_pack(
    pack_dir: str | Path,
    *,
    layer: str | None = None,
    public_key: Any = None,
    hmac_key: bytes | None = None,
    allow_dev_hmac: bool = False,
    require_signature: bool = True,
    artifact_strings: Set[str] | None = None,
) -> InstalledPack:
    """Load, verify and validate a single pack from a directory."""
    pack_path = Path(pack_dir)
    manifest = validate_pack(pack_path)

    resolved_layer = layer or _detect_layer(pack_path)
    if resolved_layer is None:
        raise PackInstallError(
            f"cannot determine layer for {pack_path}; pass layer= explicitly"
        )
    if resolved_layer not in _LAYER_PRIORITY:
        raise PackFormatError(f"unknown layer: {resolved_layer!r}")

    signature_backend = "unverified"
    signature_path = pack_path / SIGNATURE_FILENAME
    if signature_path.is_file():
        try:
            with open(signature_path, encoding="utf-8") as handle:
                sig_data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise PackInstallError(f"unreadable {SIGNATURE_FILENAME}: {exc}") from exc
        verified = verify_pack(
            pack_path,
            sig_data,
            public_key=public_key,
            hmac_key=hmac_key,
            allow_dev_hmac=allow_dev_hmac,
        )
        if not verified:
            raise PackInstallError(f"signature verification failed for {pack_path}")
        signature_backend = str(sig_data.get("backend", "unknown"))
    elif require_signature:
        raise PackInstallError(
            f"missing {SIGNATURE_FILENAME} in {pack_path}; "
            "pass require_signature=False only for local development"
        )

    violations = scan_pack(pack_path, artifact_strings=artifact_strings)
    if violations:
        preview = ", ".join(
            f"{violation.file}{violation.path}:{violation.reason}"
            for violation in violations[:5]
        )
        raise PackInstallError(
            f"redaction scan rejected {pack_path} ({len(violations)} violations): "
            f"{preview}"
        )

    return InstalledPack(
        pack_id=manifest["pack_id"],
        domain=manifest["domain"],
        version=manifest["version"],
        layer=resolved_layer,
        path=pack_path,
        manifest=manifest,
        invariants=_load_collection(pack_path, "invariants"),
        rules=_load_collection(pack_path, "rules"),
        signature_backend=signature_backend,
        redaction_violations=violations,
    )


def _invariant_key(invariant: dict[str, Any]) -> str:
    """Identity of an invariant, independent of its human-facing title.

    Keying on the title would let any pack override a built-in invariant just
    by reusing its label.
    """
    declared = invariant.get("invariant_id")
    if isinstance(declared, str) and declared:
        return declared
    try:
        return parse_invariant(invariant).invariant_id
    except (ValueError, TypeError, KeyError):
        return json.dumps(invariant, ensure_ascii=False, sort_keys=True)


def merge_packs(
    packs: list[InstalledPack],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge packs by layer order, higher layer wins on conflicts.

    Returns ``(merged_invariants, merged_rules)``.
    """
    sorted_packs = sorted(
        packs,
        key=lambda pack: _LAYER_PRIORITY.get(pack.layer, len(LAYER_ORDER)),
    )

    invariant_map: dict[str, dict[str, Any]] = {}
    rule_map: dict[str, dict[str, Any]] = {}

    for pack in sorted_packs:
        for invariant in pack.invariants:
            invariant_map[_invariant_key(invariant)] = invariant
        for rule in pack.rules:
            key = rule.get("rule_id") or json.dumps(
                rule, ensure_ascii=False, sort_keys=True
            )
            rule_map[str(key)] = rule

    return list(invariant_map.values()), list(rule_map.values())
