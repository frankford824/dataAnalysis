"""Corpus persistence: write situation_fingerprint and adjudication_case rows.

These tables exist in the schema but had zero INSERT statements until now.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from commerce_harness.corpus.fingerprint import situation_fingerprint

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory


def _now() -> datetime:
    return datetime.now(UTC)


def upsert_fingerprint(
    database: DuckDBMemory,
    fingerprint_id: str,
    domain: str,
    features: dict[str, Any],
    *,
    invariant_family: str | None = None,
    period_id: str | None = None,
    store_id: str | None = None,
) -> None:
    """Insert or update a situation_fingerprint row.

    On conflict the occurrence_count is incremented, distinct_periods and
    distinct_stores are bumped when a new value is seen, and last_seen_at
    is refreshed.
    """
    now = _now()
    features_json = json.dumps(features, ensure_ascii=False, sort_keys=True, default=str)

    existing = database.execute(
        "SELECT occurrence_count, distinct_periods, distinct_stores "
        "FROM situation_fingerprint WHERE fingerprint_id = ?",
        [fingerprint_id],
    ).fetchone()

    if existing is None:
        database.execute(
            """
            INSERT INTO situation_fingerprint (
                fingerprint_id, domain, invariant_family, features_json,
                occurrence_count, distinct_periods, distinct_stores,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            [
                fingerprint_id,
                domain,
                invariant_family,
                features_json,
                1 if period_id else 0,
                1 if store_id else 0,
                now,
                now,
            ],
        )
    else:
        old_count, old_periods, old_stores = existing
        new_periods = old_periods
        new_stores = old_stores
        if period_id:
            period_seen = database.execute(
                """
                SELECT 1 FROM adjudication_case
                WHERE fingerprint_id = ? AND period_id = ?
                LIMIT 1
                """,
                [fingerprint_id, period_id],
            ).fetchone()
            if period_seen is None:
                new_periods = old_periods + 1
        if store_id:
            store_seen = database.execute(
                """
                SELECT 1 FROM adjudication_case
                WHERE fingerprint_id = ? AND store_id = ?
                LIMIT 1
                """,
                [fingerprint_id, store_id],
            ).fetchone()
            if store_seen is None:
                new_stores = old_stores + 1
        database.execute(
            """
            UPDATE situation_fingerprint
            SET occurrence_count = ?,
                distinct_periods = ?,
                distinct_stores = ?,
                last_seen_at = ?
            WHERE fingerprint_id = ?
            """,
            [old_count + 1, new_periods, new_stores, now, fingerprint_id],
        )


_REQUIRED_CASE_FIELDS = frozenset({
    "fingerprint_id",
    "domain",
    "subject_kind",
    "subject_key",
    "disposition_kind",
    "rationale",
    "decided_by",
    "decided_role",
    "evidence_binding_digest",
})


def insert_case(database: DuckDBMemory, case: dict[str, Any]) -> str:
    """Write an adjudication_case row. Returns the case_id."""
    missing = _REQUIRED_CASE_FIELDS - set(case)
    if missing:
        raise ValueError(f"adjudication_case missing required fields: {sorted(missing)}")

    case_id = case.get("case_id") or f"case_{uuid.uuid4().hex}"
    now = _now()
    outcome_json = (
        json.dumps(case["outcome_json"], ensure_ascii=False, sort_keys=True, default=str)
        if case.get("outcome_json") is not None
        else None
    )

    database.execute(
        """
        INSERT INTO adjudication_case (
            case_id, fingerprint_id, domain, subject_kind, subject_key,
            period_id, store_id, disposition_kind, posting_target,
            rationale, decided_by, decided_role, model_suggested_by,
            decided_at, evidence_binding_digest, verified_by_experiment,
            outcome_json, promoted_to_rule_version, review_due_at,
            export_allowed, consent_record_id, redaction_profile_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            case_id,
            case["fingerprint_id"],
            case["domain"],
            case["subject_kind"],
            case["subject_key"],
            case.get("period_id"),
            case.get("store_id"),
            case["disposition_kind"],
            case.get("posting_target"),
            case["rationale"],
            case["decided_by"],
            case["decided_role"],
            case.get("model_suggested_by"),
            case.get("decided_at", now),
            case["evidence_binding_digest"],
            case.get("verified_by_experiment"),
            outcome_json,
            case.get("promoted_to_rule_version"),
            case.get("review_due_at"),
            case.get("export_allowed", False),
            case.get("consent_record_id"),
            case.get("redaction_profile_version"),
        ],
    )
    return case_id


def record_review_as_case(
    database: DuckDBMemory,
    *,
    domain: str,
    subject_kind: str,
    subject_key: str,
    disposition_kind: str,
    rationale: str,
    decided_by: str,
    decided_role: str,
    evidence_binding_digest: str,
    source_kinds: list[str],
    amounts: list[Any],
    business_description: str | None = None,
    invariant_family: str | None = None,
    period_id: str | None = None,
    store_id: str | None = None,
    posting_target: str | None = None,
    model_suggested_by: str | None = None,
    outcome_json: dict[str, Any] | None = None,
) -> str:
    """Compute a situation fingerprint, upsert it, and insert a case.

    Combines fingerprinting + persistence in one call for callers that
    already have the structural features at hand.
    """
    fp_id = situation_fingerprint(
        source_kinds=source_kinds,
        amounts=amounts,
        business_description=business_description,
        invariant_family=invariant_family,
    )

    features = {
        "source_kinds": sorted(set(source_kinds)),
        "domain": domain,
    }

    upsert_fingerprint(
        database,
        fp_id,
        domain,
        features,
        invariant_family=invariant_family,
        period_id=period_id,
        store_id=store_id,
    )

    case_id = insert_case(database, {
        "fingerprint_id": fp_id,
        "domain": domain,
        "subject_kind": subject_kind,
        "subject_key": subject_key,
        "disposition_kind": disposition_kind,
        "posting_target": posting_target,
        "rationale": rationale,
        "decided_by": decided_by,
        "decided_role": decided_role,
        "model_suggested_by": model_suggested_by,
        "evidence_binding_digest": evidence_binding_digest,
        "period_id": period_id,
        "store_id": store_id,
        "outcome_json": outcome_json,
    })

    return case_id
