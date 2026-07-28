"""Claim state machine with append-only event logging.

Transition graph::

    draft → packaged → submitted → accepted         → recovered → closed
                                  → partially_accepted → recovered → closed
                                  → rejected         → closed

Amounts (claimed_amount, accepted_amount, recovered_amount) are immutable once
set: the service refuses any write that would overwrite a non-NULL amount field.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory

logger = logging.getLogger(__name__)

CLAIM_STATUSES = frozenset({
    "draft", "packaged", "submitted",
    "accepted", "partially_accepted", "rejected",
    "recovered", "closed",
})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft":                frozenset({"packaged"}),
    "packaged":             frozenset({"submitted"}),
    "submitted":            frozenset({"accepted", "partially_accepted", "rejected"}),
    "accepted":             frozenset({"recovered", "closed"}),
    "partially_accepted":   frozenset({"recovered", "closed"}),
    "rejected":             frozenset({"closed"}),
    "recovered":            frozenset({"closed"}),
}


class InvalidClaimTransition(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _append_event(
    database: DuckDBMemory,
    *,
    claim_id: str,
    from_status: str,
    to_status: str,
    actor: str,
    response_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    event_id = f"cevt_{uuid.uuid4().hex}"
    database.execute(
        """
        INSERT INTO claim_event (
            event_id, claim_id, from_status, to_status,
            actor, response_text, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            event_id,
            claim_id,
            from_status,
            to_status,
            actor,
            response_text,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str),
            _now(),
        ],
    )
    return event_id


def _validate_transition(current: str, target: str) -> None:
    allowed = _TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidClaimTransition(
            f"不允许从 {current} 转换到 {target}，"
            f"允许的目标状态: {sorted(allowed)}"
        )


def _fetch_claim(database: DuckDBMemory, claim_id: str) -> dict[str, Any]:
    row = database.execute(
        """
        SELECT claim_id, status, claimed_amount, accepted_amount,
               recovered_amount, period_id
        FROM claim WHERE claim_id = ?
        """,
        [claim_id],
    ).fetchone()
    if row is None:
        raise LookupError(f"claim {claim_id} not found")
    return {
        "claim_id": row[0],
        "status": row[1],
        "claimed_amount": row[2],
        "accepted_amount": row[3],
        "recovered_amount": row[4],
        "period_id": row[5],
    }


def transition_to_packaged(
    database: DuckDBMemory,
    claim_id: str,
    *,
    packet_sha256: str,
    actor: str = "system",
) -> dict[str, Any]:
    claim = _fetch_claim(database, claim_id)
    _validate_transition(claim["status"], "packaged")
    database.execute(
        "UPDATE claim SET status = 'packaged', packet_sha256 = ? WHERE claim_id = ?",
        [packet_sha256, claim_id],
    )
    _append_event(
        database,
        claim_id=claim_id,
        from_status=claim["status"],
        to_status="packaged",
        actor=actor,
        payload={"packet_sha256": packet_sha256},
    )
    return {"claim_id": claim_id, "status": "packaged"}


def transition_to_submitted(
    database: DuckDBMemory,
    claim_id: str,
    *,
    operator_name: str,
    external_ref: str | None = None,
) -> dict[str, Any]:
    if not operator_name or not operator_name.strip():
        raise ValueError("操作人不能为空")

    claim = _fetch_claim(database, claim_id)
    _validate_transition(claim["status"], "submitted")

    now = _now()
    database.execute(
        """
        UPDATE claim
        SET status = 'submitted', operator_name = ?, submitted_at = ?, external_ref = ?
        WHERE claim_id = ?
        """,
        [operator_name.strip(), now, external_ref, claim_id],
    )
    _append_event(
        database,
        claim_id=claim_id,
        from_status=claim["status"],
        to_status="submitted",
        actor=operator_name.strip(),
        payload={"external_ref": external_ref},
    )
    return {"claim_id": claim_id, "status": "submitted", "submitted_at": now.isoformat()}


