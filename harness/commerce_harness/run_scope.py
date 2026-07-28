"""Which run's numbers are the current answer.

Every reconcile writes a fresh immutable row set keyed by ``run_id``, so any
aggregate that forgets to pin one run sums every historical attempt (and every
period) together. Anything that shows money to a human, or freezes money into a
certification, must go through this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from commerce_harness.memory.database import DuckDBMemory

LATEST_SUCCEEDED_RECONCILE_PER_PERIOD = """
SELECT run_id, period_id
FROM (
    SELECT
        run_id,
        period_id,
        row_number() OVER (
            PARTITION BY period_id
            ORDER BY coalesce(finished_at, started_at) DESC, run_id DESC
        ) AS rank_in_period
    FROM run_log
    WHERE run_kind = 'reconcile'
      AND status = 'succeeded'
      AND period_id IS NOT NULL
)
WHERE rank_in_period = 1
"""


@dataclass(frozen=True)
class RunScope:
    """The single (period, run) pair whose rows are authoritative."""

    period_id: str
    run_id: str
    store_id: str | None
    period_start: str | None
    period_end: str | None


def latest_run_for_period(
    database: DuckDBMemory, period_id: str
) -> str | None:
    """Return the newest successful reconcile run of one period."""
    row = database.execute(
        f"""
        WITH latest AS ({LATEST_SUCCEEDED_RECONCILE_PER_PERIOD})
        SELECT run_id FROM latest WHERE period_id = ?
        """,
        [period_id],
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def scope_for_period(
    database: DuckDBMemory, period_id: str
) -> RunScope | None:
    row = database.execute(
        f"""
        WITH latest AS ({LATEST_SUCCEEDED_RECONCILE_PER_PERIOD})
        SELECT latest.run_id, period.store_id,
               period.period_start, period.period_end
        FROM latest
        JOIN accounting_period AS period
             ON period.period_id = latest.period_id
        WHERE latest.period_id = ?
        """,
        [period_id],
    ).fetchone()
    if row is None:
        return None
    return RunScope(
        period_id=period_id,
        run_id=str(row[0]),
        store_id=str(row[1]) if row[1] else None,
        period_start=str(row[2]) if row[2] else None,
        period_end=str(row[3]) if row[3] else None,
    )


def latest_scope(database: DuckDBMemory) -> RunScope | None:
    """The most recent period that has a successful reconcile run."""
    row = database.execute(
        f"""
        WITH latest AS ({LATEST_SUCCEEDED_RECONCILE_PER_PERIOD})
        SELECT latest.period_id, latest.run_id, period.store_id,
               period.period_start, period.period_end
        FROM latest
        JOIN accounting_period AS period
             ON period.period_id = latest.period_id
        ORDER BY period.period_end DESC, period.period_start DESC,
                 latest.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return RunScope(
        period_id=str(row[0]),
        run_id=str(row[1]),
        store_id=str(row[2]) if row[2] else None,
        period_start=str(row[3]) if row[3] else None,
        period_end=str(row[4]) if row[4] else None,
    )
