"""Tests for phase 3 period-close service."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness.kernel.period import InvalidPeriodTransition, PeriodLockedError
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.period_service import (
    effective_period_status,
    finalize_period,
    get_period_detail,
    period_is_locked,
    post_adjustment,
    preclose_period,
)
from commerce_harness.phase_a import reconcile_period
from commerce_harness.workbench import WorkbenchPaths


def _seed(database: DuckDBMemory, *, status: str = "open") -> str:
    """Insert minimal scaffolding and return the period_id."""
    database.initialize()
    database.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id,
            platform_code, contract_version, effective_from, status,
            definition_json
        )
        VALUES (
            'contract-pc', 'logical-pc', 'enterprise-pc', 'store-pc',
            'taobao', 1, DATE '2026-02-01', 'active', '{}'
        )
        """
    )
    database.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id,
            period_start, period_end, status
        )
        VALUES (
            'period-pc', 'contract-pc', 'store-pc',
            DATE '2026-02-01', DATE '2026-02-28', ?
        )
        """,
        [status],
    )
    return "period-pc"


# ------------------------------------------------------------------ #
#  open → preclose → finalize  full lifecycle
# ------------------------------------------------------------------ #


def test_open_preclose_finalize_lifecycle() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db)

        result = preclose_period(db, pid, "张三")
        assert result["status"] == "preclosed"
        assert effective_period_status(db, pid) == "preclosed"

        result = finalize_period(db, pid, "李四")
        assert result["status"] == "closed"
        assert result["closed_at"] is not None
        assert effective_period_status(db, pid) == "closed"

        detail = get_period_detail(db, pid)
        assert detail["status"] == "closed"
        assert detail["closed_at"] is not None
        assert detail["closed_by"] == "李四"

        # Close state is persisted beside the period, not in it, because
        # DuckDB refuses UPDATEs to FK-referenced parent rows.
        state = db.fetchone_required(
            """
            SELECT status, closed_at, closed_by
            FROM accounting_period_state WHERE period_id = ?
            """,
            [pid],
        )
        assert state[0] == "closed"
        assert state[1] is not None
        assert state[2] == "李四"


# ------------------------------------------------------------------ #
#  Cannot preclose a non-open period
# ------------------------------------------------------------------ #


def test_preclose_rejects_non_open() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="preclosed")
        with pytest.raises(InvalidPeriodTransition, match="preclosed"):
            preclose_period(db, pid, "操作员")


# ------------------------------------------------------------------ #
#  Cannot finalize a non-preclosed period
# ------------------------------------------------------------------ #


def test_finalize_rejects_non_preclosed() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="open")
        with pytest.raises(InvalidPeriodTransition, match="open"):
            finalize_period(db, pid, "操作员")


# ------------------------------------------------------------------ #
#  Cannot reconcile after close (gate in phase_a)
# ------------------------------------------------------------------ #


def test_open_period_is_not_locked() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="open")
        assert period_is_locked(db, pid) is False
        assert effective_period_status(db, pid) == "open"


def test_period_becomes_locked_after_preclose() -> None:
    """The gate reconcile_period consults must observe the transition."""
    with DuckDBMemory() as db:
        pid = _seed(db, status="open")
        assert period_is_locked(db, pid) is False

        preclose_period(db, pid, "操作员")

        assert effective_period_status(db, pid) == "preclosed"
        assert period_is_locked(db, pid) is True


def test_period_stays_locked_through_finalize_and_adjustment() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="open")
        preclose_period(db, pid, "操作员")
        finalize_period(db, pid, "操作员")
        assert effective_period_status(db, pid) == "closed"
        assert period_is_locked(db, pid) is True

        post_adjustment(
            db, pid, "操作员",
            amount=Decimal("1.00"),
            reason="补录",
        )
        assert effective_period_status(db, pid) == "restated"
        assert period_is_locked(db, pid) is True


def test_reconcile_period_refuses_locked_period(tmp_path: Path) -> None:
    """End-to-end: the real reconcile_period aborts on a closed period."""
    database_path = tmp_path / "ledger.duckdb"
    with DuckDBMemory(database_path) as db:
        pid = _seed(db, status="open")
        preclose_period(db, pid, "操作员")
        finalize_period(db, pid, "操作员")
        assert effective_period_status(db, pid) == "closed"

    workbench = WorkbenchPaths(
        root=tmp_path,
        database=database_path,
        snapshots=tmp_path / "snapshots",
        normalized=tmp_path / "normalized",
        reports=tmp_path / "reports",
        llm_logs=tmp_path / "llm_logs",
        locks=tmp_path / "locks",
    )
    with pytest.raises(PeriodLockedError, match="不允许再执行核对"):
        reconcile_period(workbench, period_token="2602")

    # The aborted attempt must not leave a reconcile run behind.
    with DuckDBMemory(database_path) as db:
        runs = db.execute(
            "SELECT count(*) FROM run_log WHERE run_kind = 'reconcile'"
        ).fetchone()
        assert runs is not None and runs[0] == 0


# ------------------------------------------------------------------ #
#  Adjustment only after finalize
# ------------------------------------------------------------------ #


def test_adjustment_only_after_finalize() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="open")
        with pytest.raises(InvalidPeriodTransition, match="open"):
            post_adjustment(
                db, pid, "操作员",
                amount=Decimal("100.00"),
                reason="补差",
            )

    with DuckDBMemory() as db:
        pid = _seed(db, status="preclosed")
        with pytest.raises(InvalidPeriodTransition, match="preclosed"):
            post_adjustment(
                db, pid, "操作员",
                amount=Decimal("100.00"),
                reason="补差",
            )


def test_adjustment_succeeds_on_closed_period() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="closed")
        result = post_adjustment(
            db, pid, "王五",
            amount=Decimal("50.25"),
            reason="运费差异补录",
        )
        assert result["status"] == "restated"
        assert result["amount"] == "50.25"
        assert result["adjustment_id"].startswith("adj_")

        detail = get_period_detail(db, pid)
        assert detail["status"] == "restated"

        adj = db.fetchone_required(
            "SELECT amount, reason, approved_by, status FROM adjustment_entry WHERE period_id = ?",
            [pid],
        )
        assert adj[0] == Decimal("50.2500")
        assert adj[1] == "运费差异补录"
        assert adj[2] == "王五"
        assert adj[3] == "posted"


def test_adjustment_with_unresolved_id() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="closed")

        db.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES ('run-adj', 'contract-pc', 'period-pc', 'reconcile', 'succeeded')
            """
        )
        db.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount,
                difference_amount, status
            )
            VALUES (
                'bal-1', 'run-adj', 'contract-pc', 'period-pc', 'key-1',
                100.0000, 90.0000, 90.0000, 10.0000, 'unresolved'
            )
            """
        )
        db.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            )
            VALUES ('ub-1', 'bal-1', 'freight_diff', 10.0000, 'open')
            """
        )

        result = post_adjustment(
            db, pid, "赵六",
            amount=Decimal("-10.00"),
            reason="运费差异核销",
            unresolved_id="ub-1",
        )
        assert result["adjustment_id"]

        adj = db.fetchone_required(
            "SELECT unresolved_id FROM adjustment_entry WHERE adjustment_id = ?",
            [result["adjustment_id"]],
        )
        assert adj[0] == "ub-1"


