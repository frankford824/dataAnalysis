"""Tests for certification report generation and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from commerce_harness.certify.report import (
    DISCLAIMER,
    build_certification_report,
    persist_certification,
)
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import WorkbenchPaths, initialize


def _setup(tmp_path: Path) -> tuple[DuckDBMemory, WorkbenchPaths]:
    config = load_config(workspace=tmp_path / "workbench")
    wb = initialize(config)
    db = DuckDBMemory(tmp_path / "workbench" / "harness.duckdb")
    db.initialize()
    return db, wb


def _seed_closed_period(db: DuckDBMemory) -> None:
    db.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES ('c1', 'taobao', 'e1', 's1', 'taobao',
                  1, DATE '2026-03-01', 'active', '{}')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        ) VALUES ('p1', 'c1', 's1', DATE '2026-03-01', DATE '2026-03-31', 'closed')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO run_log (run_id, contract_id, period_id, run_kind, status)
        VALUES ('run-1', 'c1', 'p1', 'reconcile', 'succeeded')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO pnl_cell (
            pnl_cell_id, run_id, period_id, store_id, sku_key,
            metric, definition_id, value, evidence_json, trust_tier
        ) VALUES
            ('pc1', 'run-1', 'p1', 's1', '__store_total__', 'revenue', 'd1',
             1000.00, '{}', 'certified'),
            ('pc2', 'run-1', 'p1', 's1', '__store_total__', 'cost', 'd1', 500.00, '{}', 'partial')
        ON CONFLICT DO NOTHING
        """
    )
    # A certification is only issuable against a frozen baseline.
    db.execute(
        """
        INSERT INTO baseline (
            baseline_id, contract_id, period_id, baseline_version,
            input_manifest_sha256, rule_set_sha256, code_sha, output_sha256,
            invariant_report_json, status, frozen_by, frozen_at
        ) VALUES (
            'baseline-p1-v1', 'c1', 'p1', 1, ?, ?, ?, ?,
            '{"reconcile_run_id": "run-1"}', 'frozen', '张三', current_timestamp
        )
        ON CONFLICT DO NOTHING
        """,
        ["a" * 64, "b" * 64, "c" * 40, "d" * 64],
    )


class TestBuildCertificationReport:
    def test_generates_report_for_closed_period(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        _seed_closed_period(db)
        report = build_certification_report(db, period_id="p1", operator_name="张三")
        assert report["reportKind"] == "certification"
        assert report["periodId"] == "p1"
        assert report["operatorName"] == "张三"
        assert report["disclaimer"] == DISCLAIMER
        assert "reportSha256" in report
        assert "reportId" in report
        assert report["hidesIncomplete"] is False
        db.close()

    def test_includes_trust_distribution(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        _seed_closed_period(db)
        report = build_certification_report(db, period_id="p1", operator_name="张三")
        trust = report["trustDistribution"]
        assert "certified" in trust
        assert "partial" in trust
        db.close()

    def test_refuses_open_period(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        db.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            ) VALUES ('c2', 'jd', 'e1', 's2', 'jd', 1, DATE '2026-04-01', 'active', '{}')
            ON CONFLICT DO NOTHING
            """
        )
        db.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            ) VALUES ('p2', 'c2', 's2', DATE '2026-04-01', DATE '2026-04-30', 'open')
            ON CONFLICT DO NOTHING
            """
        )
        with pytest.raises(RuntimeError, match="已结账"):
            build_certification_report(db, period_id="p2", operator_name="张三")
        db.close()

    def test_refuses_empty_operator(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        _seed_closed_period(db)
        with pytest.raises(ValueError, match="操作人"):
            build_certification_report(db, period_id="p1", operator_name="  ")
        db.close()

    def test_missing_period_raises(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        with pytest.raises(LookupError, match="账期不存在"):
            build_certification_report(db, period_id="nonexistent", operator_name="张三")
        db.close()

    def test_partial_blocked_noted(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        _seed_closed_period(db)
        report = build_certification_report(db, period_id="p1", operator_name="张三")
        assert "completenessNote" in report
        db.close()


class TestPersistCertification:
    def test_persists_to_disk(self, tmp_path: Path) -> None:
        db, wb = _setup(tmp_path)
        _seed_closed_period(db)
        report = build_certification_report(db, period_id="p1", operator_name="张三")
        path = persist_certification(db, wb, report)
        assert path.is_file()
        import json
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["reportId"] == report["reportId"]
        db.close()
