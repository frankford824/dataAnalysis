from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import commerce_harness.auto_compute as auto_compute_module
from commerce_harness.api import create_app
from commerce_harness.auto_compute import (
    AutoComputeRunner,
    _execution_signature,
    _manifest_signature,
    refresh_target_plan,
)
from commerce_harness.bootstrap import StoreTarget, bootstrap_targets, stable_identity
from commerce_harness.code_identity import CodeIdentity, resolve_code_identity
from commerce_harness.config import (
    ComputeConfig,
    HarnessConfig,
    SourceScope,
    WorkspaceConfig,
)
from commerce_harness.inventory import InventoryResult
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.target_plan import (
    MonthlyTarget,
    PeriodState,
    TargetPlan,
    TargetStatus,
)
from commerce_harness.workbench import initialize


def _single_scope_plan(
    status: TargetStatus = TargetStatus.AVAILABLE,
) -> TargetPlan:
    return TargetPlan(
        scope_start=date(2026, 2, 1),
        scope_end=date(2026, 2, 28),
        targets=(
            MonthlyTarget(
                target_key="target-test",
                platform="taobao",
                logical_store="测试旗舰店",
                logical_store_key="store-test",
                period="2026-02",
                status=status,
                period_state=PeriodState.CLOSED,
                source_ids=("candidate-1",) if status is not TargetStatus.MISSING else (),
                evidence=("test",),
                aliases=("测试旗舰店",),
            ),
        ),
        review_required=(),
    )


def _two_scope_plan() -> TargetPlan:
    first = _single_scope_plan().targets[0]
    return TargetPlan(
        scope_start=date(2026, 2, 1),
        scope_end=date(2026, 3, 31),
        targets=(
            first,
            MonthlyTarget(
                target_key="target-test-march",
                platform="taobao",
                logical_store="测试旗舰店",
                logical_store_key="store-test",
                period="2026-03",
                status=TargetStatus.AVAILABLE,
                period_state=PeriodState.CLOSED,
                source_ids=("candidate-2",),
                evidence=("test",),
                aliases=("测试旗舰店",),
            ),
        ),
        review_required=(),
    )


def test_dynamic_scope_runs_from_february_through_current_month() -> None:
    scope = SourceScope(
        include_all_discovered=True,
        start_month="2026-02",
        through_current_month=True,
    )

    assert scope.resolved_periods(date(2026, 7, 24)) == [
        "2602",
        "2603",
        "2604",
        "2605",
        "2606",
        "2607",
    ]


