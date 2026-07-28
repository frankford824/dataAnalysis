from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness import performance_engine as performance_engine_module
from commerce_harness.config import load_config
from commerce_harness.evidence_policy import NORMALIZATION_RULE_VERSION
from commerce_harness.memory import database as database_module
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.performance_engine import (
    PerformanceBlockCode,
    PerformanceCalculationBlocked,
    builtin_performance_policy_checksum,
    builtin_performance_policy_definition,
    calculate_certified_performance,
    ensure_builtin_performance_policy,
)
from commerce_harness.workbench import WorkbenchPaths, initialize

ENTERPRISE_ID = "enterprise-1"
PERIOD_ID = "period-2602"
STORE_ID = "store-1"
PRODUCT_ID = "product-1"
POLICY_ID = "policy-1"

PNL_VALUES = {
    "sales": Decimal("1000.0000"),
    "refund": Decimal("-100.0000"),
    "platform_fee": Decimal("-50.0000"),
    "freight": Decimal("-20.0000"),
    "cost": Decimal("-300.0000"),
    "advertising": Decimal("-80.0000"),
    "profit": Decimal("450.0000"),
}


def _workbench(tmp_path: Path) -> WorkbenchPaths:
    return initialize(load_config(workspace=tmp_path / "workbench"))


def _insert_snapshot(
    memory: DuckDBMemory,
    snapshot_id: str,
    *,
    digest: str,
) -> None:
    memory.execute(
        """
        INSERT INTO source_snapshot (
            snapshot_id, content_sha256, byte_size, object_uri, source_uri,
            original_name, captured_at, manifest_json
        )
        VALUES (?, ?, 1, ?, ?, ?, current_timestamp, '{}')
        """,
        [
            snapshot_id,
            digest,
            f"/immutable/{snapshot_id}",
            f"fixture://{snapshot_id}",
            f"{snapshot_id}.xlsx",
        ],
    )


