from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from commerce_harness.memory import REQUIRED_TABLES, SCHEMA_VERSION, DuckDBMemory
from commerce_harness.snapshot import (
    BytesReader,
    NormalizedArtifactSpec,
    ParquetArtifactStore,
    SnapshotStore,
)


def test_initialize_creates_complete_versioned_schema() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        assert memory.table_names() >= REQUIRED_TABLES
        assert memory.execute(
            "SELECT version FROM harness_schema_version"
        ).fetchone() == (SCHEMA_VERSION,)

        amount_type = memory.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = 'reconciliation_item' AND column_name = 'amount'
            """
        ).fetchone()
        assert amount_type == ("DECIMAL(38,4)",)


def test_file_database_initialization_is_reused_within_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "metadata.duckdb"
    with DuckDBMemory(database_path) as memory:
        memory.initialize()

    def fail_if_reinitialized(_memory: DuckDBMemory) -> None:
        raise AssertionError("schema initialization should be reused")

    monkeypatch.setattr(
        DuckDBMemory,
        "_initialize_database",
        fail_if_reinitialized,
    )
    with DuckDBMemory(database_path) as memory:
        memory.initialize()
        assert "source_snapshot" in memory.table_names()


def test_file_database_retries_transient_duckdb_attach_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = duckdb.connect
    attempts = 0

    def flaky_connect(database: str) -> duckdb.DuckDBPyConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise duckdb.BinderException(
                'Binder Error: Unique file handle conflict: Cannot attach "ledger"'
            )
        return real_connect(database)

    monkeypatch.setattr(duckdb, "connect", flaky_connect)

    with DuckDBMemory(tmp_path / "ledger.duckdb") as memory:
        assert memory.execute("SELECT 1").fetchone() == (1,)

    assert attempts == 2


def test_registers_snapshot_and_parquet_metadata(tmp_path: Path) -> None:
    snapshot = SnapshotStore(tmp_path / "snapshots").capture(
        BytesReader(b"order_id,amount\nA-1,1.2300\n"),
        original_name="orders.csv",
        media_type="text/csv",
    )
    table = pa.table(
        {
            "order_id": ["A-1"],
            "amount": pa.array([Decimal("1.2300")], type=pa.decimal128(38, 4)),
        }
    )
    artifact = ParquetArtifactStore(tmp_path / "normalized").write_table(
        table,
        spec=NormalizedArtifactSpec(
            dataset_kind="order",
            schema_version="v1",
            source_snapshot_sha256=snapshot.content_sha256,
        ),
    )

    with DuckDBMemory(tmp_path / "metadata.duckdb") as memory:
        memory.initialize()
        with memory.transaction():
            memory.register_snapshot(snapshot)
            memory.register_artifact(artifact, source_snapshot_id=snapshot.snapshot_id)
        assert memory.execute("SELECT count(*) FROM source_snapshot").fetchone() == (1,)
        row = memory.execute(
            "SELECT dataset_kind, row_count FROM normalized_artifact"
        ).fetchone()
        assert row == ("order", 1)


def test_transaction_rolls_back_invalid_or_partial_metadata() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        with pytest.raises(RuntimeError), memory.transaction() as connection:
            connection.execute(
                """
                    INSERT INTO rule_definition(rule_id, logical_key, rule_kind, title)
                    VALUES ('rule-1', 'logical-rule', 'classification', 'Synthetic rule')
                    """
            )
            raise RuntimeError("synthetic failure")
        assert memory.execute("SELECT count(*) FROM rule_definition").fetchone() == (0,)


def test_schema_rejects_invalid_period_state() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract(
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('c1', 'contract', 'e1', 's1', 'taobao', 1, DATE '2026-01-01',
                    'active', '{}')
            """
        )
        with pytest.raises(duckdb.ConstraintException):
            memory.execute(
                """
                INSERT INTO accounting_period(
                    period_id, contract_id, store_id, period_start, period_end, status
                )
                VALUES ('p1', 'c1', 's1', DATE '2026-02-01', DATE '2026-02-28',
                        'silently_overwritten')
                """
            )


