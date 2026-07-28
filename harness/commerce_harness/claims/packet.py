"""Export a self-contained evidence packet for a claim.

Produces a directory under workbench/exports/claims/{claim_id}/ containing:
- manifest.json  (claim metadata + evidence summary)
- evidence/       (supporting data)

Uses packs/sign.py pack_content_sha256 for content hashing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from commerce_harness.packs.sign import pack_content_sha256

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _gather_evidence(database: DuckDBMemory, claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect evidence bindings related to the claim's subject."""
    if claim["subject_kind"] != "unresolved_balance":
        return []

    unresolved_id = claim["subject_key"]
    rows = database.execute(
        """
        SELECT
            eb.binding_id, eb.ordinal, eb.snapshot_id, eb.source_sheet,
            eb.row_no, eb.field, eb.source_value, eb.normalization_version,
            er.evidence_kind, er.payload_json
        FROM unresolved_balance ub
        JOIN evidence_record er ON er.evidence_id = ub.evidence_id
        JOIN evidence_binding eb ON eb.evidence_id = er.evidence_id
        WHERE ub.unresolved_id = ?
        ORDER BY eb.ordinal
        """,
        [unresolved_id],
    ).fetchall()

    evidence = []
    for row in rows:
        evidence.append({
            "binding_id": row[0],
            "ordinal": row[1],
            "snapshot_id": row[2],
            "source_sheet": row[3],
            "row_no": row[4],
            "field": row[5],
            "source_value": row[6],
            "normalization_version": row[7],
            "evidence_kind": row[8],
        })
    return evidence


def _gather_invariant_context(
    database: DuckDBMemory,
    invariant_version_id: str,
) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT iv.invariant_version_id, iv.invariant_id, iv.semver,
               id.domain, id.family, id.title
        FROM invariant_version iv
        JOIN invariant_definition id ON id.invariant_id = iv.invariant_id
        WHERE iv.invariant_version_id = ?
        """,
        [invariant_version_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "invariant_version_id": row[0],
        "invariant_id": row[1],
        "semver": row[2],
        "domain": row[3],
        "family": row[4],
        "title": row[5],
    }


def export_packet(
    database: DuckDBMemory,
    claim_id: str,
    exports_root: Path,
) -> dict[str, Any]:
    """Build and write a claim evidence packet. Returns manifest dict + sha256."""
    from .service import get_claim

    claim = get_claim(database, claim_id)
    evidence = _gather_evidence(database, claim)
    invariant_ctx = _gather_invariant_context(
        database, claim["invariant_version_id"]
    )

    claims_root = (exports_root / "claims").resolve()
    packet_dir = (claims_root / claim_id).resolve()
    if packet_dir.parent != claims_root:
        raise ValueError(f"索赔编号不能用于构造目录: {claim_id}")
    packet_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = packet_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)

    evidence_summary = {
        "claim_id": claim_id,
        "binding_count": len(evidence),
        "bindings": evidence,
    }
    (evidence_dir / "summary.json").write_text(
        json.dumps(evidence_summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    manifest = {
        "format": "claim-packet-v1",
        "claim_id": claim_id,
        "contract_id": claim.get("contract_id"),
        "period_id": claim.get("period_id"),
        "store_id": claim.get("store_id"),
        "invariant_version_id": claim.get("invariant_version_id"),
        "invariant": invariant_ctx,
        "subject_kind": claim.get("subject_kind"),
        "subject_key": claim.get("subject_key"),
        "reason_code": claim.get("reason_code"),
        "claimed_amount": claim.get("claimed_amount"),
        "currency": claim.get("currency"),
        "evidence_count": len(evidence),
        "exported_at": datetime.now(UTC).isoformat(),
    }
    (packet_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    sha256 = pack_content_sha256(packet_dir)

    return {
        "claim_id": claim_id,
        "packet_dir": str(packet_dir),
        "packet_sha256": sha256,
        "evidence_count": len(evidence),
    }
