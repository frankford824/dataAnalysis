"""Aggregate acceptance/recovery rates by invariant_version_id.

Materializes statistics into the ``invariant_claim_stats`` table so that
downstream analyses (dashboards, invariant quality reviews) can access
pre-computed claim outcome metrics without scanning the full claim table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory


def refresh_invariant_claim_stats(database: DuckDBMemory) -> list[dict[str, Any]]:
    """Recompute invariant_claim_stats from the current claim table.

    Upserts one row per invariant_version_id that has at least one claim.
    Returns the refreshed stats rows.
    """
    database.execute(
        """
        DELETE FROM invariant_claim_stats
        WHERE invariant_version_id NOT IN (
            SELECT DISTINCT invariant_version_id FROM claim
        )
        """
    )

    database.execute(
        """
        INSERT INTO invariant_claim_stats (
            invariant_version_id,
            total_claims,
            accepted_count,
            partially_accepted_count,
            rejected_count,
            recovered_count,
            total_claimed,
            total_accepted,
            total_recovered,
            updated_at
        )
        SELECT
            invariant_version_id,
            count(*),
            count(*) FILTER (WHERE status = 'accepted'),
            count(*) FILTER (WHERE status = 'partially_accepted'),
            count(*) FILTER (WHERE status = 'rejected'),
            count(*) FILTER (WHERE status = 'recovered'),
            coalesce(sum(claimed_amount), 0),
            coalesce(sum(accepted_amount), 0),
            coalesce(sum(recovered_amount), 0),
            current_timestamp
        FROM claim
        GROUP BY invariant_version_id
        ON CONFLICT (invariant_version_id) DO UPDATE SET
            total_claims = EXCLUDED.total_claims,
            accepted_count = EXCLUDED.accepted_count,
            partially_accepted_count = EXCLUDED.partially_accepted_count,
            rejected_count = EXCLUDED.rejected_count,
            recovered_count = EXCLUDED.recovered_count,
            total_claimed = EXCLUDED.total_claimed,
            total_accepted = EXCLUDED.total_accepted,
            total_recovered = EXCLUDED.total_recovered,
            updated_at = EXCLUDED.updated_at
        """
    )

    rows = database.execute(
        """
        SELECT invariant_version_id, total_claims,
               accepted_count, partially_accepted_count,
               rejected_count, recovered_count,
               total_claimed, total_accepted, total_recovered
        FROM invariant_claim_stats
        ORDER BY total_claims DESC
        """
    ).fetchall()

    return [
        {
            "invariant_version_id": row[0],
            "total_claims": row[1],
            "accepted_count": row[2],
            "partially_accepted_count": row[3],
            "rejected_count": row[4],
            "recovered_count": row[5],
            "total_claimed": str(row[6]),
            "total_accepted": str(row[7]),
            "total_recovered": str(row[8]),
            "acceptance_rate": (
                round((row[2] + row[3]) / row[1], 4) if row[1] > 0 else 0
            ),
            "recovery_rate": (
                round(row[5] / row[1], 4) if row[1] > 0 else 0
            ),
        }
        for row in rows
    ]
