"""Record external (platform) verdicts on claims.

The ``external_verdict`` table captures the platform's response to a
submitted claim — acceptance, rejection, or recovery — so the harness
can close the feedback loop between its own invariant-based claims and
the real-world outcome.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory


def _now() -> datetime:
    return datetime.now(UTC)


def record_external_verdict(
    database: DuckDBMemory,
    *,
    claim_id: str,
    verdict: str,
    source: str,
    case_id: str | None = None,
    accepted_amount: Decimal | None = None,
    recovered_amount: Decimal | None = None,
    verdict_at: datetime | None = None,
    note: str | None = None,
) -> str:
    """Insert an external_verdict row and return the verdict_id."""
    if verdict not in {"accepted", "partially_accepted", "rejected", "recovered"}:
        raise ValueError(
            f"verdict must be one of accepted/partially_accepted/rejected/recovered, got: {verdict}"
        )

    verdict_id = f"ev_{uuid.uuid4().hex}"
    database.execute(
        """
        INSERT INTO external_verdict (
            verdict_id, claim_id, case_id, verdict,
            accepted_amount, recovered_amount, verdict_at,
            source, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            verdict_id,
            claim_id,
            case_id,
            verdict,
            accepted_amount,
            recovered_amount,
            verdict_at or _now(),
            source,
            note,
        ],
    )
    return verdict_id
