from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from commerce_harness.api import create_app
from commerce_harness.bootstrap import stable_identity
from commerce_harness.config import load_config
from commerce_harness.evidence_policy import (
    NORMALIZATION_RULE_VERSION,
    PERFORMANCE_ENGINE_VERSION,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.performance import (
    _bulk_insert,
    _ensure_person,
    _ensure_product,
    _money,
    _period_dates,
    _source_name,
    sync_performance_sources,
)
from commerce_harness.performance_engine import ensure_builtin_performance_policy
from commerce_harness.workbench import initialize


def _save_workbook(path: Path, rows: list[list[object]], title: str) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def _snapshot(
    memory: DuckDBMemory,
    *,
    snapshot_id: str,
    path: Path,
    source_uri: str,
) -> None:
    memory.execute(
        """
        INSERT INTO source_snapshot (
            snapshot_id, content_sha256, byte_size, object_uri, source_uri,
            original_name, captured_at, manifest_json
        )
        VALUES (?, ?, ?, ?, ?, ?, current_timestamp, '{}')
        """,
        [
            snapshot_id,
            f"sha-{snapshot_id}",
            path.stat().st_size,
            str(path),
            source_uri,
            path.name,
        ],
    )


def test_performance_value_guards_and_identity_cache(tmp_path: Path) -> None:
    assert _source_name(
        r"finance-win-ro://D:\Finance\工资\2026\2602.csv"
    ) == "2602.csv"
    assert _money("") == Decimal("0.0000")
    assert _money("1,234.56789") == Decimal("1234.5679")
    with pytest.raises(ValueError, match="金额格式无法识别"):
        _money("not-money")
    assert _period_dates("2602") == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    with pytest.raises(ValueError, match="无效月份"):
        _period_dates("2026-02")

    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        with memory.transaction() as connection:
            _bulk_insert(
                connection,
                table="canonical_product",
                columns=["product_id"],
                rows=[],
                conflict_columns=["product_id"],
            )
            assert _ensure_product(
                connection,
                "enterprise-1",
                "",
                cache={},
            ) is None
            product_cache: dict[str, str] = {}
            first_product = _ensure_product(
                connection,
                "enterprise-1",
                "SKU-001",
                cache=product_cache,
            )
            assert first_product is not None
            assert (
                _ensure_product(
                    connection,
                    "enterprise-1",
                    "SKU-001",
                    cache=product_cache,
                )
                == first_product
            )
            assert _ensure_person(
                connection,
                enterprise_id="enterprise-1",
                name="",
                status="provisional",
                snapshot_id="snapshot-1",
                row_no=1,
                cache={},
            ) is None


def test_syncs_people_assignments_and_reference_metrics_idempotently(
    tmp_path: Path,
) -> None:
    employee_path = tmp_path / "employees.xlsx"
    operator_path = tmp_path / "operator-links.xlsx"
    reference_path = tmp_path / "测试店铺.csv"
    _save_workbook(
        employee_path,
        [
            ["姓名", "身份证号码", "部门", "类型"],
            ["员工甲", "ignored-sensitive-value", "电商一部", "运营"],
            ["员工乙", "ignored-sensitive-value", "电商二部", "运营"],
        ],
        "员工信息表",
    )
    _save_workbook(
        operator_path,
        [
            ["宝贝编码", "是否启用", "修改", "2602姓名", "2603姓名"],
            ["SKU-1", None, None, "员工甲", "员工乙"],
        ],
        "商品ID",
    )
    with reference_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "2602姓名",
                "宝贝编码",
                "交易收款",
                "交易退款",
                "交易赔付",
                "软件服务费",
                "营销费用",
                "发货运费",
                "订单成本",
                "补发成本",
                "本金佣金",
                "代购代发",
                "店铺毛利",
                "毛利率",
                "广告费",
                "店铺利润",
                "利润率",
            ]
        )
        writer.writerow(
            [
                "员工甲",
                "SKU-1",
                "100",
                "-10",
                "0",
                "-5",
                "-2",
                "-3",
                "-20",
                "0",
                "0",
                "0",
                "60",
                "0.6",
                "-10",
                "50",
                "0.5",
            ]
        )

    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        _snapshot(
            memory,
            snapshot_id="employee-snapshot",
            path=employee_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\工资\\员工信息表.xlsx"
            ),
        )
        _snapshot(
            memory,
            snapshot_id="operator-snapshot",
            path=operator_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\其他\\运营链接.xlsx"
            ),
        )
        _snapshot(
            memory,
            snapshot_id="reference-snapshot",
            path=reference_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\工资\\2026"
                "\\阿里单算\\2月\\测试店铺.csv"
            ),
        )

    enterprise_id = stable_identity("enterprise", "local-enterprise")
    first = sync_performance_sources(workbench, enterprise_id=enterprise_id)
    second = sync_performance_sources(workbench, enterprise_id=enterprise_id)

    assert first.imported_snapshots == 3
    assert first.employee_rows == 2
    assert first.assignment_rows == 2
    assert first.reference_rows == 1
    assert first.issue_count == 0
    assert second.imported_snapshots == 0
    assert second.skipped_snapshots == 3
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT collected_amount, refund_amount, gross_profit,
                   advertising_fee, store_profit, gross_formula_residual,
                   profit_formula_residual, validation_status
            FROM performance_reference_fact
            """
        ).fetchone() == (
            100,
            -10,
            60,
            -10,
            50,
            0,
            0,
            "passed",
        )
        assert memory.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE store_name = '测试店铺')
            FROM responsibility_assignment_version
            WHERE status = 'active'
            """
        ).fetchone() == (3, 1)
        assert memory.execute(
            """
            SELECT count(*) FROM person_identity
            WHERE status = 'active'
            """
        ).fetchone() == (2,)
    client = TestClient(create_app(config))
    overview = client.get(
        "/api/v1/performance/overview",
        params={"calculationMode": "single", "period": "2602"},
    )
    people = client.get(
        "/api/v1/performance/people",
        params={"calculationMode": "single", "period": "2602"},
    )
    assert overview.status_code == 200
    assert overview.json()["referenceOnly"] is True
    assert overview.json()["formulaPassRate"] == "1.000000"
    assert overview.json()["certifiedPerformanceAvailable"] is False
    assert people.status_code == 200
    assert people.json()["rows"][0]["productCount"] == 1
    assert people.json()["rows"][0]["storeProfit"] == "50.0000"

    policy_id = ensure_builtin_performance_policy(
        workbench,
        enterprise_id=enterprise_id,
        effective_from=date(2026, 2, 1),
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            UPDATE canonical_product
            SET status = 'active'
            WHERE enterprise_id = ? AND merchant_product_code = 'SKU-1'
            """,
            [enterprise_id],
        )
        person_id = str(
            memory.fetchone_required(
                """
                SELECT person_id FROM person_identity
                WHERE enterprise_id = ? AND status = 'active'
                ORDER BY person_id LIMIT 1
                """,
                [enterprise_id],
            )[0]
        )
        product_id = str(
            memory.fetchone_required(
                """
                SELECT product_id FROM canonical_product
                WHERE enterprise_id = ? AND status = 'active'
                ORDER BY product_id LIMIT 1
                """,
                [enterprise_id],
            )[0]
        )
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
                VALUES (
                    'contract-certified', 'contract-certified', ?,
                    'store-certified', 'taobao', 2, DATE '2026-02-01',
                'active', '{"store_name":"测试店铺"}'
            )
            """,
            [enterprise_id],
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status
            )
            VALUES (
                'period-certified', 'contract-certified', 'store-certified',
                DATE '2026-02-01', DATE '2026-02-28', 'open'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
                VALUES (
                    'contract-certified-retired', 'contract-certified', ?,
                    'store-certified', 'taobao', 1, DATE '2026-02-01',
                'retired', '{"store_name":"测试店铺"}'
            )
            """,
            [enterprise_id],
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status
            )
            VALUES (
                'period-certified-retired', 'contract-certified-retired',
                'store-certified', DATE '2026-02-01',
                DATE '2026-02-28', 'open'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES (
                'run-certified', 'contract-certified', 'period-certified',
                'reconcile', 'succeeded'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO performance_result (
                result_id, run_id, enterprise_id, period_id, person_id,
                store_id, product_id, policy_version_id, collected_amount,
                refund_amount, direct_cost, allocated_cost, operating_profit,
                completeness_ratio, status, evidence_policy_version,
                engine_version, evidence_json, checksum_sha256
            )
            VALUES (
                'result-certified', 'run-certified', ?, 'period-certified', ?,
                'store-certified', ?, ?, 100, -10, -20, -5, 65, 1,
                'complete', ?, ?, '{}', repeat('a', 64)
            )
            """,
            [
                enterprise_id,
                person_id,
                product_id,
                policy_id,
                NORMALIZATION_RULE_VERSION,
                PERFORMANCE_ENGINE_VERSION,
            ],
        )
        memory.execute(
            """
            INSERT INTO performance_result_head (scope_key, result_id)
            VALUES ('performance-v1:certified-fixture', 'result-certified')
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
            VALUES (
                'contract-certified-2', 'contract-certified-2', ?,
                'store-certified-2', 'pinduoduo', 1, DATE '2026-02-01',
                'active', '{"store_name":"第二店"}'
            )
            """,
            [enterprise_id],
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status
            )
            VALUES (
                'period-certified-2', 'contract-certified-2',
                'store-certified-2', DATE '2026-02-01',
                DATE '2026-02-28', 'open'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO compute_job (
                job_id, cycle_id, job_kind, contract_id, period_id, store_id,
                period_token, status, progress_percent, business_label,
                finished_at, metrics_json
            )
            VALUES (
                'job-certified', 'cycle-fixture', 'reconcile',
                'contract-certified', 'period-certified', 'store-certified',
                '2602', 'succeeded', 100, '测试店铺 2602',
                current_timestamp, '{"performance":{"status":"certified"}}'
            )
            """
        )

    certified = client.get(
        "/api/v1/performance/overview",
        params={
            "calculationMode": "single",
            "period": "2602",
            "store": "测试店铺",
        },
    )
    wrong_period = client.get(
        "/api/v1/performance/overview",
        params={
            "calculationMode": "single",
            "period": "2603",
            "store": "测试店铺",
        },
    )
    wrong_store = client.get(
        "/api/v1/performance/overview",
        params={
            "calculationMode": "single",
            "period": "2602",
            "store": "另一店",
        },
    )
    partial_all_stores = client.get(
        "/api/v1/performance/overview",
        params={"calculationMode": "single", "period": "2602"},
    )
    assert certified.json()["certifiedPerformanceAvailable"] is True
    assert wrong_period.json()["certifiedPerformanceAvailable"] is False
    assert wrong_store.json()["certifiedPerformanceAvailable"] is False
    assert partial_all_stores.json()["certifiedPerformanceAvailable"] is False
    assert partial_all_stores.json()["engineGate"]["status"] == "waiting"
    assert partial_all_stores.json()["engineGate"]["details"] == {
        "scopeCount": 2,
        "certifiedScopeCount": 1,
        "waitingScopeCount": 1,
    }

    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES (
                'run-certified-2', 'contract-certified-2',
                'period-certified-2', 'reconcile', 'succeeded'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO performance_result (
                result_id, run_id, enterprise_id, period_id, person_id,
                store_id, product_id, policy_version_id, collected_amount,
                refund_amount, direct_cost, allocated_cost, operating_profit,
                completeness_ratio, status, evidence_policy_version,
                engine_version, evidence_json, checksum_sha256
            )
            VALUES (
                'result-certified-2', 'run-certified-2', ?,
                'period-certified-2', ?, 'store-certified-2', ?, ?,
                200, -20, -40, -10, 130, 1, 'complete', ?, ?, '{}',
                repeat('b', 64)
            )
            """,
            [
                enterprise_id,
                person_id,
                product_id,
                policy_id,
                NORMALIZATION_RULE_VERSION,
                PERFORMANCE_ENGINE_VERSION,
            ],
        )
        memory.execute(
            """
            INSERT INTO performance_result_head (scope_key, result_id)
            VALUES ('performance-v1:certified-fixture-2', 'result-certified-2')
            """
        )
        memory.execute(
            """
            INSERT INTO compute_job (
                job_id, cycle_id, job_kind, contract_id, period_id, store_id,
                period_token, status, progress_percent, business_label,
                finished_at, metrics_json
            )
            VALUES (
                'job-certified-2', 'cycle-fixture', 'reconcile',
                'contract-certified-2', 'period-certified-2',
                'store-certified-2', '2602', 'succeeded', 100, '第二店 2602',
                current_timestamp, '{"performance":{"status":"certified"}}'
            )
            """
        )
    complete_all_stores = client.get(
        "/api/v1/performance/overview",
        params={"calculationMode": "single", "period": "2602"},
    )
    assert complete_all_stores.json()["certifiedPerformanceAvailable"] is True
    assert complete_all_stores.json()["engineGate"]["status"] == "certified"
    assert complete_all_stores.json()["engineGate"]["details"] == {
        "scopeCount": 2,
        "certifiedScopeCount": 2,
    }