def _insert_run_and_pnl(
    memory: DuckDBMemory,
    *,
    run_id: str,
    values: dict[str, Decimal] | None = None,
    certifiable: bool = True,
    product_grain: bool = True,
    evidence_snapshot_id: str = "snapshot-pnl",
    finished_at: str = "2026-03-01 01:00:00+00",
) -> None:
    memory.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status, code_sha,
            input_manifest_sha256, rule_set_sha256, started_at, finished_at,
            metrics_json
        )
        VALUES (?, 'contract-1', ?, 'reconcile', 'succeeded', 'code-sha',
                'input-sha', 'rule-sha', ?, ?, ?)
        """,
        [
            run_id,
            PERIOD_ID,
            finished_at,
            finished_at,
            json.dumps({"certifiable": certifiable}),
        ],
    )
    sku_key = "SKU-1" if product_grain else "__store_total__"
    for row_no, (metric, value) in enumerate(
        (values or PNL_VALUES).items(),
        start=2,
    ):
        memory.execute(
            """
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, 'certified-product-pnl-v1', ?, ?)
            """,
            [
                f"{run_id}:{metric}",
                run_id,
                PERIOD_ID,
                STORE_ID,
                sku_key,
                metric,
                value,
                json.dumps(
                    [
                        {
                            "file_id": evidence_snapshot_id,
                            "source_sheet": "订单",
                            "row_no": row_no,
                            "field": metric,
                            "rule_version": NORMALIZATION_RULE_VERSION,
                        }
                    ]
                ),
            ],
        )


def _insert_assignment(
    memory: DuckDBMemory,
    *,
    assignment_id: str,
    person_id: str,
    ratio: str,
    effective_from: str = "2026-02-01",
    effective_to: str = "2026-02-28",
    version: int = 1,
    status: str = "active",
    store_id: str | None = STORE_ID,
    store_name: str | None = None,
) -> None:
    memory.execute(
        """
        INSERT INTO responsibility_assignment_version (
            assignment_id, enterprise_id, person_id, product_id, store_id,
            store_name, allocation_ratio, effective_from, effective_to,
            version, status, source_kind, source_snapshot_id, source_sheet,
            source_row_no, checksum_sha256
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fixture',
                'snapshot-assignment', '负责人', ?, ?)
        """,
        [
            assignment_id,
            ENTERPRISE_ID,
            person_id,
            PRODUCT_ID,
            store_id,
            store_name,
            ratio,
            effective_from,
            effective_to,
            version,
            status,
            10 + version,
            f"checksum-{assignment_id}",
        ],
    )


def _seed(
    workbench: WorkbenchPaths,
    *,
    with_policy: bool = True,
    with_assignments: bool = True,
    certifiable: bool = True,
    product_grain: bool = True,
    period_status: str = "open",
) -> None:
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        _insert_snapshot(memory, "snapshot-pnl", digest="1" * 64)
        _insert_snapshot(memory, "snapshot-assignment", digest="2" * 64)
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
            VALUES (
                'contract-1', 'contract-logical-1', ?, ?, 'taobao', 1,
                DATE '2026-02-01', 'active', '{"store_name": "测试店铺"}'
            )
            """,
            [ENTERPRISE_ID, STORE_ID],
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status, revision_no
            )
            VALUES (
                ?, 'contract-1', ?, DATE '2026-02-01', DATE '2026-02-28',
                ?, 1
            )
            """,
            [PERIOD_ID, STORE_ID, period_status],
        )
        for person_id, name in (("person-a", "员工甲"), ("person-b", "员工乙")):
            memory.execute(
                """
                INSERT INTO person_identity (
                    person_id, enterprise_id, display_name, status,
                    identity_checksum
                )
                VALUES (?, ?, ?, 'active', ?)
                """,
                [person_id, ENTERPRISE_ID, name, f"identity-{person_id}"],
            )
        memory.execute(
            """
            INSERT INTO canonical_product (
                product_id, enterprise_id, merchant_product_code, status
            )
            VALUES (?, ?, 'SKU-1', 'active')
            """,
            [PRODUCT_ID, ENTERPRISE_ID],
        )
        if with_policy:
            definition = builtin_performance_policy_definition()
            memory.execute(
                """
                INSERT INTO performance_policy_version (
                    policy_version_id, enterprise_id, policy_code, version,
                    effective_from, status, definition_json, checksum_sha256,
                    approved_by, approved_at
                )
                VALUES (?, ?, 'certified_product_performance', 1,
                        DATE '2026-02-01', 'approved', ?, ?, 'finance-owner',
                        current_timestamp)
                """,
                [
                    POLICY_ID,
                    ENTERPRISE_ID,
                    json.dumps(definition, ensure_ascii=False, sort_keys=True),
                    builtin_performance_policy_checksum(),
                ],
            )
        if with_assignments:
            _insert_assignment(
                memory,
                assignment_id="assignment-a",
                person_id="person-a",
                ratio="0.6000",
            )
            _insert_assignment(
                memory,
                assignment_id="assignment-b",
                person_id="person-b",
                ratio="0.4000",
            )
        _insert_run_and_pnl(
            memory,
            run_id="run-1",
            certifiable=certifiable,
            product_grain=product_grain,
        )


def _blocked(
    workbench: WorkbenchPaths,
    code: PerformanceBlockCode,
) -> PerformanceCalculationBlocked:
    with pytest.raises(PerformanceCalculationBlocked) as caught:
        calculate_certified_performance(
            workbench,
            enterprise_id=ENTERPRISE_ID,
            period_id=PERIOD_ID,
        )
    assert caught.value.code == code
    return caught.value


def test_calculates_decimal_traceable_results_and_is_idempotent(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)

    first = calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )
    second = calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )

    assert first.result_count == 2
    assert first.created_count == 2
    assert first.superseded_count == 0
    assert not first.idempotent
    assert second.result_ids == first.result_ids
    assert second.created_count == 0
    assert second.idempotent
    assert second.batch_checksum_sha256 == first.batch_checksum_sha256

    with DuckDBMemory(workbench.database) as memory:
        rows = memory.execute(
            """
            SELECT person_id, collected_amount, refund_amount, direct_cost,
                   allocated_cost, operating_profit, completeness_ratio,
                   status, checksum_sha256, evidence_json
            FROM performance_result
            ORDER BY person_id
            """
        ).fetchall()
        assert rows[0][:8] == (
            "person-a",
            Decimal("600.0000"),
            Decimal("-60.0000"),
            Decimal("-180.0000"),
            Decimal("-90.0000"),
            Decimal("270.0000"),
            Decimal("1.000000"),
            "complete",
        )
        assert rows[1][:8] == (
            "person-b",
            Decimal("400.0000"),
            Decimal("-40.0000"),
            Decimal("-120.0000"),
            Decimal("-60.0000"),
            Decimal("180.0000"),
            Decimal("1.000000"),
            "complete",
        )
        assert len(rows[0][8]) == 64
        evidence = json.loads(rows[0][9])
        assert evidence["result_version"] == 1
        assert evidence["certified_run"] == {
            "run_id": "run-1",
            "code_sha": "code-sha",
            "input_manifest_sha256": "input-sha",
            "rule_set_sha256": "rule-sha",
        }
        assert evidence["assignments"][0]["assignment_id"] == "assignment-a"
        assert evidence["assignments"][0]["source"]["snapshot_id"] == (
            "snapshot-assignment"
        )
        assert evidence["pnl_cells"][0]["sources"][0]["snapshot_id"] == (
            "snapshot-pnl"
        )
        assert memory.execute(
            """
            SELECT sum(collected_amount), sum(operating_profit)
            FROM performance_result_head head
            JOIN performance_result result ON result.result_id = head.result_id
            """
        ).fetchone() == (Decimal("1000.0000"), Decimal("450.0000"))


def test_open_period_new_certified_head_supersedes_previous_version(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    first = calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )
    revised = dict(PNL_VALUES)
    revised["sales"] = Decimal("1100.0000")
    revised["profit"] = Decimal("550.0000")
    with DuckDBMemory(workbench.database) as memory:
        _insert_run_and_pnl(
            memory,
            run_id="run-2",
            values=revised,
            finished_at="2026-03-02 01:00:00+00",
        )

    second = calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )

    assert second.result_ids != first.result_ids
    assert second.created_count == 2
    assert second.superseded_count == 2
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT status, count(*) FROM performance_result
            GROUP BY status ORDER BY status
            """
        ).fetchall() == [("complete", 2), ("superseded", 2)]
        head_rows = memory.execute(
            """
            SELECT collected_amount, operating_profit, evidence_json
            FROM performance_result_head head
            JOIN performance_result result ON result.result_id = head.result_id
            ORDER BY person_id
            """
        ).fetchall()
        assert [row[:2] for row in head_rows] == [
            (Decimal("660.0000"), Decimal("330.0000")),
            (Decimal("440.0000"), Decimal("220.0000")),
        ]
        assert all(json.loads(row[2])["result_version"] == 2 for row in head_rows)


