"""Tests for corpus persistence (fingerprint upsert, case insert, external verdict)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness.config import load_config
from commerce_harness.corpus.external import record_external_verdict
from commerce_harness.corpus.persist import (
    insert_case,
    record_review_as_case,
    upsert_fingerprint,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _setup_db(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    db = DuckDBMemory(tmp_path / "workbench" / "harness.duckdb")
    db.initialize()
    return db


def _seed_claim_prereqs(db: DuckDBMemory) -> None:
    """Seed contract, period, invariant, and claim so FK constraints pass."""
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
        ) VALUES ('inv-1', 'settlement', 'equality', '订单平台差额',
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
        """
        INSERT INTO claim (
            claim_id, contract_id, period_id, store_id,
            invariant_version_id, subject_kind, subject_key,
            reason_code, claimed_amount, currency, status
        ) VALUES ('claim-1', 'contract-1', 'period-1', 'store-1',
                  'inv-1:1.0.0', 'unresolved_balance', 'ub-1',
                  'amount_mismatch', 100.5000, 'CNY', 'submitted')
        ON CONFLICT DO NOTHING
        """
    )


class TestUpsertFingerprint:
    def test_insert_new_fingerprint(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        upsert_fingerprint(
            db,
            "fp_abc123",
            "settlement",
            {"source_kinds": ["orders"]},
            period_id="period-1",
            store_id="store-1",
        )
        row = db.execute(
            "SELECT occurrence_count, distinct_periods, distinct_stores, domain "
            "FROM situation_fingerprint WHERE fingerprint_id = 'fp_abc123'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == 1
        assert row[2] == 1
        assert row[3] == "settlement"
        db.close()

    def test_upsert_increments_count(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        upsert_fingerprint(db, "fp_inc", "settlement", {"k": "v"})
        upsert_fingerprint(db, "fp_inc", "settlement", {"k": "v"})
        upsert_fingerprint(db, "fp_inc", "settlement", {"k": "v"})
        row = db.execute(
            "SELECT occurrence_count FROM situation_fingerprint WHERE fingerprint_id = 'fp_inc'"
        ).fetchone()
        assert row is not None
        assert row[0] == 3
        db.close()

    def test_upsert_without_period_or_store(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        upsert_fingerprint(db, "fp_bare", "recon", {"k": "v"})
        row = db.execute(
            "SELECT distinct_periods, distinct_stores FROM situation_fingerprint "
            "WHERE fingerprint_id = 'fp_bare'"
        ).fetchone()
        assert row == (0, 0)
        db.close()


class TestInsertCase:
    def test_insert_minimal_case(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        upsert_fingerprint(db, "fp_case1", "settlement", {"k": "v"})
        case_id = insert_case(db, {
            "fingerprint_id": "fp_case1",
            "domain": "settlement",
            "subject_kind": "unresolved_balance",
            "subject_key": "ub-1",
            "disposition_kind": "rule_candidate",
            "rationale": "金额一致",
            "decided_by": "human",
            "decided_role": "accountant",
            "evidence_binding_digest": "digest_abc",
        })
        assert case_id.startswith("case_")
        row = db.execute(
            "SELECT domain, subject_kind FROM adjudication_case WHERE case_id = ?",
            [case_id],
        ).fetchone()
        assert row == ("settlement", "unresolved_balance")
        db.close()

    def test_missing_fields_raises(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        with pytest.raises(ValueError, match="missing required fields"):
            insert_case(db, {"fingerprint_id": "fp_x", "domain": "settlement"})
        db.close()


class TestRecordReviewAsCase:
    def test_creates_fingerprint_and_case(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        case_id = record_review_as_case(
            db,
            domain="settlement",
            subject_kind="unresolved_balance",
            subject_key="ub-99",
            disposition_kind="explained",
            rationale="手续费扣减",
            decided_by="张三",
            decided_role="reviewer",
            evidence_binding_digest="digest_xyz",
            source_kinds=["orders", "alipay_ledger"],
            amounts=["100.00", "-5.00"],
            business_description="技术服务费",
            period_id="p-202603",
            store_id="store-A",
        )
        assert case_id.startswith("case_")

        fp_row = db.execute("SELECT count(*) FROM situation_fingerprint").fetchone()
        assert fp_row is not None and fp_row[0] >= 1

        case_row = db.execute(
            "SELECT subject_key, decided_by FROM adjudication_case WHERE case_id = ?",
            [case_id],
        ).fetchone()
        assert case_row == ("ub-99", "张三")
        db.close()


class TestExternalVerdict:
    def test_record_accepted_verdict(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_prereqs(db)
        vid = record_external_verdict(
            db,
            claim_id="claim-1",
            verdict="accepted",
            source="operator:李四",
            accepted_amount=Decimal("100.5000"),
        )
        assert vid.startswith("ev_")
        row = db.execute(
            "SELECT verdict, accepted_amount, source FROM external_verdict WHERE verdict_id = ?",
            [vid],
        ).fetchone()
        assert row[0] == "accepted"
        assert row[1] == Decimal("100.5000")
        assert row[2] == "operator:李四"
        db.close()

    def test_record_rejected_verdict(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_prereqs(db)
        vid = record_external_verdict(
            db,
            claim_id="claim-1",
            verdict="rejected",
            source="platform:taobao",
            note="证据不足",
        )
        row = db.execute(
            "SELECT verdict, note FROM external_verdict WHERE verdict_id = ?",
            [vid],
        ).fetchone()
        assert row == ("rejected", "证据不足")
        db.close()

    def test_invalid_verdict_raises(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_prereqs(db)
        with pytest.raises(ValueError, match="verdict must be one of"):
            record_external_verdict(
                db,
                claim_id="claim-1",
                verdict="maybe",
                source="test",
            )
        db.close()
