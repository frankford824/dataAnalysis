from __future__ import annotations

import json

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.trust import trust_matrix

ENTERPRISE = "enterprise-trust"


def _store(memory: DuckDBMemory, index: int, name: str) -> tuple[str, str, str]:
    contract_id = f"contract-{index}"
    store_id = f"store-{index}"
    period_id = f"period-{index}"
    memory.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        )
        VALUES (?, ?, ?, ?, 'taobao', 1, DATE '2026-02-01', 'active', ?)
        """,
        [
            contract_id,
            f"logical-{index}",
            ENTERPRISE,
            store_id,
            json.dumps({"store_name": name}, ensure_ascii=False),
        ],
    )
    memory.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        )
        VALUES (?, ?, ?, DATE '2026-05-01', DATE '2026-05-31', 'open')
        """,
        [period_id, contract_id, store_id],
    )
    return contract_id, store_id, period_id


def _run(
    memory: DuckDBMemory,
    index: int,
    contract_id: str,
    period_id: str,
    *,
    certifiable: bool,
) -> str:
    run_id = f"run-{index}"
    memory.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status,
            started_at, finished_at, metrics_json
        )
        VALUES (?, ?, ?, 'reconcile', 'succeeded',
                current_timestamp - INTERVAL 1 MINUTE, current_timestamp, ?)
        """,
        [
            run_id,
            contract_id,
            period_id,
            json.dumps({"certifiable": certifiable}),
        ],
    )
    return run_id


def _requirement(
    memory: DuckDBMemory,
    index: int,
    contract_id: str,
    period_id: str,
    run_id: str,
    status: str,
) -> None:
    requirement_id = f"requirement-{index}"
    memory.execute(
        """
        INSERT INTO checklist_requirement (
            requirement_id, contract_id, source_kind, store_scope, required,
            effective_from, expected_frequency
        )
        VALUES (?, ?, 'baobei_order', 'store', true, DATE '2026-02-01', 'monthly')
        """,
        [requirement_id, contract_id],
    )
    memory.execute(
        """
        INSERT INTO checklist_result (
            result_id, run_id, period_id, requirement_id, status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [f"result-{index}", run_id, period_id, requirement_id, status],
    )