def test_schema_upgrade_invalidates_obsolete_performance_head(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            UPDATE performance_result
            SET engine_version = 'certified-person-performance-v1'
            """
        )

    database_module._INITIALIZED_DATABASES.discard(  # noqa: SLF001
        str(workbench.database.resolve())
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        assert memory.execute(
            "SELECT count(*) FROM performance_result_head"
        ).fetchone() == (0,)
        assert memory.execute(
            "SELECT distinct status FROM performance_result"
        ).fetchall() == [("superseded",)]


def test_builtin_policy_revision_is_atomic_and_gets_new_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbench = _workbench(tmp_path)
    first = ensure_builtin_performance_policy(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        effective_from=date(2026, 2, 1),
    )
    revised = builtin_performance_policy_definition()
    revised["revision_note"] = "evidence-completeness-gate"
    monkeypatch.setattr(
        performance_engine_module,
        "builtin_performance_policy_definition",
        lambda: revised,
    )

    second = ensure_builtin_performance_policy(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        effective_from=date(2026, 2, 1),
    )

    assert second != first
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT version, status
            FROM performance_policy_version
            ORDER BY version
            """
        ).fetchall() == [(1, "retired"), (2, "approved")]


def test_builtin_policy_extends_to_an_earlier_backfilled_period(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    march_policy = ensure_builtin_performance_policy(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        effective_from=date(2026, 3, 1),
    )

    february_policy = ensure_builtin_performance_policy(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        effective_from=date(2026, 2, 1),
    )

    assert february_policy != march_policy
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT version, effective_from, status, checksum_sha256
            FROM performance_policy_version
            ORDER BY version
            """
        ).fetchall() == [
            (
                1,
                date(2026, 3, 1),
                "retired",
                builtin_performance_policy_checksum(),
            ),
            (
                2,
                date(2026, 2, 1),
                "approved",
                builtin_performance_policy_checksum(),
            ),
        ]


def test_rounding_preserves_each_person_formula_and_store_total(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    tiny_values = {
        "sales": Decimal("0.0001"),
        "refund": Decimal("-0.0002"),
        "platform_fee": Decimal("0.0000"),
        "freight": Decimal("0.0000"),
        "cost": Decimal("0.0000"),
        "advertising": Decimal("0.0000"),
        "profit": Decimal("-0.0001"),
    }
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            UPDATE responsibility_assignment_version
            SET allocation_ratio = 0.5000
            """
        )
        for metric, value in tiny_values.items():
            memory.execute(
                """
                UPDATE pnl_cell SET value = ?
                WHERE run_id = 'run-1' AND metric = ?
                """,
                [value, metric],
            )

    calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )

    with DuckDBMemory(workbench.database) as memory:
        rows = memory.execute(
            """
            SELECT collected_amount, refund_amount, direct_cost,
                   allocated_cost, operating_profit
            FROM performance_result
            ORDER BY person_id
            """
        ).fetchall()
    assert all(sum(row[:4], Decimal("0.0000")) == row[4] for row in rows)
    assert sum((row[4] for row in rows), Decimal("0.0000")) == Decimal("-0.0001")


