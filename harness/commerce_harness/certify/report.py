"""Certification reports for closed periods — model-free, number-guarded."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.period_service import effective_period_status
from commerce_harness.run_scope import latest_run_for_period
from commerce_harness.workbench import WorkbenchPaths

DISCLAIMER = (
    "本报告不是法定审计报告，也不是鉴证业务报告。"
    "它只说明在既定口径与证据范围内，本系统敢认到什么程度。"
    "不敢认的部分会明确列出，不得隐藏。"
)


def _frozen_baseline(
    database: DuckDBMemory, period_id: str
) -> tuple[str, str, str, str, str | None]:
    """Return the frozen baseline hashes plus the run they describe.

    A certification without a frozen baseline cannot be re-verified offline, so
    refusing here is the only honest outcome.
    """
    row = database.execute(
        """
        SELECT input_manifest_sha256, rule_set_sha256, code_sha, output_sha256,
               json_extract_string(invariant_report_json, '$.reconcile_run_id')
        FROM baseline
        WHERE period_id = ? AND status = 'frozen'
        ORDER BY baseline_version DESC
        LIMIT 1
        """,
        [period_id],
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "该账期还没有冻结的黄金基线，无法生成可复核的对外认证报告"
        )
    if not all(row[index] for index in range(4)):
        raise RuntimeError("黄金基线缺少校验哈希，无法生成对外认证报告")
    return (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]) if row[4] else None,
    )


def build_certification_report(
    database: DuckDBMemory,
    *,
    period_id: str,
    operator_name: str,
) -> dict[str, Any]:
    if not operator_name.strip():
        raise ValueError("生成认证报告必须填写操作人")
    period = database.execute(
        """
        SELECT period_id, store_id, period_start, period_end
        FROM accounting_period WHERE period_id = ?
        """,
        [period_id],
    ).fetchone()
    if period is None:
        raise LookupError(f"账期不存在: {period_id}")
    # accounting_period.status is stale once the period is closed through
    # accounting_period_state; always resolve the effective status.
    status = effective_period_status(database, period_id)
    if status != "closed":
        raise RuntimeError("只有已结账的账期才能生成对外认证报告")

    input_sha, rule_sha, code_sha, output_sha, baseline_run_id = _frozen_baseline(
        database, period_id
    )
    # The reported money must come from the same run the baseline hashes cover.
    run_id = baseline_run_id or latest_run_for_period(database, period_id)
    if run_id is None:
        raise RuntimeError("找不到该账期对应的对账运行，无法生成对外认证报告")

    trust_rows = database.execute(
        """
        SELECT trust_tier, count(*), coalesce(sum(value), 0)
        FROM pnl_cell
        WHERE period_id = ? AND run_id = ? AND sku_key = '__store_total__'
        GROUP BY 1
        """,
        [period_id, run_id],
    ).fetchall()
    def _tier(count: int, amount: Decimal) -> dict[str, Any]:
        return {"count": count, "amount": format(amount, "f")}

    trust: dict[str, dict[str, Any]] = {
        str(row[0]): _tier(int(row[1]), Decimal(str(row[2]))) for row in trust_rows
    }
    partial = trust.get("partial", _tier(0, Decimal(0)))
    blocked = trust.get("blocked", _tier(0, Decimal(0)))

    unresolved = database.execute(
        """
        SELECT count(*), coalesce(sum(abs(amount)), 0)
        FROM unresolved_balance ub
        JOIN reconciliation_balance rb ON rb.balance_id = ub.balance_id
        WHERE rb.period_id = ? AND rb.run_id = ? AND ub.status = 'open'
        """,
        [period_id, run_id],
    ).fetchone()

    body: dict[str, Any] = {
        "reportKind": "certification",
        "periodId": period_id,
        "runId": run_id,
        "storeId": period[1],
        "periodStart": str(period[2]),
        "periodEnd": str(period[3]),
        "status": status,
        "operatorName": operator_name.strip(),
        "generatedAt": datetime.now(UTC).isoformat(),
        "disclaimer": DISCLAIMER,
        "trustDistribution": trust,
        "partial": partial,
        "blocked": blocked,
        "openUnresolved": _tier(
            int(unresolved[0] or 0) if unresolved else 0,
            Decimal(str(unresolved[1] or 0)) if unresolved else Decimal(0),
        ),
        "baseline": {
            "inputManifestSha256": input_sha,
            "ruleSetSha256": rule_sha,
            "codeSha": code_sha,
            "outputSha256": output_sha,
        },
        "hidesIncomplete": False,
    }
    if int(partial["count"]) > 0 or int(blocked["count"]) > 0:
        body["completenessNote"] = (
            "本报告包含只敢认一部分或不敢出数的部分，详见 trustDistribution。"
        )
    report_id = f"cert_{uuid.uuid4().hex}"
    body["reportId"] = report_id
    # Hash excludes reportId/reportSha256/path so the offline verifier can
    # recompute without circular dependence on the id itself.
    hashable = {
        key: value
        for key, value in body.items()
        if key not in {"reportId", "reportSha256", "path"}
    }
    body["reportSha256"] = hashlib.sha256(
        json.dumps(hashable, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return body


def persist_certification(
    database: DuckDBMemory,
    workbench: WorkbenchPaths,
    report: dict[str, Any],
) -> Path:
    export_dir = workbench.root / "exports" / "certifications"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{report['reportId']}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    database.execute(
        """
        INSERT INTO maintenance_log (
            action_id, action, affected_runs, affected_rows, details_json
        ) VALUES (?, 'certification_issued', 0, 1, ?)
        """,
        [
            f"cert_{uuid.uuid4().hex}",
            json.dumps(
                {
                    "report_id": report["reportId"],
                    "period_id": report["periodId"],
                    "report_sha256": report["reportSha256"],
                    "path": str(path),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ],
    )
    return path