def _balance(
    memory: DuckDBMemory,
    index: int,
    run_id: str,
    contract_id: str,
    period_id: str,
    *,
    difference: str,
    unresolved: bool,
) -> None:
    balance_id = f"balance-{index}"
    memory.execute(
        """
        INSERT INTO reconciliation_balance (
            balance_id, run_id, contract_id, period_id, balance_key,
            expected_amount, actual_amount, matched_amount, difference_amount,
            status, evidence_json
        )
        VALUES (?, ?, ?, ?, ?, 100.0000, 100.0000,
                cast(? AS DECIMAL(38,4)), cast(? AS DECIMAL(38,4)), ?, '[]')
        """,
        [
            balance_id,
            run_id,
            contract_id,
            period_id,
            f"key-{index}",
            "100" if not unresolved else "90",
            difference,
            "unresolved" if unresolved else "balanced",
        ],
    )
    if unresolved:
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            )
            VALUES (?, ?, 'amount_mismatch', cast(? AS DECIMAL(38,4)), 'open')
            """,
            [f"unresolved-{index}", balance_id, difference],
        )


def test_trust_matrix_translates_gates_into_business_states() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()

        contract, _, period = _store(memory, 1, "可用店")
        run = _run(memory, 1, contract, period, certifiable=True)
        _requirement(memory, 1, contract, period, run, "present")
        _balance(
            memory,
            1,
            run,
            contract,
            period,
            difference="0",
            unresolved=False,
        )

        contract, _, period = _store(memory, 2, "缺文件店")
        run = _run(memory, 2, contract, period, certifiable=False)
        _requirement(memory, 2, contract, period, run, "missing")

        contract, _, period = _store(memory, 3, "差额店")
        run = _run(memory, 3, contract, period, certifiable=False)
        _requirement(memory, 3, contract, period, run, "present")
        _balance(
            memory,
            3,
            run,
            contract,
            period,
            difference="12.34",
            unresolved=True,
        )

        contract, _, period = _store(memory, 4, "待确认店")
        run = _run(memory, 4, contract, period, certifiable=False)
        _requirement(memory, 4, contract, period, run, "present")

        result = trust_matrix(memory, enterprise_id=ENTERPRISE)

    states = {
        cell["storeName"]: cell["status"]
        for cell in result["cells"]  # type: ignore[index]
    }
    assert states == {
        "可用店": "usable",
        "缺文件店": "missing_sources",
        "差额店": "amount_mismatch",
        "待确认店": "waiting_review",
    }
    assert result["summary"] == {
        "storeCount": 4,
        "usableCount": 1,
        "attentionCount": 3,
        "missingSourceCount": 1,
        "amountMismatchCount": 1,
        "waitingReviewCount": 1,
        "processingCount": 0,
        "collectingCount": 0,
        "verdict": "当前有 3 家店需要关注。",
    }
    first = result["firstAttention"]
    assert first["storeName"] == "差额店"  # type: ignore[index]
    assert first["firstReviewId"] == "unresolved-3"  # type: ignore[index]
    assert "¥12.34" in first["explanation"]["happened"]  # type: ignore[index]
    assert first["facts"]["lastCalculatedAt"].endswith("+00:00")  # type: ignore[index]


def test_trust_matrix_never_calls_empty_workspace_trustworthy() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        result = trust_matrix(memory, enterprise_id=ENTERPRISE)

    assert result["cells"] == []
    assert result["summary"]["verdict"] == "当前还没有形成可核验的店铺月份。"  # type: ignore[index]


def test_trust_matrix_explains_no_source_without_claiming_zero_files_missing() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _store(memory, 1, "尚无文件店")
        result = trust_matrix(memory, enterprise_id=ENTERPRISE)

    cell = result["cells"][0]  # type: ignore[index]
    assert cell["status"] == "missing_sources"
    assert cell["explanation"]["happened"] == "本月还没有找到可用于整理的原始文件。"
    assert "0 项" not in cell["explanation"]["happened"]


def test_trust_matrix_uses_period_checklist_not_reconcile_run_id() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        contract, _, period = _store(memory, 1, "冻结清单店")
        reconcile_run = _run(
            memory,
            1,
            contract,
            period,
            certifiable=False,
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES ('freeze-checklist', ?, ?, 'freeze', 'succeeded')
            """,
            [contract, period],
        )
        _requirement(
            memory,
            1,
            contract,
            period,
            "freeze-checklist",
            "missing",
        )
        memory.execute(
            """
            INSERT INTO checklist_requirement (
                requirement_id, contract_id, source_kind, store_scope,
                required, effective_from, expected_frequency
            )
            VALUES (
                'optional-ad', ?, 'advertising', 'store', false,
                DATE '2026-02-01', 'monthly'
            )
            """,
            [contract],
        )
        memory.execute(
            """
            INSERT INTO checklist_result (
                result_id, run_id, period_id, requirement_id, status
            )
            VALUES (
                'optional-ad-result', 'freeze-checklist', ?,
                'optional-ad', 'not_applicable'
            )
            """,
            [period],
        )

        result = trust_matrix(memory, enterprise_id=ENTERPRISE)

    cell = result["cells"][0]  # type: ignore[index]
    assert cell["runId"] == reconcile_run
    assert cell["status"] == "missing_sources"
    assert cell["facts"]["requiredSourceCount"] == 1
    assert cell["facts"]["missingSourceCount"] == 1


def test_current_open_month_without_files_is_collecting_not_missing() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        contract, _, period = _store(memory, 1, "本月店")
        memory.execute(
            """
            UPDATE accounting_period
            SET period_start = cast(date_trunc('month', current_date) AS DATE),
                period_end = last_day(current_date)
            WHERE period_id = ?
            """,
            [period],
        )
        result = trust_matrix(memory, enterprise_id=ENTERPRISE)

    cell = result["cells"][0]  # type: ignore[index]
    assert cell["status"] == "collecting"
    assert cell["statusLabel"] == "本月进行中"
    assert result["summary"]["missingSourceCount"] == 0  # type: ignore[index]
    assert result["summary"]["collectingCount"] == 1  # type: ignore[index]