def test_locked_period_blocks_initial_or_changed_publication(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench, period_status="closed")
    _blocked(workbench, PerformanceBlockCode.LOCKED_PERIOD_CHANGE)
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            "SELECT count(*) FROM performance_result"
        ).fetchone() == (0,)
        assert memory.execute(
            "SELECT count(*) FROM performance_result_head"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("seed_options", "expected_code"),
    [
        (
            {"with_policy": False},
            PerformanceBlockCode.POLICY_MISSING,
        ),
        (
            {"certifiable": False},
            PerformanceBlockCode.UNCERTIFIED_INPUT,
        ),
        (
            {"product_grain": False},
            PerformanceBlockCode.PRODUCT_GRAIN_MISSING,
        ),
        (
            {"with_assignments": False},
            PerformanceBlockCode.ASSIGNMENT_MISSING,
        ),
    ],
)
def test_blocks_missing_prerequisites(
    tmp_path: Path,
    seed_options: dict[str, bool],
    expected_code: PerformanceBlockCode,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(
        workbench,
        with_policy=seed_options.get("with_policy", True),
        with_assignments=seed_options.get("with_assignments", True),
        certifiable=seed_options.get("certifiable", True),
        product_grain=seed_options.get("product_grain", True),
    )
    _blocked(workbench, expected_code)


def test_provisional_people_and_products_never_enter_certified_performance(
    tmp_path: Path,
) -> None:
    provisional_person = _workbench(tmp_path / "person")
    _seed(provisional_person)
    with DuckDBMemory(provisional_person.database) as memory:
        memory.execute("UPDATE person_identity SET status = 'provisional'")
    _blocked(provisional_person, PerformanceBlockCode.ASSIGNMENT_MISSING)

    provisional_product = _workbench(tmp_path / "product")
    _seed(provisional_product)
    with DuckDBMemory(provisional_product.database) as memory:
        memory.execute("UPDATE canonical_product SET status = 'provisional'")
    _blocked(provisional_product, PerformanceBlockCode.PRODUCT_IDENTITY_MISSING)


def test_historical_reference_cannot_seed_certified_assignment(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            INSERT INTO performance_source_import (
                import_id, enterprise_id, snapshot_id, source_kind,
                status, row_count, issue_count, finished_at
            )
            VALUES (
                'reference-import', ?, 'snapshot-assignment',
                'performance_reference', 'succeeded', 2, 0, current_timestamp
            )
            """,
            [ENTERPRISE_ID],
        )
    _blocked(workbench, PerformanceBlockCode.ASSIGNMENT_MISSING)


def test_latest_successful_run_cannot_fall_back_to_older_certified_run(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with DuckDBMemory(workbench.database) as memory:
        _insert_run_and_pnl(
            memory,
            run_id="run-2",
            certifiable=False,
            finished_at="2026-03-02 01:00:00+00",
        )
    blocked = _blocked(workbench, PerformanceBlockCode.UNCERTIFIED_INPUT)
    assert blocked.details["run_id"] == "run-2"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_cost", PerformanceBlockCode.COST_COVERAGE_INSUFFICIENT),
        ("missing_sales", PerformanceBlockCode.PRODUCT_METRIC_MISSING),
        ("profit_mismatch", PerformanceBlockCode.PRODUCT_PROFIT_MISMATCH),
        ("missing_product", PerformanceBlockCode.PRODUCT_IDENTITY_MISSING),
        ("missing_evidence", PerformanceBlockCode.EVIDENCE_MISSING),
    ],
)
def test_blocks_incomplete_or_untraceable_product_inputs(
    tmp_path: Path,
    mutation: str,
    expected_code: PerformanceBlockCode,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with DuckDBMemory(workbench.database) as memory:
        if mutation == "missing_cost":
            memory.execute(
                "DELETE FROM pnl_cell WHERE run_id = 'run-1' AND metric = 'cost'"
            )
        elif mutation == "missing_sales":
            memory.execute(
                "DELETE FROM pnl_cell WHERE run_id = 'run-1' AND metric = 'sales'"
            )
        elif mutation == "profit_mismatch":
            memory.execute(
                """
                UPDATE pnl_cell SET value = 451.0000
                WHERE run_id = 'run-1' AND metric = 'profit'
                """
            )
        elif mutation == "missing_product":
            memory.execute("UPDATE canonical_product SET status = 'inactive'")
        else:
            memory.execute(
                """
                UPDATE pnl_cell SET evidence_json = '[]'
                WHERE run_id = 'run-1' AND metric = 'sales'
                """
            )
    _blocked(workbench, expected_code)


def test_old_or_sheetless_evidence_never_enters_certified_performance(
    tmp_path: Path,
) -> None:
    old_binding = _workbench(tmp_path / "old-binding")
    _seed(old_binding)
    with DuckDBMemory(old_binding.database) as memory:
        memory.execute(
            """
            UPDATE pnl_cell
            SET evidence_json = replace(
                evidence_json, ?, 'finite-normalization-v4'
            )
            WHERE run_id = 'run-1'
            """,
            [NORMALIZATION_RULE_VERSION],
        )
    _blocked(old_binding, PerformanceBlockCode.EVIDENCE_MISSING)

    sheetless_pnl = _workbench(tmp_path / "sheetless-pnl")
    _seed(sheetless_pnl)
    with DuckDBMemory(sheetless_pnl.database) as memory:
        memory.execute(
            """
            UPDATE pnl_cell SET evidence_json = ?
            WHERE run_id = 'run-1' AND metric = 'sales'
            """,
            [
                json.dumps(
                    [
                        {
                            "file_id": "snapshot-pnl",
                            "row_no": 2,
                            "field": "sales",
                            "rule_version": NORMALIZATION_RULE_VERSION,
                        }
                    ]
                )
            ],
        )
    _blocked(sheetless_pnl, PerformanceBlockCode.EVIDENCE_MISSING)

    sheetless_assignment = _workbench(tmp_path / "sheetless-assignment")
    _seed(sheetless_assignment)
    with DuckDBMemory(sheetless_assignment.database) as memory:
        memory.execute(
            "UPDATE responsibility_assignment_version SET source_sheet = NULL"
        )
    _blocked(sheetless_assignment, PerformanceBlockCode.EVIDENCE_MISSING)

    partially_valid = _workbench(tmp_path / "partially-valid")
    _seed(partially_valid)
    with DuckDBMemory(partially_valid.database) as memory:
        memory.execute(
            """
            UPDATE pnl_cell SET evidence_json = ?
            WHERE run_id = 'run-1' AND metric = 'sales'
            """,
            [
                json.dumps(
                    [
                        {
                            "file_id": "snapshot-pnl",
                            "source_sheet": "订单",
                            "row_no": 2,
                            "field": "sales",
                            "rule_version": NORMALIZATION_RULE_VERSION,
                        },
                        {
                            "file_id": "snapshot-pnl",
                            "source_sheet": "订单",
                            "row_no": 3,
                            "field": "sales",
                            "rule_version": "finite-normalization-v4",
                        },
                    ]
                )
            ],
        )
    blocked = _blocked(partially_valid, PerformanceBlockCode.EVIDENCE_MISSING)
    assert blocked.details["reason"] == "obsolete_evidence_version"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("conflict", PerformanceBlockCode.ASSIGNMENT_CONFLICT),
        ("ratio", PerformanceBlockCode.ASSIGNMENT_RATIO_INVALID),
        ("split", PerformanceBlockCode.ASSIGNMENT_PERIOD_SPLIT),
    ],
)
def test_blocks_unsafe_responsibility_assignments(
    tmp_path: Path,
    mutation: str,
    expected_code: PerformanceBlockCode,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with DuckDBMemory(workbench.database) as memory:
        if mutation == "conflict":
            memory.execute(
                """
                UPDATE responsibility_assignment_version
                SET status = 'conflict'
                WHERE assignment_id = 'assignment-a'
                """
            )
        elif mutation == "ratio":
            memory.execute(
                """
                UPDATE responsibility_assignment_version
                SET allocation_ratio = 0.5000
                WHERE assignment_id = 'assignment-a'
                """
            )
        else:
            memory.execute(
                "DELETE FROM responsibility_assignment_version"
            )
            _insert_assignment(
                memory,
                assignment_id="assignment-a-early",
                person_id="person-a",
                ratio="1.0000",
                effective_to="2026-02-14",
            )
            _insert_assignment(
                memory,
                assignment_id="assignment-b-late",
                person_id="person-b",
                ratio="1.0000",
                effective_from="2026-02-15",
                version=2,
            )
    _blocked(workbench, expected_code)


def test_effective_date_rollover_with_same_owner_is_traceable(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench, with_assignments=False)
    with DuckDBMemory(workbench.database) as memory:
        _insert_assignment(
            memory,
            assignment_id="assignment-v1",
            person_id="person-a",
            ratio="1.0000",
            effective_to="2026-02-14",
        )
        _insert_assignment(
            memory,
            assignment_id="assignment-v2",
            person_id="person-a",
            ratio="1.0000",
            effective_from="2026-02-15",
            version=2,
        )

    result = calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )

    assert result.result_count == 1
    with DuckDBMemory(workbench.database) as memory:
        row = memory.execute(
            "SELECT evidence_json FROM performance_result"
        ).fetchone()
        assert row is not None
        evidence = json.loads(row[0])
    assert [
        assignment["assignment_id"] for assignment in evidence["assignments"]
    ] == ["assignment-v1", "assignment-v2"]


def test_store_specific_assignment_wins_over_generic_assignment(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench, with_assignments=False)
    with DuckDBMemory(workbench.database) as memory:
        _insert_assignment(
            memory,
            assignment_id="assignment-generic",
            person_id="person-b",
            ratio="1.0000",
            store_id=None,
        )
        _insert_assignment(
            memory,
            assignment_id="assignment-specific",
            person_id="person-a",
            ratio="1.0000",
        )

    calculate_certified_performance(
        workbench,
        enterprise_id=ENTERPRISE_ID,
        period_id=PERIOD_ID,
    )

    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            "SELECT person_id FROM performance_result"
        ).fetchone() == ("person-a",)


def test_policy_checksum_or_definition_drift_is_blocked(
    tmp_path: Path,
) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with DuckDBMemory(workbench.database) as memory:
        definition = builtin_performance_policy_definition()
        definition["minimum_cost_coverage_ratio"] = "0.800000"
        memory.execute(
            """
            UPDATE performance_policy_version
            SET definition_json = ?, checksum_sha256 = ?
            WHERE policy_version_id = ?
            """,
            [
                json.dumps(definition, sort_keys=True),
                builtin_performance_policy_checksum(),
                POLICY_ID,
            ],
        )
    _blocked(workbench, PerformanceBlockCode.POLICY_DRIFT)


def test_cross_enterprise_period_access_is_rejected(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    _seed(workbench)
    with pytest.raises(PerformanceCalculationBlocked) as caught:
        calculate_certified_performance(
            workbench,
            enterprise_id="enterprise-other",
            period_id=PERIOD_ID,
        )
    assert caught.value.code == PerformanceBlockCode.PERIOD_ENTERPRISE_MISMATCH


def test_period_must_exist(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    with pytest.raises(PerformanceCalculationBlocked) as caught:
        calculate_certified_performance(
            workbench,
            enterprise_id=ENTERPRISE_ID,
            period_id="missing-period",
        )
    assert caught.value.code == PerformanceBlockCode.PERIOD_NOT_FOUND
    assert caught.value.details == {"period_id": "missing-period"}