def test_period_scopes_keep_only_latest_contract_for_logical_store(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        for contract_id, logical_key, store_id, platform, created_offset in (
            (
                "contract-old",
                "legacy-key",
                "store-old",
                "taobao",
                "- INTERVAL 1 DAY",
            ),
            (
                "contract-current",
                "current-key",
                "store-current",
                "pinduoduo",
                "",
            ),
        ):
            database.execute(
                f"""
                INSERT INTO reconciliation_contract (
                    contract_id, logical_key, enterprise_id, store_id,
                    platform_code, contract_version, effective_from,
                    status, definition_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, 1,
                        DATE '2026-02-01', 'active', ?,
                        current_timestamp {created_offset})
                """,
                [
                    contract_id,
                    logical_key,
                    stable_identity("enterprise", "local-enterprise"),
                    store_id,
                    platform,
                    json.dumps({"store_name": "PDD测试旗舰店"}, ensure_ascii=False),
                ],
            )
            database.execute(
                """
                INSERT INTO accounting_period (
                    period_id, contract_id, store_id, period_start,
                    period_end, status
                )
                VALUES (?, ?, ?, DATE '2026-02-01',
                        DATE '2026-02-28', 'open')
                """,
                [f"period-{store_id}", contract_id, store_id],
            )

    plan = TargetPlan(
        scope_start=date(2026, 2, 1),
        scope_end=date(2026, 2, 28),
        targets=(
            MonthlyTarget(
                target_key="target-pdd-test",
                platform="pinduoduo",
                logical_store="PDD测试旗舰店",
                logical_store_key="store-pdd-test",
                period="2026-02",
                status=TargetStatus.AVAILABLE,
                period_state=PeriodState.CLOSED,
                source_ids=("source-pdd-test",),
                evidence=("test",),
                aliases=("PDD测试旗舰店",),
            ),
        ),
        review_required=(),
    )
    scopes = AutoComputeRunner(config, workbench)._period_scopes(plan)

    assert len(scopes) == 1
    assert scopes[0]["contract_id"] == "contract-current"
    assert scopes[0]["store_id"] == "store-current"


def test_inventory_signature_ignores_scan_timestamp_and_non_candidates(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "inventory.json"
    payload = {
        "scanned_at": "2026-07-24T01:00:00Z",
        "candidate_source_ids": ["source-a"],
        "records": [
            {
                "source_id": "source-a",
                "path": r"D:\orders\a.xlsx",
                "purpose": "orders",
                "size": 10,
                "mtime_utc": "2026-07-24T00:00:00Z",
                "attributes": [],
            },
            {
                "source_id": "ignored",
                "path": r"D:\history\old.xlsx",
                "purpose": "orders",
                "size": 20,
                "mtime_utc": "2020-01-01T00:00:00Z",
                "attributes": [],
            },
        ],
    }
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    first = _manifest_signature(inventory)

    payload["scanned_at"] = "2026-07-24T02:00:00Z"
    payload["records"][1]["size"] = 99
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    assert _manifest_signature(inventory) == first


def test_runner_is_disabled_by_default_and_schema_contains_job_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FA_AUTO_COMPUTE", raising=False)
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
    )
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        assert "compute_job" in database.table_names()

    runner = AutoComputeRunner(config, workbench)
    result = runner.trigger()

    assert result.accepted is False
    assert result.running is False


def test_container_source_identity_is_traceable_but_not_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "commit_sha": "base-commit",
                "worktree_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FA_CODE_IDENTITY_FILE", str(identity_path))

    identity = resolve_code_identity(tmp_path / "outside-a-git-repository")

    assert identity.value == f"base-commit+dirty.{'a' * 16}"
    assert identity.committed is False


def test_compute_api_is_honest_when_automatic_compute_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FA_AUTO_COMPUTE", raising=False)
    config = HarnessConfig(workspace=WorkspaceConfig(root=tmp_path))
    initialize(config)

    with TestClient(create_app(config)) as client:
        status = client.get("/api/v1/status")
        trigger = client.post("/api/v1/compute/run")
        targets = client.get("/api/v1/compute/targets")

    assert status.status_code == 200
    assert status.json()["localComputeEnabled"] is False
    assert trigger.status_code == 409
    assert targets.status_code == 200
    assert targets.json()["scope_start"] == "2026-02-01"
    assert targets.json()["targets"] == []


