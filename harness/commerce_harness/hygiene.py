"""Workspace hygiene: backfill empty runs, archive stale details, counterfactual schema.

Hygiene mutates the audit ledger, so every action leaves its own trace in
``maintenance_log`` and nothing is deleted until it has been copied into the
``archive`` schema. Details belonging to a locked period are never touched.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from commerce_harness.memory.experiment_tables import COUNTERFACTUAL_RESULT_TABLE_DDL

if TYPE_CHECKING:
    from commerce_harness.memory.database import DuckDBMemory

ARCHIVED_DETAIL_TABLES = (
    "reconciliation_item",
    "reconciliation_balance",
    "unresolved_balance",
    "reconciliation_link",
    "reconciliation_link_member",
)

LOCKED_PERIOD_STATUSES = ("preclosed", "closed", "restated")


def _record_maintenance(
    database: DuckDBMemory,
    *,
    action: str,
    affected_runs: int,
    affected_rows: int,
    details: dict[str, Any],
) -> None:
    from commerce_harness.code_identity import resolve_code_identity

    database.execute(
        """
        INSERT INTO maintenance_log (
            action_id, action, affected_runs, affected_rows, details_json, code_sha
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            f"maint_{uuid.uuid4().hex}",
            action,
            affected_runs,
            affected_rows,
            json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
            resolve_code_identity().value,
        ],
    )


def _empty_item_count_predicate() -> str:
    """Runs that explicitly recorded zero items.

    A missing ``item_count`` is not the same as zero: treating it as zero would
    silently reclassify historical runs that predate the metric.
    """
    return """
          json_extract(metrics_json, '$.item_count') IS NOT NULL
          AND coalesce(
              try_cast(
                  json_extract_string(metrics_json, '$.item_count') AS INTEGER
              ),
              -1
          ) = 0
    """


def backfill_empty_reconcile_runs(database: DuckDBMemory) -> int:
    """Mark empty succeeded reconcile runs as skipped (or cancelled on legacy CHECK)."""

    predicate = _empty_item_count_predicate()
    affected = [
        str(row[0])
        for row in database.execute(
            f"""
            SELECT run_id FROM run_log
            WHERE run_kind = 'reconcile'
              AND status = 'succeeded'
              AND {predicate}
            ORDER BY run_id
            """
        ).fetchall()
    ]
    if not affected:
        return 0

    supports_skipped = database.run_log_supports_skipped()
    if supports_skipped:
        database.execute(
            f"""
            UPDATE run_log
            SET status = 'skipped'
            WHERE run_kind = 'reconcile'
              AND status = 'succeeded'
              AND {predicate}
            """
        )
    else:
        database.execute(
            f"""
            UPDATE run_log
            SET status = 'cancelled', error_code = 'skipped_empty'
            WHERE run_kind = 'reconcile'
              AND status = 'succeeded'
              AND {predicate}
            """
        )
    _record_maintenance(
        database,
        action="backfill_empty_reconcile_runs",
        affected_runs=len(affected),
        affected_rows=len(affected),
        details={
            "from_status": "succeeded",
            "to_status": "skipped" if supports_skipped else "cancelled",
            "run_ids": affected,
        },
    )
    return len(affected)


def _ensure_archive_tables(database: DuckDBMemory) -> None:
    database.execute("CREATE SCHEMA IF NOT EXISTS archive")
    for table in ARCHIVED_DETAIL_TABLES:
        database.execute(
            f"CREATE TABLE IF NOT EXISTS archive.{table} AS "
            f"SELECT * FROM {table} WHERE false"
        )