# ------------------------------------------------------------------ #
#  operator_name required
# ------------------------------------------------------------------ #


def test_operator_name_required_preclose() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db)
        with pytest.raises(ValueError, match="operator_name"):
            preclose_period(db, pid, "")
        with pytest.raises(ValueError, match="operator_name"):
            preclose_period(db, pid, "   ")


def test_operator_name_required_finalize() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="preclosed")
        with pytest.raises(ValueError, match="operator_name"):
            finalize_period(db, pid, "")


def test_operator_name_required_adjustment() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="closed")
        with pytest.raises(ValueError, match="operator_name"):
            post_adjustment(
                db, pid, "",
                amount=Decimal("1.00"),
                reason="测试",
            )


# ------------------------------------------------------------------ #
#  Zero-amount adjustment rejected
# ------------------------------------------------------------------ #


def test_zero_amount_adjustment_rejected() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="closed")
        with pytest.raises(ValueError, match="不能为零"):
            post_adjustment(
                db, pid, "操作员",
                amount=Decimal("0"),
                reason="不该通过",
            )


# ------------------------------------------------------------------ #
#  get_period_detail returns status + net adjustments
# ------------------------------------------------------------------ #


def test_get_period_detail() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db, status="closed")
        post_adjustment(
            db, pid, "调整员",
            amount=Decimal("100.00"),
            reason="补录",
        )
        post_adjustment(
            db, pid, "调整员",
            amount=Decimal("-30.00"),
            reason="冲回",
        )
        detail = get_period_detail(db, pid)
        assert detail["status"] == "restated"
        assert Decimal(detail["net_adjustments"]) == Decimal("70.0000")
        assert detail["period_id"] == pid
        assert detail["store_id"] == "store-pc"


# ------------------------------------------------------------------ #
#  Maintenance log audit trail
# ------------------------------------------------------------------ #


def test_maintenance_log_recorded() -> None:
    with DuckDBMemory() as db:
        pid = _seed(db)
        preclose_period(db, pid, "审计员")
        count = db.execute(
            "SELECT count(*) FROM maintenance_log WHERE action = 'period_preclose'"
        ).fetchone()
        assert count is not None and count[0] == 1

        finalize_period(db, pid, "审计员")
        count = db.execute(
            "SELECT count(*) FROM maintenance_log WHERE action = 'period_finalize'"
        ).fetchone()
        assert count is not None and count[0] == 1

        post_adjustment(
            db, pid, "审计员",
            amount=Decimal("5.00"),
            reason="审计调整",
        )
        count = db.execute(
            "SELECT count(*) FROM maintenance_log WHERE action = 'period_adjustment'"
        ).fetchone()
        assert count is not None and count[0] == 1


# ------------------------------------------------------------------ #
#  Period not found
# ------------------------------------------------------------------ #


def test_nonexistent_period_raises() -> None:
    with DuckDBMemory() as db:
        db.initialize()
        with pytest.raises(LookupError):
            preclose_period(db, "nonexistent-period", "操作员")
        with pytest.raises(LookupError):
            finalize_period(db, "nonexistent-period", "操作员")
        with pytest.raises(LookupError):
            get_period_detail(db, "nonexistent-period")
