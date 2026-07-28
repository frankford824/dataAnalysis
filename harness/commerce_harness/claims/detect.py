"""Detect claimable discrepancies from unresolved_balance + invariant_evaluation.

Criteria for generating a draft claim:
- Lineage frozen if available (evidence bindings use current normalization)
- abs(amount) > materiality floor (Decimal)
- reason_code in whitelist
- Results sorted by abs(amount) descending
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory

CLAIMABLE_REASON_CODES = frozenset({
    "amount_mismatch",
    "missing_side",
    "missing_cash_bridge",
    "cash_amount_mismatch",
})

DEFAULT_MATERIALITY_FLOOR = Decimal("0.01")


def detect_claims(
    database: DuckDBMemory,
    *,
    period_id: str | None = None,
    store_id: str | None = None,
    materiality_floor: Decimal = DEFAULT_MATERIALITY_FLOOR,
) -> list[dict[str, Any]]:
    """Scan unresolved_balance joined with invariant_evaluation to find claimable items.

    Returns a list of newly created draft claim dicts, sorted by abs(amount) desc.
    """
    conditions: list[str] = [
        "ub.status = 'open'",
    ]
    params: list[Any] = []

    if period_id:
        conditions.append("rb.period_id = ?")
        params.append(period_id)
    if store_id:
        conditions.append(
            "rb.contract_id IN ("
            "SELECT contract_id FROM reconciliation_contract WHERE store_id = ?"
            ")"
        )
        params.append(store_id)

    where = " AND ".join(conditions)

    rows = database.execute(
        f"""
        SELECT
            ub.unresolved_id,
            ub.balance_id,
            ub.reason_code,
            ub.amount,
            rb.contract_id,
            rb.period_id,
            rb.run_id,
            rc.store_id
        FROM unresolved_balance ub
        JOIN reconciliation_balance rb ON rb.balance_id = ub.balance_id
        JOIN reconciliation_contract rc ON rc.contract_id = rb.contract_id
        WHERE {where}
        ORDER BY abs(ub.amount) DESC
        """,
        params,
    ).fetchall()

    created: list[dict[str, Any]] = []

    for row in rows:
        unresolved_id = row[0]
        reason_code = str(row[2])
        amount = Decimal(str(row[3]))
        contract_id = str(row[4])
        row_period_id = str(row[5])
        run_id = str(row[6])
        row_store_id = str(row[7])

        if reason_code not in CLAIMABLE_REASON_CODES:
            continue
        if abs(amount) < materiality_floor:
            continue

        existing = database.execute(
            """
            SELECT claim_id FROM claim
            WHERE subject_kind = 'unresolved_balance'
              AND subject_key = ?
              AND status NOT IN ('closed', 'rejected')
            """,
            [unresolved_id],
        ).fetchone()
        if existing:
            continue

        invariant_row = database.execute(
            """
            SELECT ie.invariant_version_id
            FROM invariant_evaluation ie
            WHERE ie.run_id = ?
              AND ie.status = 'violated'
              AND ie.is_material = true
            ORDER BY abs(ie.gap_amount) DESC
            LIMIT 1
            """,
            [run_id],
        ).fetchone()

        if invariant_row is None:
            invariant_version_id = _fallback_invariant_version(database)
        else:
            invariant_version_id = str(invariant_row[0])

        if invariant_version_id is None:
            continue

        claim_id = f"claim_{uuid.uuid4().hex}"
        now = datetime.now(UTC)

        database.execute(
            """
            INSERT INTO claim (
                claim_id, contract_id, period_id, store_id,
                invariant_version_id, subject_kind, subject_key,
                reason_code, claimed_amount, currency, status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, 'unresolved_balance', ?, ?, ?, 'CNY', 'draft', ?)
            """,
            [
                claim_id,
                contract_id,
                row_period_id,
                row_store_id,
                invariant_version_id,
                unresolved_id,
                reason_code,
                amount,
                now,
            ],
        )

        created.append({
            "claim_id": claim_id,
            "contract_id": contract_id,
            "period_id": row_period_id,
            "store_id": row_store_id,
            "invariant_version_id": invariant_version_id,
            "reason_code": reason_code,
            "claimed_amount": str(amount),
            "status": "draft",
        })

    return created


def _fallback_invariant_version(database: DuckDBMemory) -> str | None:
    """Pick the first active invariant_version if no violated evaluation exists."""
    row = database.execute(
        """
        SELECT invariant_version_id
        FROM invariant_version
        WHERE status = 'active'
        LIMIT 1
        """,
    ).fetchone()
    return str(row[0]) if row else None
