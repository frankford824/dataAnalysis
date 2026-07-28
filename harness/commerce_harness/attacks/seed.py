"""Seed built-in attack cases for red-team validation."""

from __future__ import annotations

import json
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