def test_checklist_result_allows_same_requirement_for_multiple_periods() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract(
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('c1', 'contract', 'e1', 's1', 'taobao', 1, DATE '2026-01-01',
                    'active', '{}')
            """
        )
        for period_id, start, end in (
            ("p1", "2026-02-01", "2026-02-28"),
            ("p2", "2026-03-01", "2026-03-31"),
        ):
            memory.execute(
                """
                INSERT INTO accounting_period(
                    period_id, contract_id, store_id, period_start, period_end, status
                )
                VALUES (?, 'c1', 's1', ?, ?, 'open')
                """,
                [period_id, start, end],
            )
        memory.execute(
            """
            INSERT INTO run_log(run_id, contract_id, run_kind, status)
            VALUES ('run1', 'c1', 'freeze', 'succeeded')
            """
        )
        memory.execute(
            """
            INSERT INTO checklist_requirement(
                requirement_id, contract_id, source_kind, store_scope,
                effective_from, expected_frequency
            )
            VALUES ('req1', 'c1', 'orders', 's1', DATE '2026-01-01', 'monthly')
            """
        )
        for result_id, period_id in (("r1", "p1"), ("r2", "p2")):
            memory.execute(
                """
                INSERT INTO checklist_result(
                    result_id, run_id, period_id, requirement_id, status
                )
                VALUES (?, 'run1', ?, 'req1', 'present')
                """,
                [result_id, period_id],
            )
        assert memory.execute(
            "SELECT count(*) FROM checklist_result"
        ).fetchone() == (2,)


def test_schema_v10_uses_immutable_revision_state_and_is_idempotent() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract(
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('c1', 'contract', 'e1', 's1', 'taobao', 1,
                    DATE '2026-01-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period(
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('p1', 'c1', 's1', DATE '2026-02-01',
                    DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO source_snapshot(
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                captured_at, manifest_json
            )
            VALUES ('snp1', repeat('a', 64), 1, '/snapshot', 'memory://snapshot',
                    current_timestamp, '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO input_revision(
                revision_id, contract_id, period_id, source_kind,
                logical_input_key, revision_no, snapshot_id, status
            )
            VALUES ('rev1', 'c1', 'p1', 'order', 'order:2602', 1,
                    'snp1', 'candidate')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log(run_id, contract_id, period_id, run_kind, status)
            VALUES ('run1', 'c1', 'p1', 'freeze', 'succeeded')
            """
        )
        memory.execute(
            """
            INSERT INTO checklist_requirement(
                requirement_id, contract_id, source_kind, store_scope,
                effective_from, expected_frequency
            )
            VALUES ('req1', 'c1', 'orders', 's1', DATE '2026-01-01', 'monthly')
            """
        )
        memory.execute(
            """
            INSERT INTO checklist_result(
                result_id, run_id, period_id, requirement_id, status, revision_id
            )
            VALUES ('result1', 'run1', 'p1', 'req1', 'present', 'rev1')
            """
        )
        memory.execute("DELETE FROM harness_schema_version")
        memory.execute("INSERT INTO harness_schema_version(version) VALUES (7)")

        memory.initialize()
        memory.initialize()
        memory.execute(
            """
            UPDATE input_revision_state
            SET status = 'current'
            WHERE revision_id = 'rev1'
            """
        )

        assert memory.execute(
            """
            SELECT state.status
            FROM input_revision revision
            JOIN input_revision_state state USING (revision_id)
            WHERE revision.revision_id = 'rev1'
            """
        ).fetchone() == ("current",)
        assert memory.execute(
            "SELECT count(*) FROM checklist_result WHERE revision_id = 'rev1'"
        ).fetchone() == (1,)
