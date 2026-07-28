"""Tests for invariant claim stats aggregation."""

from __future__ import annotations

from pathlib import Path

from commerce_harness.claims.value import refresh_invariant_claim_stats
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _setup_db(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    db = DuckDBMemory(tmp_path / "workbench" / "harness.duckdb")
    db.initialize()
    return db


def _seed_invariant(db: DuckDBMemory) -> None:
    db.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES ('contract-1', 'taobao', 'ent-1', 'store-1', 'taobao',
                  1, DATE '2026-03-01', 'active', '{}')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        ) VALUES ('period-1', 'contract-1', 'store-1',
                  DATE '2026-03-01', DATE '2026-03-31', 'open')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO invariant_definition (
            invariant_id, domain, family, title, definition_json, origin
        ) VALUES ('inv-1', 'settlement', 'equality', '差额',
                  '{"family":"equality"}', 'builtin')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO invariant_version (
            invariant_version_id, invariant_id, semver, status
        ) VALUES ('inv-1:1.0.0', 'inv-1', '1.0.0', 'active')
        ON CONFLICT DO NOTHING
        """
    )


def _insert_claim(
    db: DuckDBMemory,
    claim_id: str,
    status: str,
    claimed: str = "100.0000",
    accepted: str | None = None,
    recovered: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO claim (
            claim_id, contract_id, period_id, store_id,
            invariant_version_id, subject_kind, subject_key,
            reason_code, claimed_amount, currency, status,
            accepted_amount, recovered_amount
        ) VALUES (?, 'contract-1', 'period-1', 'store-1',
                  'inv-1:1.0.0', 'unresolved_balance', ?,
                  'amount_mismatch', ?, 'CNY', ?, ?, ?)
        """,
        [claim_id, f"ub-{claim_id}", claimed, status, accepted, recovered],
    )


class TestRefreshStats:
    def test_aggregates_correctly(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_invariant(db)
        _insert_claim(db, "c1", "accepted", accepted="100.0000")
        _insert_claim(db, "c2", "partially_accepted", claimed="200.0000", accepted="80.0000")
        _insert_claim(db, "c3", "rejected")
        _insert_claim(db, "c4", "recovered", claimed="50.0000", recovered="50.0000")

        stats = refresh_invariant_claim_stats(db)

        assert len(stats) == 1
        s = stats[0]
        assert s["invariant_version_id"] == "inv-1:1.0.0"
        assert s["total_claims"] == 4
        assert s["accepted_count"] == 1
        assert s["partially_accepted_count"] == 1
        assert s["rejected_count"] == 1
        assert s["recovered_count"] == 1
        assert s["acceptance_rate"] == 0.5
        assert s["recovery_rate"] == 0.25
        db.close()

    def test_empty_table_returns_empty(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_invariant(db)
        stats = refresh_invariant_claim_stats(db)
        assert stats == []
        db.close()

    def test_idempotent_refresh(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_invariant(db)
        _insert_claim(db, "c1", "accepted", accepted="100.0000")

        s1 = refresh_invariant_claim_stats(db)
        s2 = refresh_invariant_claim_stats(db)
        assert s1 == s2
        db.close()