def _try_record_external_verdict(
    database: DuckDBMemory,
    *,
    claim_id: str,
    verdict: str,
    accepted_amount: Decimal | None = None,
    recovered_amount: Decimal | None = None,
    source: str = "claims_service",
) -> None:
    """Record the platform's verdict for learning.

    The claim transition itself already succeeded, so a failure here must not
    undo the operator's action — but it must be visible in the log instead of
    silently losing the feedback signal.
    """
    try:
        from commerce_harness.corpus.external import record_external_verdict

        record_external_verdict(
            database,
            claim_id=claim_id,
            verdict=verdict,
            source=source,
            accepted_amount=accepted_amount,
            recovered_amount=recovered_amount,
        )
    except Exception:
        logger.exception(
            "claim %s moved to %s but the external verdict was not recorded",
            claim_id,
            verdict,
        )


def transition_to_response(
    database: DuckDBMemory,
    claim_id: str,
    *,
    operator_name: str,
    verdict: str,
    accepted_amount: Decimal | None = None,
    response_text: str | None = None,
) -> dict[str, Any]:
    if verdict not in {"accepted", "partially_accepted", "rejected"}:
        raise ValueError(f"verdict 必须是 accepted/partially_accepted/rejected，收到: {verdict}")
    if not operator_name or not operator_name.strip():
        raise ValueError("操作人不能为空")

    claim = _fetch_claim(database, claim_id)
    _validate_transition(claim["status"], verdict)

    if claim["accepted_amount"] is not None:
        raise InvalidClaimTransition("accepted_amount 已设置，不能重写")

    now = _now()
    effective_accepted = accepted_amount if verdict != "rejected" else Decimal("0")

    database.execute(
        """
        UPDATE claim
        SET status = ?, responded_at = ?, accepted_amount = ?
        WHERE claim_id = ?
        """,
        [verdict, now, effective_accepted, claim_id],
    )
    _append_event(
        database,
        claim_id=claim_id,
        from_status=claim["status"],
        to_status=verdict,
        actor=operator_name.strip(),
        response_text=response_text,
        payload={
            "verdict": verdict,
            "accepted_amount": str(effective_accepted) if effective_accepted is not None else None,
        },
    )

    _try_record_external_verdict(
        database,
        claim_id=claim_id,
        verdict=verdict,
        accepted_amount=effective_accepted,
        source=f"operator:{operator_name.strip()}",
    )
    if verdict == "rejected":
        try:
            from commerce_harness.attacks.seed import seed_attacks_from_rejected_claims

            seed_attacks_from_rejected_claims(database, claim_ids=[claim_id])
        except Exception:
            logger.exception(
                "claim %s was rejected but the attack library was not updated",
                claim_id,
            )

    return {"claim_id": claim_id, "status": verdict, "responded_at": now.isoformat()}


def transition_to_recovered(
    database: DuckDBMemory,
    claim_id: str,
    *,
    operator_name: str,
    recovered_amount: Decimal,
) -> dict[str, Any]:
    if not operator_name or not operator_name.strip():
        raise ValueError("操作人不能为空")
    if recovered_amount <= Decimal("0"):
        raise ValueError("回收金额必须大于零")

    claim = _fetch_claim(database, claim_id)
    _validate_transition(claim["status"], "recovered")

    if claim["recovered_amount"] is not None:
        raise InvalidClaimTransition("recovered_amount 已设置，不能重写")

    now = _now()
    database.execute(
        """
        UPDATE claim
        SET status = 'recovered', recovered_amount = ?, recovered_at = ?
        WHERE claim_id = ?
        """,
        [recovered_amount, now, claim_id],
    )
    _append_event(
        database,
        claim_id=claim_id,
        from_status=claim["status"],
        to_status="recovered",
        actor=operator_name.strip(),
        payload={"recovered_amount": str(recovered_amount)},
    )

    # A closed period can only change through an explicit adjustment entry; an
    # open one simply gets recomputed. Either way the outcome is reported rather
    # than swallowed, so recovered money is never silently left unposted.
    from commerce_harness.period_service import (
        LOCKED_STATUSES,
        effective_period_status,
        post_adjustment,
    )

    adjustment_id: str | None = None
    adjustment_error: str | None = None
    period_status = effective_period_status(database, claim["period_id"])
    if period_status in LOCKED_STATUSES:
        try:
            result = post_adjustment(
                database,
                claim["period_id"],
                operator_name.strip(),
                amount=recovered_amount,
                reason=f"claim recovery: {claim_id}",
            )
            adjustment_id = result.get("adjustment_id")
            database.execute(
                "UPDATE claim SET adjustment_id = ? WHERE claim_id = ?",
                [adjustment_id, claim_id],
            )
        except Exception as exc:
            adjustment_error = str(exc)
            logger.exception(
                "claim %s recovery was recorded but the period adjustment failed",
                claim_id,
            )
            _append_event(
                database,
                claim_id=claim_id,
                from_status="recovered",
                to_status="recovered",
                actor=operator_name.strip(),
                payload={
                    "adjustment_failed": True,
                    "reason": adjustment_error,
                    "recovered_amount": str(recovered_amount),
                },
            )

    _try_record_external_verdict(
        database,
        claim_id=claim_id,
        verdict="recovered",
        recovered_amount=recovered_amount,
        source=f"operator:{operator_name.strip()}",
    )

    return {
        "claim_id": claim_id,
        "status": "recovered",
        "recovered_amount": str(recovered_amount),
        "recovered_at": now.isoformat(),
        "adjustment_id": adjustment_id,
        "adjustmentError": adjustment_error,
        "periodStatus": period_status,
    }


