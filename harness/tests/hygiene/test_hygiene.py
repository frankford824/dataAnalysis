from __future__ import annotations

from pathlib import Path

from commerce_harness.hygiene import (
    archive_stale_reconcile_details,
    backfill_empty_reconcile_runs,
    ensure_counterfactual_schema,
)
from commerce_harness.memory.database import DuckDBMemory


def _seed_runs(database: DuckDBMemory, *, period_status: str = "open") -> None:
    database.initialize()
    database.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES (
            'c1', 'c1', 'e1', 's1', 'taobao', 1, DATE '2026-01-01',
            'active', '{}'
        )
        """
    )
    database.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        ) VALUES (
            'p1', 'c1', 's1', DATE '2026-02-01', DATE '2026-02-28', ?
        )
        """,
        [period_status],
    )
    database.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status, metrics_json,
            finished_at
        ) VALUES
        ('r-empty', 'c1', 'p1', 'reconcile', 'succeeded',
         '{"item_count": 0}', current_timestamp - INTERVAL 2 DAY),
        ('r-old', 'c1', 'p1', 'reconcile', 'succeeded',
         '{"item_count": 2}', current_timestamp - INTERVAL 1 DAY),
        ('r-new', 'c1', 'p1', 'reconcile', 'succeeded',
         '{"item_count": 2}', current_timestamp)
        """
    )
    for run_id, item_id in (("r-old", "i-old"), ("r-new", "i-new")):
        database.execute(
            """
            INSERT INTO reconciliation_item (
                item_id, run_id, contract_id, period_id, source_kind,
                source_record_key, side, business_key, currency, amount
            ) VALUES (?, ?, 'c1', 'p1', 'baobei_order', ?, 'order', 'b',
                      'CNY', 1.0000)
            """,
            [item_id, run_id, item_id],
        )
        database.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                currency, expected_amount, actual_amount, matched_amount,
                difference_amount, status
            ) VALUES (?, ?, 'c1', 'p1', ?, 'CNY', 1, 1, 1, 0, 'balanced')
            """,
            [f"bal-{run_id}", run_id, run_id],
        )


def test_backfill_marks_empty_runs(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database)
        changed = backfill_empty_reconcile_runs(database)
        assert changed == 1
        status, error_code = database.fetchone_required(
            "SELECT status, error_code FROM run_log WHERE run_id = 'r-empty'"
        )
        assert status in {"skipped", "cancelled"}
        if status == "cancelled":
            assert error_code == "skipped_empty"


def test_archive_keeps_only_latest_succeeded(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database)
        backfill_empty_reconcile_runs(database)
        result = archive_stale_reconcile_details(database)
        assert result["stale_runs"] == 2  # empty + old
        assert result["kept_runs"] == 1
        assert result["archived_items"] == 1
        assert result["archived_balances"] == 1
        remaining = database.execute(
            "SELECT run_id FROM reconciliation_item ORDER BY run_id"
        ).fetchall()
        assert remaining == [("r-new",)]


def test_archived_details_are_still_retrievable(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database)
        archive_stale_reconcile_details(database)
        archived = database.execute(
            "SELECT item_id FROM archive.reconciliation_item ORDER BY item_id"
        ).fetchall()
        assert archived == [("i-old",)]


def test_locked_period_details_are_never_archived(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database, period_status="closed")
        result = archive_stale_reconcile_details(database)
        assert result["stale_runs"] == 0
        assert result["locked_runs_skipped"] == 2
        remaining = database.execute(
            "SELECT run_id FROM reconciliation_item ORDER BY run_id"
        ).fetchall()
        assert remaining == [("r-new",), ("r-old",)]


def test_hygiene_actions_are_recorded(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database)
        backfill_empty_reconcile_runs(database)
        archive_stale_reconcile_details(database)
        actions = [
            str(row[0])
            for row in database.execute(
                "SELECT action FROM maintenance_log ORDER BY action"
            ).fetchall()
        ]
        assert actions == [
            "archive_stale_reconcile_details",
            "backfill_empty_reconcile_runs",
        ]


def test_backfill_ignores_runs_without_an_item_count(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        _seed_runs(database)
        database.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status, metrics_json
            ) VALUES ('r-legacy', 'c1', 'p1', 'reconcile', 'succeeded', '{}')
            """
        )
        backfill_empty_reconcile_runs(database)
        status = database.fetchone_required(
            "SELECT status FROM run_log WHERE run_id = 'r-legacy'"
        )[0]
        assert status == "succeeded"


def test_counterfactual_schema_created(tmp_path: Path) -> None:
    with DuckDBMemory(tmp_path / "ledger.duckdb") as database:
        database.initialize()
        ensure_counterfactual_schema(database)
        tables = {
            str(row[0])
            for row in database.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'counterfactual'
                """
            ).fetchall()
        }
        assert "cf_reconciliation_item" in tables
        assert "cf_invariant_evaluation" in tables
