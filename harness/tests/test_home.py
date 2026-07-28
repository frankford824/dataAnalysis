"""Home screen aggregation: one period, one run, no double counting."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from commerce_harness.api import create_app
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


@pytest.fixture(autouse=True)
def _no_auto_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FA_AUTO_COMPUTE", "0")


def _client(tmp_path: Path) -> TestClient:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    return TestClient(create_app(config))


def _database(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    database = DuckDBMemory(workbench.database)
    database.initialize()
    return database


def _seed(database: DuckDBMemory) -> None:
    database.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES ('contract-1', 'taobao', 'ent-1', 'store-1', 'taobao',
                  1, DATE '2026-01-01', 'active', '{}')
        """
    )
    for period_id, start, end in (
        ("period-feb", "2026-02-01", "2026-02-28"),
        ("period-mar", "2026-03-01", "2026-03-31"),
    ):
        database.execute(
            f"""
            INSERT INTO accounting_period (
                period_id, contract_id, store_id,
                period_start, period_end, status
            ) VALUES ('{period_id}', 'contract-1', 'store-1',
                      DATE '{start}', DATE '{end}', 'open')
            """
        )
    # Two attempts on March plus one on February: only run-mar-2 counts.
    for run_id, period_id, minute in (
        ("run-feb-1", "period-feb", "10:00:00"),
        ("run-mar-1", "period-mar", "11:00:00"),
        ("run-mar-2", "period-mar", "12:00:00"),
    ):
        database.execute(
            f"""
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status,
                started_at, finished_at
            ) VALUES ('{run_id}', 'contract-1', '{period_id}',
                      'reconcile', 'succeeded',
                      TIMESTAMPTZ '2026-04-01 {minute}+00',
                      TIMESTAMPTZ '2026-04-01 {minute}+00')
            """
        )
    for run_id, period_id, value in (
        ("run-feb-1", "period-feb", 7000),
        ("run-mar-1", "period-mar", 1000),
        ("run-mar-2", "period-mar", 1234),
    ):
        database.execute(
            f"""
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json, trust_tier
            ) VALUES ('cell-{run_id}', '{run_id}', '{period_id}', 'store-1',
                      '__store_total__', 'profit', 'def-1', {value},
                      '{{}}', 'certified')
            """
        )
    database.execute(
        """
        INSERT INTO checklist_requirement (
            requirement_id, contract_id, source_kind, store_scope,
            effective_from, expected_frequency
        ) VALUES ('req-1', 'contract-1', 'platform', 'store-1',
                  DATE '2026-01-01', 'monthly')
        """
    )
    for run_id, period_id, status in (
        ("run-mar-1", "period-mar", "missing"),
        ("run-mar-2", "period-mar", "present"),
    ):
        database.execute(
            f"""
            INSERT INTO checklist_result (
                result_id, run_id, period_id, requirement_id, status
            ) VALUES ('cr-{run_id}', '{run_id}', '{period_id}',
                      'req-1', '{status}')
            """
        )
    for balance_id, run_id, period_id in (
        ("bal-mar-1", "run-mar-1", "period-mar"),
        ("bal-mar-2", "run-mar-2", "period-mar"),
    ):
        database.execute(
            f"""
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount,
                difference_amount, status
            ) VALUES ('{balance_id}', '{run_id}', 'contract-1', '{period_id}',
                      'key-1', 100, 50, 50, 50, 'unresolved')
            """
        )
    for unresolved_id, balance_id, amount in (
        ("ub-old", "bal-mar-1", 900),
        ("ub-new", "bal-mar-2", 50),
    ):
        database.execute(
            f"""
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            ) VALUES ('{unresolved_id}', '{balance_id}',
                      'amount_mismatch', {amount}, 'open')
            """
        )
    database.execute(
        """
        INSERT INTO invariant_definition (
            invariant_id, domain, family, title, definition_json, origin
        ) VALUES ('inv-1', 'settlement', 'equality', '差额',
                  '{"family":"equality"}', 'builtin')
        """
    )
    database.execute(
        """
        INSERT INTO invariant_version (
            invariant_version_id, invariant_id, semver, status
        ) VALUES ('inv-1:1.0.0', 'inv-1', '1.0.0', 'active')
        """
    )
    for claim_id, period_id, amount in (
        ("claim-feb", "period-feb", 4000),
        ("claim-mar", "period-mar", 120),
    ):
        database.execute(
            f"""
            INSERT INTO claim (
                claim_id, contract_id, period_id, store_id,
                invariant_version_id, subject_kind, subject_key,
                reason_code, claimed_amount, currency, status
            ) VALUES ('{claim_id}', 'contract-1', '{period_id}', 'store-1',
                      'inv-1:1.0.0', 'unresolved_balance', 'ub-new',
                      'amount_mismatch', {amount}, 'CNY', 'draft')
            """
        )


def _bar(payload: dict, bar_id: str) -> dict:
    return next(bar for bar in payload["bars"] if bar["id"] == bar_id)


def test_home_without_any_run_refuses_to_report_zero(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/v1/home")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] is None
    assert _bar(payload, "conclusion")["tone"] == "pending"
    assert "0 元" not in _bar(payload, "conclusion")["summary"]


def test_home_reports_only_latest_run_of_latest_period(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _seed(database)
    database.close()

    payload = _client(tmp_path).get("/api/v1/home").json()

    assert payload["scope"]["periodId"] == "period-mar"
    assert payload["scope"]["runId"] == "run-mar-2"
    # 1234 only: not 1234 + 1000 (stale run) nor + 7000 (other period).
    assert "1234" in _bar(payload, "conclusion")["summary"]
    assert _bar(payload, "conclusion")["tone"] == "ok"
    # Stale run said the file was missing; the current run says it arrived.
    assert _bar(payload, "files")["summary"] == "本月应到文件已齐。"
    decide = _bar(payload, "decide")
    assert "1 件事" in decide["summary"]
    assert "50" in decide["summary"] and "950" not in decide["summary"]
    recoverable = _bar(payload, "recoverable")
    assert "共 1 笔" in recoverable["summary"]
    assert "120" in recoverable["summary"]