def transition_to_closed(
    database: DuckDBMemory,
    claim_id: str,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    claim = _fetch_claim(database, claim_id)
    _validate_transition(claim["status"], "closed")

    database.execute(
        "UPDATE claim SET status = 'closed' WHERE claim_id = ?",
        [claim_id],
    )
    _append_event(
        database,
        claim_id=claim_id,
        from_status=claim["status"],
        to_status="closed",
        actor=actor,
    )
    return {"claim_id": claim_id, "status": "closed"}


def get_claim(database: DuckDBMemory, claim_id: str) -> dict[str, Any]:
    row = database.execute(
        """
        SELECT claim_id, contract_id, period_id, store_id,
               invariant_version_id, subject_kind, subject_key,
               reason_code, claimed_amount, currency, status,
               packet_sha256, operator_name, submitted_at,
               external_ref, responded_at, accepted_amount,
               recovered_amount, recovered_at, adjustment_id,
               created_at
        FROM claim WHERE claim_id = ?
        """,
        [claim_id],
    ).fetchone()
    if row is None:
        raise LookupError(f"claim {claim_id} not found")
    cols = [
        "claim_id", "contract_id", "period_id", "store_id",
        "invariant_version_id", "subject_kind", "subject_key",
        "reason_code", "claimed_amount", "currency", "status",
        "packet_sha256", "operator_name", "submitted_at",
        "external_ref", "responded_at", "accepted_amount",
        "recovered_amount", "recovered_at", "adjustment_id",
        "created_at",
    ]
    result: dict[str, Any] = {}
    for i, col in enumerate(cols):
        val = row[i]
        if isinstance(val, Decimal):
            val = format(val, "f")
        elif isinstance(val, datetime):
            val = val.isoformat()
        result[col] = val
    return result


def list_claims(
    database: DuckDBMemory,
    *,
    period_id: str | None = None,
    store_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    if period_id:
        conditions.append("period_id = ?")
        params.append(period_id)
    if store_id:
        conditions.append("store_id = ?")
        params.append(store_id)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total_row = database.execute(
        f"SELECT count(*) FROM claim {where}", params,
    ).fetchone()
    total = int(total_row[0]) if total_row else 0

    rows = database.execute(
        f"""
        SELECT claim_id, contract_id, period_id, store_id,
               invariant_version_id, subject_kind, subject_key,
               reason_code, claimed_amount, currency, status,
               created_at
        FROM claim {where}
        ORDER BY abs(claimed_amount) DESC, created_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    items = []
    for row in rows:
        items.append({
            "claim_id": row[0],
            "contract_id": row[1],
            "period_id": row[2],
            "store_id": row[3],
            "invariant_version_id": row[4],
            "subject_kind": row[5],
            "subject_key": row[6],
            "reason_code": row[7],
            "claimed_amount": str(row[8]) if isinstance(row[8], Decimal) else str(row[8]),
            "currency": row[9],
            "status": row[10],
            "created_at": row[11].isoformat() if isinstance(row[11], datetime) else str(row[11]),
        })

    return {
        "total": total,
        "items": items,
        "hasMore": offset + limit < total,
    }
