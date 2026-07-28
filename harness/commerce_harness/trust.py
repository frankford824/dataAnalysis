from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

_ZERO = Decimal("0")
_STATUS_PRIORITY = {
    "amount_mismatch": 0,
    "missing_sources": 1,
    "waiting_review": 2,
    "processing": 3,
    "collecting": 4,
    "usable": 5,
}


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return _ZERO


def _store_name(definition: object, fallback: str) -> str:
    try:
        payload = json.loads(str(definition or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if isinstance(payload, dict):
        value = payload.get("store_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _iso_timestamp(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    text = str(value)
    if len(text) >= 3 and text[-3] in {"+", "-"} and text[-2:].isdigit():
        return f"{text}:00"
    return text


def _check(
    key: str,
    label: str,
    state: str,
    explanation: str,
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "state": state,
        "explanation": explanation,
    }


def _business_copy(
    *,
    status: str,
    missing_count: int,
    failed_count: int,
    unresolved_count: int,
    difference_amount: Decimal,
) -> tuple[str, str, str, str]:
    if status == "processing":
        return (
            "系统正在整理这个月份的原始记录。",
            "整理完成前，本月数字可能继续变化。",
            "暂时不需要操作，完成后系统会重新判断。",
            "完成后会自动更新本月状态和需要关注的事项。",
        )
    if status == "collecting":
        return (
            "本月仍在进行，来源文件尚未到约定的月结检查时间。",
            "当前数字只反映已经收到的记录，不能当作完整月度结果。",
            "暂时不需要补文件；系统会继续只读检查新增记录。",
            "月结后会按平台合同判断文件是否齐全，再进行金额核对。",
        )
    if status == "missing_sources":
        count = missing_count + failed_count
        if count == 0:
            return (
                "本月还没有找到可用于整理的原始文件。",
                "没有原始记录时，销售、退款、费用和利润都不能判断。",
                "检查数据目录中是否已有这家店本月的订单或账单文件。",
                "找到文件并完成整理后，系统会自动核对本月金额。",
            )
        return (
            f"本月还有 {count} 项需要的文件没有准备好。",
            "缺少文件时，销售、退款、费用或利润可能不完整。",
            "先补齐系统提示的本月文件。",
            "文件处理完成后，系统会自动重新核对金额。",
        )
    if status == "amount_mismatch":
        return (
            (
                f"已有文件之间还有 {unresolved_count} 笔记录没有对应上，"
                f"差额合计 ¥{difference_amount:,.2f}。"
            ),
            "相关店铺和月份的经营金额暂时不能作为正式结果。",
            "从金额最大的疑问开始，查看原始记录并确认原因。",
            "确认、补文件或重新计算后，差额会重新汇总。",
        )
    if status == "waiting_review":
        return (
            "文件和计算已经完成，但还有业务情况需要确认。",
            "确认前系统不会把本月标记为可直接使用。",
            "按系统给出的业务问题逐项确认，不需要判断技术规则。",
            "所有必要问题确认后，系统会重新检查是否可以使用。",
        )
    return (
        "本月已配置的文件、金额核对和必要确认都已通过。",
        "这些数字可以用于当前经营查看。",
        "不需要额外操作；如需抽查，可打开任意数字查看原始依据。",
        "后续收到修订文件时，系统会保留旧证据并重新核对。",
    )


def trust_matrix(
    database: Any,
    *,
    enterprise_id: str,
) -> dict[str, object]:
    records = _rows(
        database.execute(
            """
            WITH prepared_periods AS (
                SELECT
                    period.period_id,
                    period.store_id,
                    period.period_start,
                    period.period_end,
                    period.status AS period_state,
                    period.revision_no,
                    contract.contract_id,
                    contract.platform_code,
                    contract.definition_json,
                    row_number() OVER (
                        PARTITION BY
                            contract.enterprise_id,
                            period.store_id,
                            period.period_start
                        ORDER BY
                            period.revision_no DESC,
                            contract.contract_version DESC,
                            contract.created_at DESC,
                            contract.contract_id DESC
                    ) AS period_position
                FROM accounting_period period
                JOIN reconciliation_contract contract
                  ON contract.contract_id = period.contract_id
                WHERE contract.enterprise_id = ?
                  AND contract.status = 'active'
            ),
            current_periods AS (
                SELECT * EXCLUDE (period_position)
                FROM prepared_periods
                WHERE period_position = 1
            ),
            ranked_runs AS (
                SELECT
                    run.*,
                    row_number() OVER (
                        PARTITION BY run.period_id
                        ORDER BY
                            run.finished_at DESC NULLS LAST,
                            run.started_at DESC,
                            run.run_id DESC
                    ) AS run_position
                FROM run_log run
                JOIN current_periods period
                  ON period.period_id = run.period_id
                WHERE run.run_kind = 'reconcile'
                  AND run.status = 'succeeded'
            ),
            latest_runs AS (
                SELECT * EXCLUDE (run_position)
                FROM ranked_runs
                WHERE run_position = 1
            ),
            ranked_checklist AS (
                SELECT
                    result.period_id,
                    result.requirement_id,
                    result.status,
                    row_number() OVER (
                        PARTITION BY result.period_id, result.requirement_id
                        ORDER BY result.checked_at DESC, result.result_id DESC
                    ) AS result_position
                FROM checklist_result result
            ),
            checklist AS (
                SELECT
                    period.period_id,
                    count(*) AS relevant_count,
                    count(*) FILTER (
                        WHERE result.status = 'present'
                    ) AS present_count,
                    count(*) FILTER (
                        WHERE coalesce(result.status, 'missing') = 'missing'
                    ) AS missing_count,
                    count(*) FILTER (
                        WHERE result.status = 'failed'
                    ) AS failed_count,
                    count(*) FILTER (
                        WHERE result.status = 'pending'
                    ) AS pending_count
                FROM current_periods period
                JOIN checklist_requirement requirement
                  ON requirement.contract_id = period.contract_id
                 AND requirement.required = true
                 AND requirement.effective_from <= period.period_end
                 AND (
                     requirement.effective_to IS NULL
                     OR requirement.effective_to >= period.period_start
                 )
                LEFT JOIN ranked_checklist result
                  ON result.period_id = period.period_id
                 AND result.requirement_id = requirement.requirement_id
                 AND result.result_position = 1
                GROUP BY period.period_id
            ),
            unresolved AS (
                SELECT
                    balance.run_id,
                    count(*) FILTER (
                        WHERE item.status = 'open'
                    ) AS unresolved_count,
                    coalesce(
                        sum(abs(item.amount)) FILTER (
                            WHERE item.status = 'open'
                        ),
                        0
                    )::DECIMAL(38,4) AS unresolved_amount,
                    arg_max(
                        item.unresolved_id,
                        CASE
                            WHEN item.status = 'open' THEN abs(item.amount)
                            ELSE NULL
                        END
                    ) AS first_review_id
                FROM reconciliation_balance balance
                LEFT JOIN unresolved_balance item
                  ON item.balance_id = balance.balance_id
                GROUP BY balance.run_id
            ),
            balances AS (
                SELECT
                    balance.run_id,
                    count(*) AS balance_count,
                    count(*) FILTER (
                        WHERE balance.status = 'balanced'
                    ) AS balanced_count,
                    coalesce(sum(abs(balance.expected_amount)), 0)
                        ::DECIMAL(38,4) AS expected_amount,
                    coalesce(sum(abs(balance.matched_amount)), 0)
                        ::DECIMAL(38,4) AS matched_amount,
                    count(*) FILTER (
                        WHERE balance.evidence_json IS NOT NULL
                    ) AS evidence_count
                FROM reconciliation_balance balance
                GROUP BY balance.run_id
            ),
            review_decisions AS (
                SELECT
                    balance.run_id,
                    count(*) FILTER (
                        WHERE item.status = 'open'
                    ) AS open_count
                FROM reconciliation_balance balance
                LEFT JOIN unresolved_balance item
                  ON item.balance_id = balance.balance_id
                GROUP BY balance.run_id
            ),
            candidates AS (
                SELECT
                    revision.period_id,
                    count(*) FILTER (
                        WHERE coalesce(state.status, revision.status) = 'candidate'
                    ) AS candidate_count
                FROM input_revision revision
                LEFT JOIN input_revision_state state
                  ON state.revision_id = revision.revision_id
                GROUP BY revision.period_id
            ),
            active_jobs AS (
                SELECT
                    store_id,
                    period_token,
                    count(*) FILTER (
                        WHERE status IN ('queued', 'running')
                    ) AS active_count
                FROM compute_job
                WHERE period_token IS NOT NULL
                GROUP BY store_id, period_token
            ),
            active_store_jobs AS (
                SELECT
                    store_id,
                    count(*) FILTER (
                        WHERE status IN ('queued', 'running')
                    ) AS active_count
                FROM compute_job
                WHERE period_token IS NULL
                GROUP BY store_id
            )
            SELECT
                period.period_id,
                period.store_id,
                period.period_start,
                period.period_end,
                period.period_state,
                period.platform_code,
                period.definition_json,
                run.run_id,
                coalesce(
                    try_cast(
                        json_extract_string(run.metrics_json, '$.certifiable')
                        AS BOOLEAN
                    ),
                    false
                ) AS certifiable,
                CASE
                    WHEN run.finished_at IS NULL THEN NULL
                    ELSE strftime(run.finished_at, '%Y-%m-%dT%H:%M:%S%z')
                END AS finished_at,
                coalesce(checklist.relevant_count, 0) AS relevant_count,
                coalesce(checklist.present_count, 0) AS present_count,
                coalesce(checklist.missing_count, 0) AS missing_count,
                coalesce(checklist.failed_count, 0) AS failed_count,
                coalesce(checklist.pending_count, 0) AS pending_count,
                coalesce(unresolved.unresolved_count, 0) AS unresolved_count,
                coalesce(unresolved.unresolved_amount, 0)
                    ::DECIMAL(38,4) AS unresolved_amount,
                unresolved.first_review_id,
                coalesce(balances.balance_count, 0) AS balance_count,
                coalesce(balances.balanced_count, 0) AS balanced_count,
                coalesce(balances.expected_amount, 0)
                    ::DECIMAL(38,4) AS expected_amount,
                coalesce(balances.matched_amount, 0)
                    ::DECIMAL(38,4) AS matched_amount,
                coalesce(balances.evidence_count, 0) AS evidence_count,
                coalesce(candidates.candidate_count, 0) AS candidate_count,
                (
                    coalesce(active_jobs.active_count, 0)
                    + coalesce(active_store_jobs.active_count, 0)
                ) AS active_count
            FROM current_periods period
            LEFT JOIN latest_runs run
              ON run.period_id = period.period_id
            LEFT JOIN checklist
              ON checklist.period_id = period.period_id
            LEFT JOIN unresolved
              ON unresolved.run_id = run.run_id
            LEFT JOIN balances
              ON balances.run_id = run.run_id
            LEFT JOIN review_decisions
              ON review_decisions.run_id = run.run_id
            LEFT JOIN candidates
              ON candidates.period_id = period.period_id
            LEFT JOIN active_jobs
              ON active_jobs.store_id = period.store_id
             AND active_jobs.period_token = strftime(period.period_start, '%Y-%m')
            LEFT JOIN active_store_jobs
              ON active_store_jobs.store_id = period.store_id
            ORDER BY
                period.period_start,
                lower(
                    trim(
                        coalesce(
                            nullif(
                                json_extract_string(
                                    period.definition_json,
                                    '$.store_name'
                                ),
                                ''
                            ),
                            period.store_id
                        )
                    )
                )
            """,
            [enterprise_id],
        )
    )

    cells: list[dict[str, object]] = []
    stores: dict[str, dict[str, str]] = {}
    periods: set[str] = set()
    for record in records:
        period = str(record["period_start"])[:7]
        store_id = str(record["store_id"])
        store_name = _store_name(record["definition_json"], store_id)
        stores.setdefault(
            store_id,
            {
                "id": store_id,
                "name": store_name,
                "platformId": str(record["platform_code"]),
            },
        )
        periods.add(period)

        missing_count = int(record["missing_count"])
        failed_count = int(record["failed_count"])
        pending_count = int(record["pending_count"])
        unresolved_count = int(record["unresolved_count"])
        unresolved_amount = _decimal(record["unresolved_amount"])
        relevant_count = int(record["relevant_count"])
        present_count = int(record["present_count"])
        balance_count = int(record["balance_count"])
        balanced_count = int(record["balanced_count"])
        expected_amount = _decimal(record["expected_amount"])
        matched_amount = _decimal(record["matched_amount"])
        candidate_count = int(record["candidate_count"])
        active_count = int(record["active_count"])
        has_run = bool(record["run_id"])
        certifiable = bool(record["certifiable"])
        is_current_open_period = (
            period == date.today().strftime("%Y-%m")
            and str(record["period_state"]) == "open"
        )

        if active_count:
            status = "processing"
        elif is_current_open_period and (
            not has_run
            or missing_count
            or pending_count
            or (relevant_count and present_count < relevant_count)
        ):
            status = "collecting"
        elif (
            not has_run
            or missing_count
            or failed_count
            or pending_count
            or (relevant_count and present_count < relevant_count)
        ):
            status = "missing_sources"
        elif unresolved_count or unresolved_amount != _ZERO:
            status = "amount_mismatch"
        elif certifiable and candidate_count == 0:
            status = "usable"
        else:
            status = "waiting_review"

        happened, impact, action, outcome = _business_copy(
            status=status,
            missing_count=missing_count,
            failed_count=failed_count,
            unresolved_count=unresolved_count,
            difference_amount=unresolved_amount,
        )
        source_state = (
            "pending"
            if status == "collecting"
            else "passed"
            if has_run
            and not (missing_count or failed_count or pending_count)
            and (not relevant_count or present_count == relevant_count)
            else "failed"
            if missing_count or failed_count
            else "pending"
        )
        amount_state = (
            "passed"
            if has_run and unresolved_count == 0 and unresolved_amount == _ZERO
            else "failed"
            if unresolved_count or unresolved_amount != _ZERO
            else "pending"
        )
        trace_state = (
            "passed"
            if balance_count and int(record["evidence_count"]) == balance_count
            else "pending"
            if balance_count
            else "not_applicable"
        )
        confirmation_state = "passed" if certifiable and candidate_count == 0 else "pending"
        amount_match_rate = (
            matched_amount / expected_amount
            if expected_amount > _ZERO
            else Decimal("1")
            if balance_count
            else Decimal("0")
        )
        amount_match_rate = max(_ZERO, min(Decimal("1"), amount_match_rate))

        cells.append(
            {
                "periodId": str(record["period_id"]),
                "storeId": store_id,
                "storeName": store_name,
                "platformId": str(record["platform_code"]),
                "period": period,
                "status": status,
                "statusLabel": {
                    "usable": "可以使用",
                    "missing_sources": "还差文件",
                    "amount_mismatch": "金额对不上",
                    "waiting_review": "等待确认",
                    "processing": "正在整理",
                    "collecting": "本月进行中",
                }[status],
                "runId": str(record["run_id"]) if record["run_id"] else None,
                "firstReviewId": (
                    str(record["first_review_id"]) if record["first_review_id"] else None
                ),
                "periodState": str(record["period_state"]),
                "facts": {
                    "requiredSourceCount": relevant_count,
                    "presentSourceCount": present_count,
                    "missingSourceCount": missing_count,
                    "failedSourceCount": failed_count,
                    "unresolvedCount": unresolved_count,
                    "unresolvedAmount": format(unresolved_amount, "f"),
                    "balanceCount": balance_count,
                    "balancedCount": balanced_count,
                    "amountMatchRate": format(amount_match_rate, ".6f"),
                    "candidateInputCount": candidate_count,
                    "lastCalculatedAt": _iso_timestamp(record["finished_at"]),
                },
                "explanation": {
                    "happened": happened,
                    "impact": impact,
                    "action": action,
                    "outcome": outcome,
                },
                "checks": [
                    _check(
                        "sources",
                        "本月需要的文件",
                        source_state,
                        (
                            f"已收到 {present_count} 项，共需要 {relevant_count} 项。"
                            if relevant_count
                            else "当前没有单独配置必需文件清单。"
                        ),
                    ),
                    _check(
                        "amounts",
                        "文件之间的金额",
                        amount_state,
                        (f"已核对 {balance_count} 组记录，仍有 {unresolved_count} 组需要处理。"),
                    ),
                    _check(
                        "trace",
                        "疑问能否找到原始记录",
                        trace_state,
                        (
                            f"{int(record['evidence_count'])} 组结果已有原始依据。"
                            if balance_count
                            else "还没有形成需要定位的核对结果。"
                        ),
                    ),
                    _check(
                        "confirmation",
                        "必要的业务确认",
                        confirmation_state,
                        (
                            "当前必要确认已经完成。"
                            if confirmation_state == "passed"
                            else f"仍有 {candidate_count} 项文件版本或业务情况待确认。"
                        ),
                    ),
                ],
            }
        )

    ordered_periods = sorted(periods)
    current_period = ordered_periods[-1] if ordered_periods else None
    current_cells = [cell for cell in cells if cell["period"] == current_period]
    counts = Counter(str(cell["status"]) for cell in current_cells)
    attention = sorted(
        (
            cell
            for cell in current_cells
            if cell["status"] not in {"usable", "collecting"}
        ),
        key=lambda cell: (
            _STATUS_PRIORITY[str(cell["status"])],
            -_decimal(cell["facts"]["unresolvedAmount"]),  # type: ignore[index]
            str(cell["storeName"]),
        ),
    )
    attention_count = sum(
        value
        for key, value in counts.items()
        if key not in {"usable", "collecting"}
    )
    return {
        "currentPeriod": current_period,
        "periods": ordered_periods,
        "stores": sorted(
            stores.values(),
            key=lambda item: (item["platformId"], item["name"].casefold()),
        ),
        "cells": cells,
        "summary": {
            "storeCount": len(stores),
            "usableCount": counts["usable"],
            "attentionCount": attention_count,
            "missingSourceCount": counts["missing_sources"],
            "amountMismatchCount": counts["amount_mismatch"],
            "waitingReviewCount": counts["waiting_review"],
            "processingCount": counts["processing"],
            "collectingCount": counts["collecting"],
            "verdict": (
                "当前月份都已通过核验，可以用于经营查看。"
                if current_cells and counts["usable"] == len(current_cells)
                else (
                    f"本月仍在进行，{counts['collecting']} 家店正在等待月度数据形成。"
                )
                if current_cells and counts["collecting"] == len(current_cells)
                else (f"当前有 {attention_count} 家店需要关注。")
                if current_cells
                else "当前还没有形成可核验的店铺月份。"
            ),
        },
        "firstAttention": attention[0] if attention else None,
        "boundary": ("“可以使用”表示通过当前已配置的文件、金额和确认门禁，不是外部审计意见。"),
    }
