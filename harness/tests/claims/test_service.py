"""Tests for claims state machine and service layer."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness.claims.service import (
    InvalidClaimTransition,
    get_claim,
    list_claims,
    transition_to_closed,
    transition_to_packaged,
    transition_to_recovered,
    transition_to_response,
    transition_to_submitted,
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


def _seed_claim(db: DuckDBMemory, *, claim_id: str = "claim-1", status: str = "draft") -> None:
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
        f"""
        INSERT INTO claim (
            claim_id, contract_id, period_id, store_id,
            invariant_version_id, subject_kind, subject_key,
            reason_code, claimed_amount, currency, status
        ) VALUES ('{claim_id}', 'contract-1', 'period-1', 'store-1',
                  'inv-1:1.0.0', 'unresolved_balance', 'ub-1',
                  'amount_mismatch', 100.5000, 'CNY', '{status}')
        """
    )


class TestStateTransitions:
    def test_draft_to_packaged(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)
        result = transition_to_packaged(db, "claim-1", packet_sha256="abc123")
        assert result["status"] == "packaged"
        claim = get_claim(db, "claim-1")
        assert claim["status"] == "packaged"
        assert claim["packet_sha256"] == "abc123"
        db.close()

    def test_packaged_to_submitted(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="packaged")
        result = transition_to_submitted(
            db, "claim-1", operator_name="张三", external_ref="EXT-001"
        )
        assert result["status"] == "submitted"
        claim = get_claim(db, "claim-1")
        assert claim["operator_name"] == "张三"
        assert claim["external_ref"] == "EXT-001"
        assert claim["submitted_at"] is not None
        db.close()

    def test_submitted_to_accepted(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        result = transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("100.5000"),
        )
        assert result["status"] == "accepted"
        claim = get_claim(db, "claim-1")
        assert claim["accepted_amount"] == "100.5000"
        db.close()

    def test_submitted_to_partially_accepted(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        result = transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="partially_accepted",
            accepted_amount=Decimal("50.2500"),
            response_text="只接受一半",
        )
        assert result["status"] == "partially_accepted"
        db.close()

    def test_submitted_to_rejected(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        result = transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="rejected",
        )
        assert result["status"] == "rejected"
        claim = get_claim(db, "claim-1")
        assert claim["accepted_amount"] == "0.0000"
        db.close()

    def test_accepted_to_recovered(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="accepted")
        db.execute(
            "UPDATE claim SET accepted_amount = 100.5000 WHERE claim_id = 'claim-1'"
        )
        result = transition_to_recovered(
            db, "claim-1",
            operator_name="王五",
            recovered_amount=Decimal("100.5000"),
        )
        assert result["status"] == "recovered"
        claim = get_claim(db, "claim-1")
        assert claim["recovered_amount"] == "100.5000"
        db.close()

    def test_recovered_to_closed(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="recovered")
        db.execute(
            "UPDATE claim SET recovered_amount = 100.5000 WHERE claim_id = 'claim-1'"
        )
        result = transition_to_closed(db, "claim-1", actor="system")
        assert result["status"] == "closed"
        db.close()

    def test_rejected_to_closed(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="rejected")
        result = transition_to_closed(db, "claim-1")
        assert result["status"] == "closed"
        db.close()

    def test_full_happy_path(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)
        transition_to_packaged(db, "claim-1", packet_sha256="sha256hex")
        transition_to_submitted(db, "claim-1", operator_name="张三")
        transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("100.5000"),
        )
        transition_to_recovered(
            db, "claim-1",
            operator_name="王五",
            recovered_amount=Decimal("100.5000"),
        )
        transition_to_closed(db, "claim-1")
        claim = get_claim(db, "claim-1")
        assert claim["status"] == "closed"
        db.close()


class TestInvalidTransitions:
    def test_draft_cannot_skip_to_submitted(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)
        with pytest.raises(InvalidClaimTransition):
            transition_to_submitted(db, "claim-1", operator_name="张三")
        db.close()

    def test_submitted_cannot_go_back_to_packaged(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        with pytest.raises(InvalidClaimTransition):
            transition_to_packaged(db, "claim-1", packet_sha256="x")
        db.close()

    def test_closed_has_no_transitions(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="closed")
        with pytest.raises(InvalidClaimTransition):
            transition_to_submitted(db, "claim-1", operator_name="张三")
        db.close()


class TestAmountImmutability:
    def test_accepted_amount_cannot_be_overwritten(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("100.5000"),
        )
        db.execute("UPDATE claim SET status = 'submitted' WHERE claim_id = 'claim-1'")
        with pytest.raises(InvalidClaimTransition, match="accepted_amount 已设置"):
            transition_to_response(
                db, "claim-1",
                operator_name="李四",
                verdict="rejected",
            )
        db.close()

    def test_recovered_amount_cannot_be_overwritten(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="accepted")
        db.execute(
            "UPDATE claim SET accepted_amount = 100.5000 WHERE claim_id = 'claim-1'"
        )
        transition_to_recovered(
            db, "claim-1",
            operator_name="王五",
            recovered_amount=Decimal("100.5000"),
        )
        db.execute(
            "UPDATE claim SET status = 'accepted' WHERE claim_id = 'claim-1'"
        )
        with pytest.raises(InvalidClaimTransition, match="recovered_amount 已设置"):
            transition_to_recovered(
                db, "claim-1",
                operator_name="王五",
                recovered_amount=Decimal("50"),
            )
        db.close()


class TestEventLogging:
    def test_transitions_create_events(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)
        transition_to_packaged(db, "claim-1", packet_sha256="sha")
        transition_to_submitted(db, "claim-1", operator_name="张三")

        events = db.execute(
            """
            SELECT from_status, to_status, actor
            FROM claim_event
            WHERE claim_id = 'claim-1'
            ORDER BY created_at
            """
        ).fetchall()
        assert len(events) == 2
        assert events[0] == ("draft", "packaged", "system")
        assert events[1] == ("packaged", "submitted", "张三")
        db.close()


class TestListClaims:
    def test_list_and_filter(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, claim_id="claim-1", status="draft")
        _seed_claim(db, claim_id="claim-2", status="submitted")

        all_claims = list_claims(db)
        assert all_claims["total"] == 2

        draft_only = list_claims(db, status="draft")
        assert draft_only["total"] == 1
        assert draft_only["items"][0]["claim_id"] == "claim-1"
        db.close()

    def test_pagination(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        for i in range(5):
            _seed_claim(db, claim_id=f"claim-{i}", status="draft")

        page = list_claims(db, limit=2, offset=0)
        assert page["total"] == 5
        assert len(page["items"]) == 2
        assert page["hasMore"] is True

        page2 = list_claims(db, limit=2, offset=4)
        assert len(page2["items"]) == 1
        assert page2["hasMore"] is False
        db.close()


class TestFullLifecycle:
    """End-to-end lifecycle: draft → packaged → submitted → accepted → recovered → closed."""

    def test_happy_path_accepted_recovered(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)

        transition_to_packaged(db, "claim-1", packet_sha256="sha256_packet")
        assert get_claim(db, "claim-1")["status"] == "packaged"

        transition_to_submitted(db, "claim-1", operator_name="张三", external_ref="EXT-001")
        claim = get_claim(db, "claim-1")
        assert claim["status"] == "submitted"
        assert claim["submitted_at"] is not None

        transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("100.5000"),
        )
        claim = get_claim(db, "claim-1")
        assert claim["status"] == "accepted"
        assert claim["accepted_amount"] == "100.5000"

        transition_to_recovered(
            db, "claim-1",
            operator_name="王五",
            recovered_amount=Decimal("100.5000"),
        )
        claim = get_claim(db, "claim-1")
        assert claim["status"] == "recovered"
        assert claim["recovered_amount"] == "100.5000"

        transition_to_closed(db, "claim-1")
        assert get_claim(db, "claim-1")["status"] == "closed"

        events = db.execute(
            "SELECT count(*) FROM claim_event WHERE claim_id = 'claim-1'"
        ).fetchone()
        assert events[0] == 5
        db.close()

    def test_rejected_path(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db)

        transition_to_packaged(db, "claim-1", packet_sha256="sha")
        transition_to_submitted(db, "claim-1", operator_name="张三")
        transition_to_response(db, "claim-1", operator_name="李四", verdict="rejected")
        assert get_claim(db, "claim-1")["status"] == "rejected"
        accepted = Decimal(get_claim(db, "claim-1")["accepted_amount"])
        assert accepted == Decimal("0")

        transition_to_closed(db, "claim-1")
        assert get_claim(db, "claim-1")["status"] == "closed"
        db.close()

    def test_amount_immutability_through_lifecycle(self, tmp_path: Path) -> None:
        """Once set, claimed_amount, accepted_amount, recovered_amount are immutable."""
        db = _setup_db(tmp_path)
        _seed_claim(db)

        original = get_claim(db, "claim-1")
        assert original["claimed_amount"] == "100.5000"

        transition_to_packaged(db, "claim-1", packet_sha256="sha")
        transition_to_submitted(db, "claim-1", operator_name="张三")
        transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("80.0000"),
        )

        claim = get_claim(db, "claim-1")
        assert claim["claimed_amount"] == "100.5000"
        assert claim["accepted_amount"] == "80.0000"

        transition_to_recovered(
            db, "claim-1",
            operator_name="王五",
            recovered_amount=Decimal("80.0000"),
        )

        claim = get_claim(db, "claim-1")
        assert claim["claimed_amount"] == "100.5000"
        assert claim["accepted_amount"] == "80.0000"
        assert claim["recovered_amount"] == "80.0000"
        db.close()

    def test_external_verdict_recorded_on_response(self, tmp_path: Path) -> None:
        """Transitioning to accepted/rejected should record an external_verdict."""
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        transition_to_response(
            db, "claim-1",
            operator_name="李四",
            verdict="accepted",
            accepted_amount=Decimal("100.5000"),
        )
        row = db.execute(
            "SELECT verdict, accepted_amount FROM external_verdict WHERE claim_id = 'claim-1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "accepted"
        db.close()


class TestValidation:
    def test_empty_operator_rejected(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="packaged")
        with pytest.raises(ValueError, match="操作人不能为空"):
            transition_to_submitted(db, "claim-1", operator_name="")
        db.close()

    def test_zero_recovery_rejected(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="accepted")
        db.execute(
            "UPDATE claim SET accepted_amount = 100.5000 WHERE claim_id = 'claim-1'"
        )
        with pytest.raises(ValueError, match="回收金额必须大于零"):
            transition_to_recovered(
                db, "claim-1",
                operator_name="王五",
                recovered_amount=Decimal("0"),
            )
        db.close()

    def test_invalid_verdict_rejected(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim(db, status="submitted")
        with pytest.raises(ValueError, match="verdict"):
            transition_to_response(
                db, "claim-1",
                operator_name="李四",
                verdict="maybe",
            )
        db.close()

    def test_not_found_raises_lookup_error(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        with pytest.raises(LookupError):
            get_claim(db, "nonexistent")
        db.close()
