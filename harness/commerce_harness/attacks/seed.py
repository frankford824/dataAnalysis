"""Seed built-in attack cases for red-team validation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory

_SEED_ATTACKS: list[dict[str, object]] = [
    {
        "attack_id": "attack-anti-gaming-001",
        "target": "reconciliation_item",
        "method_json": {
            "technique": "anti_gaming",
            "description": "Insert a synthetic refund row that inflates platform-side totals",
            "payload": {"source_type": "alipay_ledger", "amount": "9999.99"},
        },
        "expected_detection": "invariant_violation:equality",
        "severity": "critical",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
    {
        "attack_id": "attack-bk-split-002",
        "target": "reconciliation_item",
        "method_json": {
            "technique": "business_key_split",
            "description": "Split a single order into two items with different business keys",
            "payload": {"original_key": "ORDER-X", "split_keys": ["ORDER-X-A", "ORDER-X-B"]},
        },
        "expected_detection": "balance:unresolved",
        "severity": "high",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
    {
        "attack_id": "attack-period-machine-003",
        "target": "accounting_period",
        "method_json": {
            "technique": "period_machine",
            "description": "Move a transaction from current period to next to defer recognition",
            "payload": {"shift_direction": "forward", "periods": 1},
        },
        "expected_detection": "invariant_violation:conservation",
        "severity": "high",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
    {
        "attack_id": "attack-tolerance-accum-004",
        "target": "invariant_evaluation",
        "method_json": {
            "technique": "tolerance_accumulation",
            "description": "Inject many sub-tolerance differences that accumulate materially",
            "payload": {"item_count": 200, "per_item_gap": "0.009"},
        },
        "expected_detection": "invariant_violation:equality",
        "severity": "medium",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
    {
        "attack_id": "attack-evidence-chain-005",
        "target": "evidence_binding",
        "method_json": {
            "technique": "evidence_chain_break",
            "description": "Remove evidence binding for a reconciliation item",
            "payload": {"target_item": "any_matched_item"},
        },
        "expected_detection": "evidence_policy:missing_binding",
        "severity": "high",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
    {
        "attack_id": "attack-gate-bypass-006",
        "target": "run_log",
        "method_json": {
            "technique": "gate_bypass",
            "description": "Force certifiable=true by manipulating metrics_json",
            "payload": {"override_field": "certifiable", "forced_value": True},
        },
        "expected_detection": "trust_tier:blocked",
        "severity": "critical",
        "discovered_by": "builtin",
        "origin_pack": "ecommerce_settlement",
    },
]


def ensure_seed_attacks(database: DuckDBMemory) -> int:
    seeded = 0
    for attack in _SEED_ATTACKS:
        method = attack["method_json"]
        database.execute(
            """
            INSERT INTO attack_case (
                attack_id, target, method_json, expected_detection,
                severity, discovered_by, origin_pack
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (attack_id) DO NOTHING
            """,
            [
                attack["attack_id"],
                attack["target"],
                json.dumps(method, ensure_ascii=False, sort_keys=True),
                attack["expected_detection"],
                attack["severity"],
                attack["discovered_by"],
                attack.get("origin_pack"),
            ],
        )
        seeded += 1
    return seeded


def seed_attacks_from_rejected_claims(
    database: DuckDBMemory,
    *,
    claim_ids: Sequence[str] | None = None,
) -> int:
    """Promote rejected claims into the attack library for future experiments.

    Pass ``claim_ids`` to seed only the claims that just changed; the full scan
    is for backfills.
    """

    if claim_ids is not None:
        if not claim_ids:
            return 0
        placeholders = ", ".join("?" for _ in claim_ids)
        rows = database.execute(
            f"""
            SELECT claim_id, reason_code, claimed_amount, subject_kind, subject_key
            FROM claim
            WHERE status = 'rejected' AND claim_id IN ({placeholders})
            """,
            list(claim_ids),
        ).fetchall()
    else:
        rows = database.execute(
            """
            SELECT claim_id, reason_code, claimed_amount, subject_kind, subject_key
            FROM claim
            WHERE status = 'rejected'
              AND claim_id NOT IN (
                  SELECT replace(attack_id, 'attack-claim-reject-', '')
                  FROM attack_case
                  WHERE discovered_by = 'external_verdict'
              )
            """
        ).fetchall()
    seeded = 0
    for row in rows:
        claim_id, reason_code, amount, subject_kind, subject_key = row
        attack_id = f"attack-claim-reject-{claim_id}"
        method = {
            "technique": "external_rejection",
            "description": "平台驳回的索赔样本，用于回归试算",
            "payload": {
                "claim_id": claim_id,
                "reason_code": reason_code,
                "amount": format(amount, "f") if amount is not None else "0",
                "subject_kind": subject_kind,
                "subject_key": subject_key,
            },
        }
        database.execute(
            """
            INSERT INTO attack_case (
                attack_id, target, method_json, expected_detection,
                severity, discovered_by, origin_pack
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (attack_id) DO NOTHING
            """,
            [
                attack_id,
                "claim",
                json.dumps(method, ensure_ascii=False, sort_keys=True),
                f"claim:rejected:{reason_code}",
                "high",
                "external_verdict",
                "claim_feedback",
            ],
        )
        seeded += 1
    return seeded