def test_compute_jobs_serializes_timestamps_without_optional_timezone_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FA_AUTO_COMPUTE", raising=False)
    config = HarnessConfig(workspace=WorkspaceConfig(root=tmp_path))
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO compute_job (
                job_id, cycle_id, job_kind, status, progress_percent,
                business_label, detail, started_at, finished_at
            )
            VALUES (
                'job-visible', 'cycle-visible', 'inventory', 'succeeded', 100,
                '检查文件', '已完成',
                TIMESTAMPTZ '2026-07-24 12:34:56+00',
                TIMESTAMPTZ '2026-07-24 12:35:00+00'
            )
            """
        )

    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/compute/jobs")

    assert response.status_code == 200
    [job] = response.json()
    assert job["jobId"] == "job-visible"
    assert job["status"] == "succeeded"
    assert job["progressPercent"] == 100
    assert job["startedAt"] == "2026-07-24 12:34:56+00"
    assert job["finishedAt"] == "2026-07-24 12:35:00+00"


def test_refresh_target_plan_bootstraps_platform_store_months_from_content(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        source={
            "scope": {
                "include_all_discovered": True,
                "start_month": "2026-02",
                "through_current_month": True,
            }
        },
    )
    workbench = initialize(config)
    source_path = r"D:\电商\导出\订单.xlsx"
    inventory = {
        "candidate_source_ids": ["source-orders"],
        "records": [
            {
                "source_id": "source-orders",
                "path": source_path,
                "purpose": "orders",
                "size": 123,
                "mtime_utc": "2026-07-23T00:00:00+00:00",
                "attributes": [],
            }
        ],
    }
    (workbench.reports / "source-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False),
        encoding="utf-8",
    )
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status, finished_at)
            VALUES ('freeze-plan', 'freeze', 'succeeded', current_timestamp),
                   ('profile-plan', 'parse', 'succeeded', current_timestamp)
            """
        )
        database.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri,
                source_uri, source_modified_ns, captured_at, manifest_json
            )
            VALUES (
                'snapshot-orders', ?, 123, 'objects/orders',
                ?, 1, current_timestamp, '{}'
            )
            """,
            ["a" * 64, f"finance-win-ro://{source_path}"],
        )
        database.execute(
            """
            INSERT INTO source_profile (
                profile_id, run_id, snapshot_id, parser_version, status,
                source_kind, template_id, route_json
            )
            VALUES (
                'profile-orders', 'profile-plan', 'snapshot-orders',
                'parser-v1', 'matched', 'orders', 'taobao_order_v1', ?
            )
            """,
            [
                json.dumps(
                    {
                        "platform": "taobao",
                        "store_name": "测试旗舰店",
                        "content_periods": ["2026-02"],
                    },
                    ensure_ascii=False,
                )
            ],
        )

    plan = refresh_target_plan(config, workbench)

    assert len(plan.targets) >= 1
    assert plan.targets[0].platform == "taobao"
    assert plan.targets[0].logical_store == "测试旗舰店"
    with DuckDBMemory(workbench.database) as database:
        assert database.fetchone_required(
            "SELECT count(*) FROM reconciliation_contract"
        )[0] == 1
        assert database.fetchone_required(
            "SELECT count(*) FROM accounting_period"
        )[0] >= 1


def test_runner_cycle_persists_success_and_failure_without_cross_scope_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    inventory_path = workbench.reports / "source-inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "candidate_source_ids": ["candidate-1"],
                "records": [
                    {
                        "source_id": "candidate-1",
                        "path": r"D:\测试旗舰店\2602\订单.csv",
                        "purpose": "orders",
                        "size": 10,
                        "mtime_utc": "2026-02-28T00:00:00+00:00",
                        "attributes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-cycle', 'freeze', 'succeeded')
            """
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-cycle",
            targets=[
                StoreTarget(
                    name="测试旗舰店",
                    period_tokens=["2602", "2603"],
                    platform_code="taobao",
                )
            ],
            records=[],
            snapshot_by_source_id={},
        )

    monkeypatch.setattr(
        auto_compute_module,
        "scan_inventory",
        lambda *_args: InventoryResult(
            path=inventory_path,
            sha256="inventory-sha",
            record_count=1,
            candidate_count=1,
            offline_count=0,
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "_execution_signature",
        lambda *_args: "execution-signature",
    )
    monkeypatch.setattr(
        auto_compute_module,
        "freeze_candidates",
        lambda *_args, **_kwargs: {"captured": 1},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "profile_snapshots",
        lambda *_args: {"matched": 1},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "refresh_target_plan",
        lambda *_args: _two_scope_plan(),
    )
    normalized_periods: list[tuple[str, ...]] = []

    def normalize_all_periods(*_args: object, **kwargs: object) -> dict[str, int]:
        periods = kwargs["periods"]
        assert isinstance(periods, tuple)
        normalized_periods.append(periods)
        return {"current_revisions": 1}

    monkeypatch.setattr(
        auto_compute_module,
        "normalize_workspace",
        normalize_all_periods,
    )
    monkeypatch.setattr(
        auto_compute_module,
        "adjudicate_workspace",
        lambda *_args, **_kwargs: {"decisions": 0},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "reconcile_period",
        lambda *_args, **kwargs: (
            (_ for _ in ()).throw(
                RuntimeError("该账期没有已确认的标准化输入版本")
            )
            if kwargs["period_token"] == "2603"
            else {"balanced": 1}
        ),
    )
    runner = AutoComputeRunner(config, workbench)

    runner._run_cycle()
    failed_job = runner._new_job(
        cycle_id="cycle-failure",
        kind="profile",
        label="故意失败",
    )
    with pytest.raises(RuntimeError, match="expected failure"):
        runner._run_job(
            failed_job,
            "正在验证错误边界",
            lambda: (_ for _ in ()).throw(RuntimeError("expected failure")),
        )

    with DuckDBMemory(workbench.database) as database:
        statuses = database.execute(
            """
            SELECT job_kind, status
            FROM compute_job
            ORDER BY created_at, job_id
            """
        ).fetchall()
        assert ("inventory", "succeeded") in statuses
        assert ("freeze", "succeeded") in statuses
        assert ("profile", "succeeded") in statuses
        assert ("normalize", "succeeded") in statuses
        assert ("reconcile", "succeeded") in statuses
        assert ("profile", "failed") in statuses
        assert database.fetchone_required(
            """
            SELECT count(*) FROM compute_job
            WHERE job_kind = 'normalize' AND status = 'succeeded'
            """
        )[0] == 1
        assert database.fetchone_required(
            """
            SELECT count(*) FROM compute_job
            WHERE job_kind = 'reconcile' AND status = 'succeeded'
            """
        )[0] == 2
    assert normalized_periods == [("2602", "2603")]


def test_runner_resumes_only_missing_scopes_without_repeating_adjudication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    inventory_path = workbench.reports / "source-inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "candidate_source_ids": ["candidate-1"],
                "records": [
                    {
                        "source_id": "candidate-1",
                        "path": r"D:\测试旗舰店\2602\订单.csv",
                        "purpose": "orders",
                        "size": 10,
                        "mtime_utc": "2026-02-28T00:00:00+00:00",
                        "attributes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-resume', 'freeze', 'succeeded')
            """
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-resume",
            targets=[
                StoreTarget(
                    name="测试旗舰店",
                    period_tokens=["2602", "2603"],
                    platform_code="taobao",
                )
            ],
            records=[],
            snapshot_by_source_id={},
        )
        scope_rows = database.execute(
            """
            SELECT contract_id, period_id, store_id,
                   strftime(period_start, '%y%m')
            FROM accounting_period
            ORDER BY period_start
            """
        ).fetchall()

    runner = AutoComputeRunner(config, workbench)
    previous_inventory = runner._new_job(
        cycle_id="cycle-previous",
        kind="inventory",
        label="检查文件",
    )
    previous_normalize = runner._new_job(
        cycle_id="cycle-previous",
        kind="normalize",
        label="整理店铺",
        store_id=str(scope_rows[0][2]),
    )
    previous_reconcile = runner._new_job(
        cycle_id="cycle-previous",
        kind="reconcile",
        label="计算二月",
        contract_id=str(scope_rows[0][0]),
        period_id=str(scope_rows[0][1]),
        store_id=str(scope_rows[0][2]),
        period_token="2602",
    )
    with DuckDBMemory(workbench.database) as database:
        database.execute(
            """
            UPDATE compute_job
            SET status = 'succeeded',
                metrics_json = '{"manifest_signature":"execution-signature"}',
                finished_at = current_timestamp
            WHERE job_id = ?
            """,
            [previous_inventory],
        )
        database.execute(
            """
            UPDATE compute_job
            SET status = 'succeeded',
                metrics_json = '{"current_revisions":1}',
                finished_at = current_timestamp
            WHERE job_id = ?
            """,
            [previous_normalize],
        )
        database.execute(
            """
            UPDATE compute_job
            SET status = 'succeeded',
                metrics_json = '{"balanced":1}',
                finished_at = current_timestamp
            WHERE job_id = ?
            """,
            [previous_reconcile],
        )

    monkeypatch.setattr(
        auto_compute_module,
        "scan_inventory",
        lambda *_args: InventoryResult(
            path=inventory_path,
            sha256="inventory-sha",
            record_count=1,
            candidate_count=1,
            offline_count=0,
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "_execution_signature",
        lambda *_args: "execution-signature",
    )
    monkeypatch.setattr(
        auto_compute_module,
        "freeze_candidates",
        lambda *_args, **_kwargs: {"captured": 0},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "profile_snapshots",
        lambda *_args: {"matched": 0},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "refresh_target_plan",
        lambda *_args: _two_scope_plan(),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "normalize_workspace",
        lambda *_args, **_kwargs: pytest.fail(
            "文件和规则未变化时不应重复整理店铺"
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "adjudicate_workspace",
        lambda *_args, **_kwargs: pytest.fail(
            "没有新的整理结果时不应重复裁决"
        ),
    )
    reconciled: list[str] = []

    def reconcile_only_missing(*_args: object, **kwargs: object) -> dict[str, int]:
        reconciled.append(str(kwargs["period_token"]))
        return {"balanced": 1}

    monkeypatch.setattr(
        auto_compute_module,
        "reconcile_period",
        reconcile_only_missing,
    )

    runner._run_cycle()

    assert reconciled == ["2603"]


@pytest.mark.parametrize(
    "target_status",
    [TargetStatus.AVAILABLE, TargetStatus.MISSING],
)
def test_runner_marks_scope_without_current_input_as_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_status: TargetStatus,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    inventory_path = workbench.reports / "source-inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "candidate_source_ids": ["candidate-1"],
                "records": [
                    {
                        "source_id": "candidate-1",
                        "path": r"D:\测试旗舰店\2602\订单.csv",
                        "purpose": "orders",
                        "size": 10,
                        "mtime_utc": "2026-02-28T00:00:00+00:00",
                        "attributes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-cycle', 'freeze', 'succeeded')
            """
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-cycle",
            targets=[
                StoreTarget(
                    name="测试旗舰店",
                    period_tokens=["2602"],
                    platform_code="taobao",
                )
            ],
            records=[],
            snapshot_by_source_id={},
        )

    monkeypatch.setattr(
        auto_compute_module,
        "scan_inventory",
        lambda *_args: InventoryResult(
            path=inventory_path,
            sha256="inventory-sha",
            record_count=1,
            candidate_count=1,
            offline_count=0,
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "_execution_signature",
        lambda *_args: "execution-signature",
    )
    monkeypatch.setattr(
        auto_compute_module,
        "freeze_candidates",
        lambda *_args, **_kwargs: {"captured": 1},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "profile_snapshots",
        lambda *_args: {"matched": 1},
    )
    monkeypatch.setattr(
        auto_compute_module,
        "refresh_target_plan",
        lambda *_args: _single_scope_plan(target_status),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "normalize_workspace",
        (
            (lambda *_args, **_kwargs: {"current_revisions": 0})
            if target_status is TargetStatus.AVAILABLE
            else (
                lambda *_args, **_kwargs: pytest.fail(
                    "明确缺少来源时不应扫描标准化文件"
                )
            )
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "adjudicate_workspace",
        lambda *_args, **_kwargs: pytest.fail(
            "没有输入时不应执行业务裁决"
        ),
    )
    monkeypatch.setattr(
        auto_compute_module,
        "reconcile_period",
        lambda *_args, **_kwargs: pytest.fail(
            "没有输入时不应计算经营结果"
        ),
    )

    AutoComputeRunner(config, workbench)._run_cycle()

    with DuckDBMemory(workbench.database) as database:
        waiting = database.fetchone_required(
            """
            SELECT status, detail,
                   json_extract_string(metrics_json, '$.data_status')
            FROM compute_job
            WHERE job_kind = 'reconcile'
            """
        )
    assert waiting == (
        "succeeded",
        "等待本月来源文件",
        "waiting_for_input",
    )


def test_execution_signature_and_runner_lifecycle_are_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
    monkeypatch.setattr(
        auto_compute_module,
        "resolve_code_identity",
        lambda *_args: CodeIdentity(
            commit_sha="test-base",
            worktree_sha256="a" * 64,
        ),
    )

    first_signature = _execution_signature(
        config,
        workbench,
        "inventory-signature",
    )
    second_signature = _execution_signature(
        config,
        workbench,
        "inventory-signature",
    )
    assert first_signature == second_signature
    assert len(first_signature) == 64

    runner = AutoComputeRunner(config, workbench)
    interrupted = runner._new_job(
        cycle_id="cycle-interrupted",
        kind="inventory",
        label="中断任务",
    )
    with DuckDBMemory(workbench.database) as database:
        database.execute(
            """
            UPDATE compute_job
            SET status = 'running', started_at = current_timestamp
            WHERE job_id = ?
            """,
            [interrupted],
        )
    completed = threading.Event()
    monkeypatch.setattr(runner, "_run_cycle", completed.set)

    runner.start()
    trigger = runner.trigger()
    assert trigger.accepted is True
    assert completed.wait(timeout=2)
    runner.stop(timeout=2)

    with DuckDBMemory(workbench.database) as database:
        recovered = database.fetchone_required(
            "SELECT status, detail FROM compute_job WHERE job_id = ?",
            [interrupted],
        )
    assert recovered == ("failed", "服务已经恢复，可重新开始计算")


def test_runner_can_reuse_successful_normalization_and_scope_results(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-reuse', 'freeze', 'succeeded')
            """
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-reuse",
            targets=[
                StoreTarget(
                    name="复用测试店铺",
                    period_tokens=["2602"],
                    platform_code="taobao",
                )
            ],
            records=[],
            snapshot_by_source_id={},
        )
        contract_id, period_id, store_id = database.fetchone_required(
            """
            SELECT p.contract_id, p.period_id, p.store_id
            FROM accounting_period p
            LIMIT 1
            """
        )

    runner = AutoComputeRunner(config, workbench)
    normalize_job = runner._new_job(
        cycle_id="cycle-reuse",
        kind="normalize",
        label="整理店铺",
        store_id=str(store_id),
    )
    reconcile_job = runner._new_job(
        cycle_id="cycle-reuse",
        kind="reconcile",
        label="计算经营结果",
        contract_id=str(contract_id),
        period_id=str(period_id),
        store_id=str(store_id),
        period_token="2602",
    )
    with DuckDBMemory(workbench.database) as database:
        database.execute(
            """
            UPDATE compute_job
            SET status = 'succeeded',
                metrics_json = '{"current_revisions":2}',
                finished_at = current_timestamp
            WHERE job_id = ?
            """,
            [normalize_job],
        )
        database.execute(
            """
            UPDATE compute_job
            SET status = 'succeeded',
                metrics_json = '{"balanced":1}',
                finished_at = current_timestamp
            WHERE job_id = ?
            """,
            [reconcile_job],
        )

    assert runner._latest_normalization_result(str(store_id)) == {
        "current_revisions": 2
    }
    assert runner._succeeded_scope_results() == {
        (str(contract_id), str(period_id))
    }
    assert runner._all_scope_results_current() is True


def test_progress_and_cycle_failure_are_visible_as_business_status(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        compute=ComputeConfig(enabled=True, run_on_startup=False),
    )
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
    runner = AutoComputeRunner(config, workbench)
    job_id = runner._new_job(
        cycle_id="cycle-progress",
        kind="freeze",
        label="保存原始文件",
    )
    with DuckDBMemory(workbench.database) as database:
        database.execute(
            "UPDATE compute_job SET status = 'running' WHERE job_id = ?",
            [job_id],
        )

    runner._update_freeze_progress(job_id, 50, 100)
    runner._record_cycle_failure(RuntimeError("remote temporarily unavailable"))

    with DuckDBMemory(workbench.database) as database:
        progress = database.fetchone_required(
            """
            SELECT progress_percent, detail
            FROM compute_job
            WHERE job_id = ?
            """,
            [job_id],
        )
        failure = database.fetchone_required(
            """
            SELECT status, detail, error_detail
            FROM compute_job
            WHERE cycle_id <> 'cycle-progress'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
    assert progress == (52, "已安全保存 50/100 份候选文件")
    assert failure == (
        "failed",
        "自动计算暂时停止，服务会继续重试",
        "remote temporarily unavailable",
    )