def test_reference_import_accepts_platform_specific_optional_columns_and_skips_store_summary(
    tmp_path: Path,
) -> None:
    detail_path = tmp_path / "抖店明细.csv"
    summary_path = tmp_path / "店铺汇总.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "2602姓名",
                "宝贝编码",
                "交易收款",
                "交易退款",
                "交易赔付",
                "软件服务费",
                "发货运费",
                "订单成本",
                "补发成本",
                "本金佣金",
                "店铺毛利",
                "广告费",
                "店铺利润",
            ]
        )
        writer.writerow(
            [
                "员工甲",
                "SKU-1",
                "100",
                "-10",
                "0",
                "-5",
                "-3",
                "-20",
                "0",
                "0",
                "62",
                "-12",
                "50",
            ]
        )
    with summary_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "年月",
                "交易收款",
                "交易退款",
                "店铺毛利",
                "广告费",
                "店铺利润",
            ]
        )
        writer.writerow(["202602", "100", "-10", "62", "-12", "50"])

    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        _snapshot(
            memory,
            snapshot_id="detail-snapshot",
            path=detail_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\工资\\2026"
                "\\阿里单算\\2月\\抖店明细.csv"
            ),
        )
        _snapshot(
            memory,
            snapshot_id="summary-snapshot",
            path=summary_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\工资\\2026"
                "\\阿里单算\\2月\\店铺汇总.csv"
            ),
        )

    result = sync_performance_sources(
        workbench,
        enterprise_id=stable_identity("enterprise", "local-enterprise"),
    )

    assert result.imported_snapshots == 2
    assert result.reference_rows == 1
    assert result.issue_count == 0
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT marketing_fee, procurement_amount, validation_status
            FROM performance_reference_fact
            """
        ).fetchone() == (0, 0, "passed")
        assert memory.execute(
            """
            SELECT status, row_count, issue_count
            FROM performance_source_import
            WHERE snapshot_id = 'summary-snapshot'
            """
        ).fetchone() == ("succeeded", 0, 0)


def test_reference_before_activation_is_preserved_as_snapshot_but_not_exposed(
    tmp_path: Path,
) -> None:
    reference_path = tmp_path / "一月历史.csv"
    with reference_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "2601姓名",
                "宝贝编码",
                "交易收款",
                "交易退款",
                "店铺毛利",
                "广告费",
                "店铺利润",
            ]
        )
        writer.writerow(["员工甲", "SKU-1", "100", "-10", "90", "-20", "70"])

    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        _snapshot(
            memory,
            snapshot_id="january-snapshot",
            path=reference_path,
            source_uri=(
                "finance-win-ro://D:\\KAOSHI\\OneDrive\\内贸\\工资\\2026"
                "\\阿里单算\\1月\\一月历史.csv"
            ),
        )

    result = sync_performance_sources(
        workbench,
        enterprise_id=stable_identity("enterprise", "local-enterprise"),
    )

    assert result.imported_snapshots == 1
    assert result.reference_rows == 0
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            "SELECT count(*) FROM source_snapshot"
        ).fetchone() == (1,)
        assert memory.execute(
            "SELECT count(*) FROM performance_reference_fact"
        ).fetchone() == (0,)
        assert memory.execute(
            """
            SELECT row_count, issue_count,
                   json_extract(metrics_json, '$.excluded_before_activation')
            FROM performance_source_import
            """
        ).fetchone() == (0, 0, "true")
