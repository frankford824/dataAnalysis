"""Claim child tables keep soft references, and they are verified on open."""

from __future__ import annotations

from pathlib import Path

import pytest

from commerce_harness.memory import database as database_module
from commerce_harness.memory.database import DuckDBMemory

_LEGACY_CLAIM_EVENT = """
CREATE TABLE claim_event (
    event_id VARCHAR PRIMARY KEY,
    claim_id VARCHAR NOT NULL REFERENCES claim(claim_id),
    from_status VARCHAR NOT NULL,
    to_status VARCHAR NOT NULL,
    actor VARCHAR NOT NULL,
    response_text VARCHAR,
    payload_json JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
)
"""


def _foreign_keys(database: DuckDBMemory, table: str) -> list[tuple]:
    return database.execute(
        """
        SELECT table_name FROM duckdb_constraints()
        WHERE constraint_type = 'FOREIGN KEY' AND table_name = ?
        """,
        [table],
    ).fetchall()


def _reopen(path: Path) -> DuckDBMemory:
    database_module._INITIALIZED_DATABASES.clear()
    database = DuckDBMemory(path)
    database.initialize()
    return database


def test_legacy_claim_foreign_key_is_rebuilt_away(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    with DuckDBMemory(path) as database:
        database.initialize()
        database.execute("DROP TABLE claim_event")
        database.execute(_LEGACY_CLAIM_EVENT)
        database.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            ) VALUES ('c1', 'c1', 'e1', 's1', 'taobao', 1, DATE '2026-01-01',
                      'active', '{}')
            """
        )
        database.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            ) VALUES ('p1', 'c1', 's1', DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        database.execute(
            """
            INSERT INTO invariant_definition (
                invariant_id, domain, family, title, definition_json, origin
            ) VALUES ('inv-1', 'settlement', 'equality', '差额', '{}', 'builtin')
            """
        )
        database.execute(
            """
            INSERT INTO invariant_version (
                invariant_version_id, invariant_id, semver, status
            ) VALUES ('inv-1:1.0.0', 'inv-1', '1.0.0', 'active')
            """
        )
        database.execute(
            """
            INSERT INTO claim (
                claim_id, contract_id, period_id, store_id, invariant_version_id,
                subject_kind, subject_key, reason_code, claimed_amount, currency,
                status
            ) VALUES ('claim-1', 'c1', 'p1', 's1', 'inv-1:1.0.0',
                      'unresolved_balance', 'ub-1', 'amount_mismatch',
                      100, 'CNY', 'draft')
            """
        )
        database.execute(
            """
            INSERT INTO claim_event (
                event_id, claim_id, from_status, to_status, actor, payload_json
            ) VALUES ('e1', 'claim-1', 'draft', 'packaged', 'alice', '{}')
            """
        )
        assert _foreign_keys(database, "claim_event") != []

    database = _reopen(path)
    assert _foreign_keys(database, "claim_event") == []
    assert database.execute("SELECT count(*) FROM claim_event").fetchone()[0] == 1
    database.execute("UPDATE claim SET status = 'packaged' WHERE claim_id = 'claim-1'")
    database.close()


def test_dangling_claim_event_is_rejected_on_open(tmp_path: Path) -> None:
    path = tmp_path / "orphan.duckdb"
    with DuckDBMemory(path) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO claim_event (
                event_id, claim_id, from_status, to_status, actor, payload_json
            ) VALUES ('e-orphan', 'claim-missing', 'draft', 'packaged', 'a', '{}')
            """
        )

    with pytest.raises(RuntimeError, match="dangling claim references"):
        _reopen(path)
    database_module._INITIALIZED_DATABASES.clear()