def archive_stale_reconcile_details(database: DuckDBMemory) -> dict[str, int]:
    """Move all but the latest succeeded reconcile details per period to archive."""

    database.execute(
        """
        CREATE OR REPLACE TEMP TABLE hygiene_keep_runs AS
        SELECT period_id, run_id
        FROM (
            SELECT
                period_id,
                run_id,
                row_number() OVER (
                    PARTITION BY period_id
                    ORDER BY finished_at DESC NULLS LAST, started_at DESC
                ) AS position
            FROM run_log
            WHERE run_kind = 'reconcile'
              AND status = 'succeeded'
              AND period_id IS NOT NULL
        ) ranked
        WHERE position = 1
        """
    )
    locked_list = ", ".join(f"'{status}'" for status in LOCKED_PERIOD_STATUSES)
    database.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE hygiene_stale_runs AS
        SELECT run.run_id
        FROM run_log run
        JOIN accounting_period period ON period.period_id = run.period_id
        LEFT JOIN accounting_period_state period_state
          ON period_state.period_id = period.period_id
        WHERE run.run_kind = 'reconcile'
          AND run.status IN ('succeeded', 'skipped', 'cancelled')
          AND run.period_id IS NOT NULL
          AND coalesce(period_state.status, period.status) NOT IN ({locked_list})
          AND run.run_id NOT IN (SELECT run_id FROM hygiene_keep_runs)
        """
    )
    stale_runs = int(
        database.fetchone_required("SELECT count(*) FROM hygiene_stale_runs")[0]
    )
    kept_runs = int(
        database.fetchone_required("SELECT count(*) FROM hygiene_keep_runs")[0]
    )
    locked_runs = int(
        database.fetchone_required(
            f"""
            SELECT count(*)
            FROM run_log run
            JOIN accounting_period period ON period.period_id = run.period_id
            WHERE run.run_kind = 'reconcile'
              AND period.status IN ({locked_list})
              AND run.run_id NOT IN (SELECT run_id FROM hygiene_keep_runs)
            """
        )[0]
    )
    empty_result = {
        "archived_items": 0,
        "archived_balances": 0,
        "archived_unresolved": 0,
        "archived_links": 0,
        "archived_link_members": 0,
        "stale_runs": 0,
        "kept_runs": kept_runs,
        "locked_runs_skipped": locked_runs,
    }
    if stale_runs == 0:
        return empty_result

    _ensure_archive_tables(database)

    counts: dict[str, int] = {}
    with database.transaction() as connection:
        # Copy first, then delete, in FK-safe order. Counts come from the copy,
        # so the reported numbers are what actually moved.
        connection.execute(
            """
            INSERT INTO archive.unresolved_balance
            SELECT * FROM unresolved_balance
            WHERE balance_id IN (
                SELECT balance_id FROM reconciliation_balance
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
            )
            """
        )
        counts["archived_unresolved"] = int(
            connection.execute(
                """
                SELECT count(*) FROM unresolved_balance
                WHERE balance_id IN (
                    SELECT balance_id FROM reconciliation_balance
                    WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
                )
                """
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO archive.reconciliation_link_member
            SELECT * FROM reconciliation_link_member
            WHERE link_id IN (
                SELECT link_id FROM reconciliation_link
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
            )
            """
        )
        counts["archived_link_members"] = int(
            connection.execute(
                """
                SELECT count(*) FROM reconciliation_link_member
                WHERE link_id IN (
                    SELECT link_id FROM reconciliation_link
                    WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
                )
                """
            ).fetchone()[0]
        )
        for table, key in (
            ("reconciliation_link", "archived_links"),
            ("reconciliation_balance", "archived_balances"),
            ("reconciliation_item", "archived_items"),
        ):
            connection.execute(
                f"""
                INSERT INTO archive.{table}
                SELECT * FROM {table}
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
                """
            )
            counts[key] = int(
                connection.execute(
                    f"""
                    SELECT count(*) FROM {table}
                    WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
                    """
                ).fetchone()[0]
            )

        connection.execute(
            """
            DELETE FROM unresolved_balance
            WHERE balance_id IN (
                SELECT balance_id FROM reconciliation_balance
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
            )
            """
        )
        connection.execute(
            """
            DELETE FROM reconciliation_link_member
            WHERE link_id IN (
                SELECT link_id FROM reconciliation_link
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
            )
            """
        )
        for table in (
            "reconciliation_link",
            "reconciliation_balance",
            "reconciliation_item",
        ):
            connection.execute(
                f"""
                DELETE FROM {table}
                WHERE run_id IN (SELECT run_id FROM hygiene_stale_runs)
                """
            )

    result = {
        **empty_result,
        **counts,
        "stale_runs": stale_runs,
        "kept_runs": kept_runs,
        "locked_runs_skipped": locked_runs,
    }
    _record_maintenance(
        database,
        action="archive_stale_reconcile_details",
        affected_runs=stale_runs,
        affected_rows=sum(counts.values()),
        details=result,
    )
    return result


def ensure_counterfactual_schema(database: DuckDBMemory) -> None:
    database.execute("CREATE SCHEMA IF NOT EXISTS counterfactual")
    for ddl in COUNTERFACTUAL_RESULT_TABLE_DDL:
        qualified = ddl.replace(
            "CREATE TABLE IF NOT EXISTS ",
            "CREATE TABLE IF NOT EXISTS counterfactual.",
        )
        database.execute(qualified)
