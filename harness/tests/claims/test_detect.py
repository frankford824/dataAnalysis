"""Tests for claim detection from unresolved balances and invariant evaluations."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from commerce_harness.claims.detect import (
    CLAIMABLE_REASON_CODES,
    detect_claims,
)
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _setup_db(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    db = DuckDBMemory(tmp_path / "workbench" / "harness.duckdb")
    db.initialize()
    return db


def _seed_unresolved(
    db: DuckDBMemory,
    *,
    unresolved_id: str = "ub-1",
    reason_code: str = "amount_mismatch",
    amount: str = "50.0000",
    status: str = "open",
) -> None:
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
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status
        ) VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded')
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
    db.execute(
        f"""
        INSERT INTO reconciliation_balance (
            balance_id, run_id, contract_id, period_id, balance_key,
            expected_amount, actual_amount, matched_amount, difference_amount,
            status
        ) VALUES (
            'balance-{unresolved_id}', 'run-1', 'contract-1', 'period-1',
            'key-{unresolved_id}', 100, 50, 50, {amount}, 'unresolved'
        )
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        f"""
        INSERT INTO unresolved_balance (
            unresolved_id, balance_id, reason_code, amount, status
        ) VALUES (
            '{unresolved_id}', 'balance-{unresolved_id}',
            '{reason_code}', {amount}, '{status}'
        )
        """
    )


class TestDetectClaims:
    def test_creates_draft_for_claimable_unresolved(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db)
        claims = detect_claims(db)
        assert len(claims) == 1
        assert claims[0]["status"] == "draft"
        assert claims[0]["reason_code"] == "amount_mismatch"
        assert claims[0]["claimed_amount"] == "50.0000"
        db.close()

    def test_skips_non_claimable_reason_codes(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db, reason_code="ambiguous_bridge")
        assert "ambiguous_bridge" not in CLAIMABLE_REASON_CODES
        claims = detect_claims(db)
        assert len(claims) == 0
        db.close()

    def test_skips_below_materiality_floor(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db, amount="0.001")
        claims = detect_claims(db)
        assert len(claims) == 0
        db.close()

    def test_skips_already_claimed(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db)
        first = detect_claims(db)
        assert len(first) == 1
        second = detect_claims(db)
        assert len(second) == 0
        db.close()

    def test_skips_explained_unresolved(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db, status="explained")
        claims = detect_claims(db)
        assert len(claims) == 0
        db.close()

    def test_filters_by_period(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db)
        claims = detect_claims(db, period_id="nonexistent")
        assert len(claims) == 0
        claims = detect_claims(db, period_id="period-1")
        assert len(claims) == 1
        db.close()

    def test_filters_by_store(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db)
        claims = detect_claims(db, store_id="nonexistent")
        assert len(claims) == 0
        claims = detect_claims(db, store_id="store-1")
        assert len(claims) == 1
        db.close()

    def test_all_claimable_reason_codes_produce_claims(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        for i, code in enumerate(sorted(CLAIMABLE_REASON_CODES)):
            _seed_unresolved(db, unresolved_id=f"ub-{i}", reason_code=code, amount="10.0000")
        claims = detect_claims(db)
        assert len(claims) == len(CLAIMABLE_REASON_CODES)
        db.close()

    def test_custom_materiality_floor(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_unresolved(db, amount="5.0000")
        claims_strict = detect_claims(db, materiality_floor=Decimal("10"))
        assert len(claims_strict) == 0
        claims_relaxed = detect_claims(db, materiality_floor=Decimal("1"))
        assert len(claims_relaxed) == 1
        db.close()
