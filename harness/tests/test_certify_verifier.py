from __future__ import annotations

import json
from pathlib import Path

import pytest
from verifier import verify

from commerce_harness import period_service
from commerce_harness.certify.report import DISCLAIMER, build_certification_report
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _seed_contract_and_period(database: DuckDBMemory) -> None:
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
            'p1', 'c1', 's1', DATE '2026-02-01', DATE '2026-02-28', 'open'
        )
        """
    )


def _seed_run_and_numbers(database: DuckDBMemory) -> None:
    for run_id, minute, value in (
        ("run-stale", "10:00:00", 9999),
        ("run-final", "11:00:00", 1500),
    ):
        database.execute(
            f"""
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status,
                started_at, finished_at
            ) VALUES ('{run_id}', 'c1', 'p1', 'reconcile', 'succeeded',
                      TIMESTAMPTZ '2026-03-01 {minute}+00',
                      TIMESTAMPTZ '2026-03-01 {minute}+00')
            """
        )
        database.execute(
            f"""
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json, trust_tier
            ) VALUES ('cell-{run_id}', '{run_id}', 'p1', 's1',
                      '__store_total__', 'profit', 'def-1', {value},
                      '{{}}', 'certified')
            """
        )


def _freeze_baseline(database: DuckDBMemory, *, run_id: str = "run-final") -> None:
    database.execute(
        """
        INSERT INTO baseline (
            baseline_id, contract_id, period_id, baseline_version,
            input_manifest_sha256, rule_set_sha256, code_sha, output_sha256,
            invariant_report_json, status, frozen_by, frozen_at
        ) VALUES (
            'baseline-p1-v1', 'c1', 'p1', 1, ?, ?, ?, ?, ?, 'frozen',
            'alice', current_timestamp
        )
        """,
        [
            "a" * 64,
            "b" * 64,
            "c" * 40,
            "d" * 64,
            json.dumps({"reconcile_run_id": run_id}),
        ],
    )


def _close(database: DuckDBMemory) -> None:
    period_service.preclose_period(database, "p1", "alice")
    period_service.finalize_period(database, "p1", "alice")


def _database(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "wb")
    workbench = initialize(config)
    database = DuckDBMemory(workbench.database)
    database.initialize()
    return database


def test_certification_requires_closed(tmp_path: Path) -> None:
    with _database(tmp_path) as database:
        _seed_contract_and_period(database)
        _seed_run_and_numbers(database)
        _freeze_baseline(database)
        with pytest.raises(RuntimeError):
            build_certification_report(
                database, period_id="p1", operator_name="alice"
            )


def test_certification_requires_frozen_baseline(tmp_path: Path) -> None:
    with _database(tmp_path) as database:
        _seed_contract_and_period(database)
        _seed_run_and_numbers(database)
        _close(database)
        with pytest.raises(RuntimeError, match="黄金基线"):
            build_certification_report(
                database, period_id="p1", operator_name="alice"
            )


def test_certification_and_offline_verifier(tmp_path: Path) -> None:
    with _database(tmp_path) as database:
        _seed_contract_and_period(database)
        _seed_run_and_numbers(database)
        _freeze_baseline(database)
        # Closing writes accounting_period_state; accounting_period.status
        # stays 'open', which used to make certification refuse outright.
        _close(database)
        report = build_certification_report(
            database, period_id="p1", operator_name="alice"
        )

    assert report["status"] == "closed"
    assert report["runId"] == "run-final"
    # Money comes from the baseline's run only, never from the stale attempt.
    assert report["trustDistribution"]["certified"]["amount"] == "1500.0000"
    assert report["hidesIncomplete"] is False
    assert report["disclaimer"] == DISCLAIMER

    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = verify(path)
    assert result.passed, result.errors


def _valid_report(tmp_path: Path) -> dict:
    with _database(tmp_path) as database:
        _seed_contract_and_period(database)
        _seed_run_and_numbers(database)
        _freeze_baseline(database)
        _close(database)
        return build_certification_report(
            database, period_id="p1", operator_name="alice"
        )


def _write(path: Path, report: dict) -> Path:
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def test_verifier_rejects_tamper(tmp_path: Path) -> None:
    report = _valid_report(tmp_path)
    report["trustDistribution"]["certified"]["amount"] = "999999"
    result = verify(_write(tmp_path / "bad.json", report))
    assert result.passed is False
    assert any("hash" in error for error in result.errors)


def test_verifier_rejects_missing_baseline_hashes(tmp_path: Path) -> None:
    report = _valid_report(tmp_path)
    report["baseline"] = {
        "inputManifestSha256": None,
        "ruleSetSha256": None,
        "codeSha": None,
        "outputSha256": None,
    }
    result = verify(_write(tmp_path / "no-baseline.json", report))
    assert result.passed is False
    assert any("baseline evidence refs missing" in error for error in result.errors)


def test_verifier_rejects_report_with_no_numbers(tmp_path: Path) -> None:
    report = _valid_report(tmp_path)
    report["trustDistribution"] = {}
    result = verify(_write(tmp_path / "empty.json", report))
    assert result.passed is False
    assert any("trustDistribution is empty" in error for error in result.errors)


def test_verifier_rejects_hidden_incomplete_part(tmp_path: Path) -> None:
    report = _valid_report(tmp_path)
    report["trustDistribution"]["blocked"] = {"count": 2, "amount": "300"}
    report["blocked"] = {"count": 2, "amount": "300"}
    report.pop("completenessNote", None)
    result = verify(_write(tmp_path / "hidden.json", report))
    assert result.passed is False
    assert any("completenessNote" in error for error in result.errors)


def test_verifier_rejects_open_period_report(tmp_path: Path) -> None:
    report = _valid_report(tmp_path)
    report["status"] = "open"
    result = verify(_write(tmp_path / "open.json", report))
    assert result.passed is False
    assert any("closed period" in error for error in result.errors)
