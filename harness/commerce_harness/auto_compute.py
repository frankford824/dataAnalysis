from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from .bootstrap import StoreTarget, bootstrap_targets, stable_identity
from .code_identity import resolve_code_identity
from .config import HarnessConfig
from .freeze import freeze_candidates
from .inventory import scan_inventory
from .memory.database import DuckDBMemory
from .performance import sync_performance_sources
from .performance_engine import (
    PerformanceCalculationBlocked,
    calculate_certified_performance,
    ensure_builtin_performance_policy,
)
from .phase_a import adjudicate_workspace, normalize_workspace, reconcile_period
from .profiling import profile_snapshots
from .role import reads_customer_sources
from .target_plan import TargetPlan, build_target_plan
from .workbench import WorkbenchPaths


@dataclass(frozen=True, slots=True)
class ComputeTriggerResult:
    accepted: bool
    running: bool
    message: str


def _json_default(value: object) -> str:
    if isinstance(value, (date, Path)):
        return str(value)
    return str(value)


def _manifest_signature(path: Path) -> str:
    """Hash stable inventory evidence without the scan timestamp."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    candidate_ids = {
        str(value) for value in payload.get("candidate_source_ids", [])
    }
    records = []
    for record in payload.get("records", []):
        if not isinstance(record, dict):
            continue
        if str(record.get("source_id")) not in candidate_ids:
            continue
        records.append(
            {
                "source_id": record.get("source_id"),
                "path": record.get("path"),
                "purpose": record.get("purpose"),
                "size": record.get("size"),
                "mtime_utc": record.get("mtime_utc"),
                "attributes": record.get("attributes"),
            }
        )
    encoded = json.dumps(
        sorted(records, key=lambda item: str(item["source_id"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _execution_signature(
    config: HarnessConfig,
    workbench: WorkbenchPaths,
    inventory_signature: str,
) -> str:
    with DuckDBMemory(workbench.database) as database:
        rule_checksums = [
            str(row[0])
            for row in database.execute(
                """
                SELECT checksum_sha256
                FROM rule_version
                WHERE status = 'approved'
                ORDER BY rule_version_id
                """
            ).fetchall()
        ]
    payload = {
        "inventory": inventory_signature,
        "code": resolve_code_identity().value,
        "rules": rule_checksums,
        "periods": config.source.scope.resolved_periods(),
        "mode": config.reconciliation.mode,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_source_path(value: object) -> str:
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.replace("\\", "/").casefold()


def _target_plan_records(
    workbench: WorkbenchPaths,
) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    inventory_path = workbench.reports / "source-inventory.json"
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory_records = [
        item
        for item in payload.get("records", [])
        if isinstance(item, dict)
    ]
    inventory_by_path = {
        _normalized_source_path(item.get("path")): item
        for item in inventory_records
    }
    planning_records: list[dict[str, Any]] = []
    snapshot_by_source_id: dict[str, str] = {}
    with DuckDBMemory(workbench.database) as database:
        latest_freeze = database.execute(
            """
            SELECT run_id
            FROM run_log
            WHERE run_kind = 'freeze' AND status = 'succeeded'
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """
        ).fetchone()
        rows = database.execute(
            """
            SELECT s.snapshot_id, s.source_uri, p.source_kind,
                   p.template_id, p.route_json
            FROM source_snapshot s
            LEFT JOIN source_profile p ON p.snapshot_id = s.snapshot_id
            QUALIFY row_number() OVER (
                PARTITION BY s.snapshot_id
                ORDER BY p.created_at DESC NULLS LAST
            ) = 1
            """
        ).fetchall()
    if not latest_freeze:
        raise RuntimeError("尚未产生可追溯的文件冻结运行")
    for snapshot_id, source_uri, source_kind, template_id, route_json in rows:
        inventory_record = inventory_by_path.get(
            _normalized_source_path(source_uri),
            {},
        )
        source_id = str(
            inventory_record.get("source_id")
            or f"snapshot_{snapshot_id}"
        )
        route: dict[str, Any] = {}
        if route_json:
            try:
                parsed = json.loads(str(route_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                route = parsed
        record = {
            **inventory_record,
            "source_id": source_id,
            "path": str(source_uri),
            "source_kind": str(source_kind or route.get("source_kind") or ""),
            "template_id": str(template_id or route.get("template_id") or ""),
            "route": route,
        }
        planning_records.append(record)
        snapshot_by_source_id[source_id] = str(snapshot_id)
    return planning_records, snapshot_by_source_id, str(latest_freeze[0])


def refresh_target_plan(
    config: HarnessConfig,
    workbench: WorkbenchPaths,
) -> TargetPlan:
    records, snapshot_by_source_id, freeze_run_id = _target_plan_records(
        workbench
    )
    with DuckDBMemory(workbench.database) as database:
        configured_store_rows = database.execute(
            """
            WITH classified AS (
                SELECT
                    coalesce(
                        nullif(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            ''
                        ),
                        store_id
                    ) AS store_name,
                    CASE
                        WHEN lower(trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                        ))) LIKE 'pdd%'
                          OR trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                          )) LIKE '拼多多%'
                            THEN 'pinduoduo'
                        WHEN trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                        )) LIKE '抖店%'
                          OR trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                          )) LIKE '抖音%'
                            THEN 'douyin'
                        WHEN trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                        )) LIKE '京东%'
                            THEN 'jd'
                        WHEN lower(trim(coalesce(
                            json_extract_string(
                                definition_json,
                                '$.store_name'
                            ),
                            store_id
                        ))) LIKE '%1688'
                            THEN '1688'
                        WHEN lower(platform_code) IN ('pdd', 'pinduoduo')
                            THEN 'pinduoduo'
                        ELSE lower(platform_code)
                    END AS platform_code,
                    created_at,
                    contract_version,
                    contract_id
                FROM reconciliation_contract
                WHERE status = 'active'
                  AND enterprise_id = ?
            ),
            ranked AS (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY platform_code, lower(trim(store_name))
                           ORDER BY created_at DESC, contract_version DESC,
                                    contract_id DESC
                       ) AS position
                FROM classified
            )
            SELECT platform_code, store_name
            FROM ranked
            WHERE position = 1
            ORDER BY platform_code, store_name
            """,
            [stable_identity("enterprise", "local-enterprise")],
        ).fetchall()
    plan = build_target_plan(
        records,
        configured_stores=[
            {
                "platform": str(platform),
                "logical_store": str(store_name),
            }
            for platform, store_name in configured_store_rows
        ],
    )
    target_groups: dict[tuple[str, str], StoreTarget] = {}
    for item in plan.targets:
        key = (item.platform, item.logical_store_key)
        current = target_groups.get(key)
        period_token = item.period[2:4] + item.period[5:7]
        if current is None:
            target_groups[key] = StoreTarget(
                name=item.logical_store,
                period_tokens=[period_token],
                platform_code=item.platform,
            )
        elif period_token not in current.period_tokens:
            current.period_tokens.append(period_token)
    if target_groups:
        with DuckDBMemory(workbench.database) as database:
            bootstrap_targets(
                database,
                freeze_run_id=freeze_run_id,
                targets=list(target_groups.values()),
                records=records,
                snapshot_by_source_id=snapshot_by_source_id,
                reconciliation_mode=config.reconciliation.mode,
                retire_missing=True,
            )
    report_path = workbench.reports / "target-plan.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return plan


class AutoComputeRunner:
    """One-writer deterministic compute loop hosted by the local Docker API.

    DuckDB is intentionally written by this single thread.  Polars and DuckDB
    may still use native parallelism inside each store/month task, while the
    API remains responsive and reads the persisted job state.
    """

    def __init__(
        self,
        config: HarnessConfig,
        workbench: WorkbenchPaths,
    ) -> None:
        self.config = config
        self.workbench = workbench
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        env = os.getenv("FA_AUTO_COMPUTE")
        if env is not None:
            return env.strip().casefold() in {"1", "true", "yes", "on"}
        return self.config.compute.enabled

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._recover_interrupted_jobs()
        self._thread = threading.Thread(
            target=self._loop,
            name="fa-local-compute",
            daemon=True,
        )
        self._thread.start()
        if self.config.compute.run_on_startup:
            self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def trigger(self) -> ComputeTriggerResult:
        if not self.enabled:
            return ComputeTriggerResult(
                accepted=False,
                running=False,
                message="本机自动计算尚未启用。",
            )
        already_running = self.running
        self._wake.set()
        return ComputeTriggerResult(
            accepted=True,
            running=already_running,
            message=(
                "计算正在进行，系统会在本轮结束后再次检查新文件。"
                if already_running
                else "已开始检查全部店铺和月份。"
            ),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.config.compute.poll_seconds)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._state_lock:
                self._running = True
            try:
                self._run_cycle()
            except Exception as exc:
                self._record_cycle_failure(exc)
            finally:
                with self._state_lock:
                    self._running = False

    def _recover_interrupted_jobs(self) -> None:
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET status = 'failed',
                    finished_at = current_timestamp,
                    error_detail = '服务重启前任务未正常结束',
                    detail = '服务已经恢复，可重新开始计算'
                WHERE status = 'running'
                """
            )

    def _new_job(
        self,
        *,
        cycle_id: str,
        kind: str,
        label: str,
        contract_id: str | None = None,
        period_id: str | None = None,
        store_id: str | None = None,
        period_token: str | None = None,
    ) -> str:
        job_id = f"job_{uuid.uuid4().hex}"
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                INSERT INTO compute_job (
                    job_id, cycle_id, job_kind, contract_id, period_id,
                    store_id, period_token, status, progress_percent,
                    business_label, detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, '等待开始')
                """,
                [
                    job_id,
                    cycle_id,
                    kind,
                    contract_id,
                    period_id,
                    store_id,
                    period_token,
                    label,
                ],
            )
        return job_id

    def _run_job(
        self,
        job_id: str,
        detail: str,
        operation: Callable[[], Any],
    ) -> Any:
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET status = 'running', progress_percent = 10,
                    detail = ?, attempt = attempt + 1,
                    started_at = current_timestamp, finished_at = NULL,
                    error_detail = NULL
                WHERE job_id = ?
                """,
                [detail, job_id],
            )
        try:
            result = operation()
        except Exception as exc:
            with DuckDBMemory(self.workbench.database) as database:
                database.execute(
                    """
                    UPDATE compute_job
                    SET status = 'failed', progress_percent = 100,
                        detail = '需要处理后重试',
                        finished_at = current_timestamp, error_detail = ?
                    WHERE job_id = ?
                    """,
                    [str(exc)[:4000], job_id],
                )
            raise
        metrics = asdict(result) if hasattr(result, "__dataclass_fields__") else result
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET status = 'succeeded', progress_percent = 100,
                    detail = '已完成', finished_at = current_timestamp,
                    metrics_json = ?
                WHERE job_id = ?
                """,
                [
                    json.dumps(
                        metrics,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=_json_default,
                    ),
                    job_id,
                ],
            )
        return result

    def _update_freeze_progress(
        self,
        job_id: str,
        completed: int,
        total: int,
    ) -> None:
        percent = min(95, 10 + int((completed / max(total, 1)) * 85))
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET progress_percent = ?,
                    detail = ?
                WHERE job_id = ? AND status = 'running'
                """,
                [
                    percent,
                    f"已安全保存 {completed}/{total} 份候选文件",
                    job_id,
                ],
            )

    def _update_freeze_progress_on_connection(
        self,
        job_id: str,
        database: DuckDBMemory,
        completed: int,
        total: int,
    ) -> None:
        percent = min(95, 10 + int((completed / max(total, 1)) * 85))
        database.execute(
            """
            UPDATE compute_job
            SET progress_percent = ?,
                detail = ?
            WHERE job_id = ? AND status = 'running'
            """,
            [
                percent,
                f"已安全保存 {completed}/{total} 份候选文件",
                job_id,
            ],
        )

    def _mark_waiting_for_input(self, job_id: str) -> None:
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET detail = '等待本月来源文件',
                    metrics_json = json_merge_patch(
                        coalesce(metrics_json, '{}'),
                        '{"data_status":"waiting_for_input"}'
                    )
                WHERE job_id = ?
                """,
                [job_id],
            )

    def _latest_inventory_signature(self) -> str | None:
        with DuckDBMemory(self.workbench.database) as database:
            row = database.execute(
                """
                SELECT json_extract_string(metrics_json, '$.manifest_signature')
                FROM compute_job
                WHERE job_kind = 'inventory'
                  AND status = 'succeeded'
                  AND json_extract_string(
                      metrics_json, '$.manifest_signature'
                  ) IS NOT NULL
                ORDER BY finished_at DESC NULLS LAST
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def _all_scope_results_current(self) -> bool:
        scopes = self._period_scopes()
        if not scopes:
            return False
        succeeded = self._succeeded_scope_results()
        return all(
            (scope["contract_id"], scope["period_id"]) in succeeded
            for scope in scopes
        )

    def _succeeded_scope_results(self) -> set[tuple[str, str]]:
        with DuckDBMemory(self.workbench.database) as database:
            return {
                (str(row[0]), str(row[1]))
                for row in database.execute(
                    """
                    SELECT DISTINCT contract_id, period_id
                    FROM compute_job
                    WHERE job_kind = 'reconcile'
                      AND status = 'succeeded'
                      AND contract_id IS NOT NULL
                      AND period_id IS NOT NULL
                    """
                ).fetchall()
            }

    def _latest_normalization_result(
        self,
        store_id: str,
    ) -> dict[str, Any] | None:
        with DuckDBMemory(self.workbench.database) as database:
            row = database.execute(
                """
                SELECT metrics_json
                FROM compute_job
                WHERE job_kind = 'normalize'
                  AND status = 'succeeded'
                  AND store_id = ?
                  AND metrics_json IS NOT NULL
                ORDER BY finished_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                [store_id],
            ).fetchone()
        if not row or row[0] is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            return None
        current_revisions = payload.get("current_revisions")
        if not isinstance(current_revisions, int):
            return None
        return payload

    def _period_scopes(
        self,
        plan: TargetPlan | None = None,
    ) -> list[dict[str, str]]:
        with DuckDBMemory(self.workbench.database) as database:
            rows = database.execute(
                """
                WITH classified AS (
                    SELECT
                        p.period_id,
                        p.contract_id,
                        p.store_id,
                        p.period_start,
                        strftime(p.period_start, '%y%m') AS period_token,
                        coalesce(
                            nullif(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                ''
                            ),
                            p.store_id
                        ) AS store_name,
                        CASE
                            WHEN lower(trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                            ))) LIKE 'pdd%'
                              OR trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                              )) LIKE '拼多多%'
                                THEN 'pinduoduo'
                            WHEN trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                            )) LIKE '抖店%'
                              OR trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                              )) LIKE '抖音%'
                                THEN 'douyin'
                            WHEN trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                            )) LIKE '京东%'
                                THEN 'jd'
                            WHEN lower(trim(coalesce(
                                json_extract_string(
                                    c.definition_json,
                                    '$.store_name'
                                ),
                                p.store_id
                            ))) LIKE '%1688'
                                THEN '1688'
                            WHEN lower(c.platform_code)
                                 IN ('pdd', 'pinduoduo')
                                THEN 'pinduoduo'
                            ELSE lower(c.platform_code)
                        END AS platform_code,
                        c.enterprise_id,
                        c.created_at,
                        c.contract_version,
                        p.revision_no
                    FROM accounting_period p
                    JOIN reconciliation_contract c
                      ON c.contract_id = p.contract_id
                    WHERE c.status = 'active'
                      AND c.enterprise_id = ?
                      AND p.period_start >= DATE '2026-02-01'
                      AND p.period_start
                          <= date_trunc('month', current_date)
                ),
                ranked AS (
                    SELECT
                        classified.*,
                        row_number() OVER (
                            PARTITION BY
                                enterprise_id,
                                platform_code,
                                lower(trim(store_name)),
                                period_start
                            ORDER BY
                                created_at DESC,
                                contract_version DESC,
                                revision_no DESC,
                                contract_id DESC
                        ) AS position
                    FROM classified
                )
                SELECT
                    period_id,
                    contract_id,
                    store_id,
                    period_token,
                    store_name,
                    platform_code
                FROM ranked
                WHERE position = 1
                ORDER BY period_start, platform_code, store_name
                """,
                [stable_identity("enterprise", "local-enterprise")],
            ).fetchall()
        scopes = [
            {
                "period_id": str(row[0]),
                "contract_id": str(row[1]),
                "store_id": str(row[2]),
                "period_token": str(row[3]),
                "store_name": str(row[4]),
                "platform_code": str(row[5]),
            }
            for row in rows
        ]
        if plan is None:
            return scopes
        planned = {
            (
                target.platform.casefold(),
                target.logical_store.strip().casefold(),
                target.period[2:4] + target.period[5:7],
            ): target
            for target in plan.targets
        }
        selected: list[dict[str, str]] = []
        for scope in scopes:
            target = planned.get(
                (
                    scope["platform_code"].casefold(),
                    scope["store_name"].strip().casefold(),
                    scope["period_token"],
                )
            )
            if target is None:
                continue
            selected.append(
                {
                    **scope,
                    "target_status": target.status.value,
                    "target_source_count": str(len(target.source_ids)),
                }
            )
        return selected

    def _reconcile_and_performance(
        self,
        scope: dict[str, str],
    ) -> dict[str, object]:
        reconciliation = reconcile_period(
            self.workbench,
            period_token=scope["period_token"],
            store_id=scope["store_id"],
            mode=self.config.reconciliation.mode,
        )
        reconciliation_payload = (
            asdict(reconciliation)
            if hasattr(reconciliation, "__dataclass_fields__")
            else dict(reconciliation)
            if isinstance(reconciliation, dict)
            else {"result": str(reconciliation)}
        )
        payload: dict[str, object] = {
            "reconciliation": reconciliation_payload,
            "performance": {
                "status": "waiting_for_certified_product_ledger",
                "message": "本月尚未形成可发布的商品级认证账本。",
            },
        }
        certifiable = bool(
            getattr(
                reconciliation,
                "certifiable",
                reconciliation_payload.get("certifiable", False),
            )
        )
        if not certifiable:
            return payload
        reconciliation_period_id = str(
            getattr(
                reconciliation,
                "period_id",
                reconciliation_payload.get("period_id", scope["period_id"]),
            )
        )
        with DuckDBMemory(self.workbench.database) as database:
            enterprise_id, period_start = database.fetchone_required(
                """
                SELECT contract.enterprise_id, period.period_start
                FROM accounting_period period
                JOIN reconciliation_contract contract
                  ON contract.contract_id = period.contract_id
                WHERE period.period_id = ?
                """,
                [reconciliation_period_id],
            )
        try:
            ensure_builtin_performance_policy(
                self.workbench,
                enterprise_id=str(enterprise_id),
                effective_from=period_start,
            )
            performance = calculate_certified_performance(
                self.workbench,
                enterprise_id=str(enterprise_id),
                period_id=reconciliation_period_id,
            )
        except PerformanceCalculationBlocked as exc:
            payload["performance"] = {
                "status": "blocked",
                "code": exc.code.value,
                "message": str(exc),
                "details": exc.details,
            }
        else:
            payload["performance"] = {
                "status": "certified",
                **asdict(performance),
            }
        return payload

    def _uploaded_snapshot_signature(self) -> str:
        """Signature of what edge has uploaded so far.

        Core cannot scan the customer's disk, so "did anything change" is
        answered by the content-addressed snapshots it has received.
        """
        with DuckDBMemory(self.workbench.database) as database:
            row = database.execute(
                """
                SELECT count(*), coalesce(max(captured_at)::VARCHAR, ''),
                       coalesce(string_agg(content_sha256, ',' ORDER BY content_sha256), '')
                FROM source_snapshot
                """
            ).fetchone()
        count, latest, digest_source = (
            (int(row[0] or 0), str(row[1] or ""), str(row[2] or ""))
            if row is not None
            else (0, "", "")
        )
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        return f"uploaded:{count}:{latest}:{digest}"

    def _run_cycle(self) -> None:
        cycle_id = f"cycle_{uuid.uuid4().hex}"
        if reads_customer_sources():
            inventory_job = self._new_job(
                cycle_id=cycle_id,
                kind="inventory",
                label="检查全部店铺的新文件",
            )
            inventory_signature = _manifest_signature(
                self._run_job(
                    inventory_job,
                    "正在只读检查 finance-win 文件",
                    lambda: scan_inventory(self.config, self.workbench),
                ).path
            )
        else:
            inventory_job = self._new_job(
                cycle_id=cycle_id,
                kind="inventory",
                label="清点已收到的文件",
            )
            inventory_signature = self._run_job(
                inventory_job,
                "正在清点已上传的文件",
                self._uploaded_snapshot_signature,
            )
        signature = _execution_signature(
            self.config,
            self.workbench,
            inventory_signature,
        )
        previous_signature = self._latest_inventory_signature()
        may_reuse_current_results = signature == previous_signature
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET metrics_json = json_merge_patch(
                    coalesce(metrics_json, '{}'),
                    ?
                )
                WHERE job_id = ?
                """,
                [
                    json.dumps(
                        {
                            "manifest_signature": signature,
                            "inventory_signature": inventory_signature,
                        }
                    ),
                    inventory_job,
                ],
            )
        sync_performance_sources(
            self.workbench,
            enterprise_id=stable_identity("enterprise", "local-enterprise"),
        )
        if signature == previous_signature and self._all_scope_results_current():
            return

        if reads_customer_sources():
            freeze_job = self._new_job(
                cycle_id=cycle_id,
                kind="freeze",
                label="保存本次计算使用的原始文件",
            )
            self._run_job(
                freeze_job,
                "正在制作不可覆盖的只读副本",
                partial(
                    freeze_candidates,
                    self.config,
                    self.workbench,
                    progress_callback=partial(
                        self._update_freeze_progress_on_connection,
                        freeze_job,
                    ),
                ),
            )
        # Uploads are already immutable content-addressed copies, so core has
        # nothing left to freeze.
        sync_performance_sources(
            self.workbench,
            enterprise_id=stable_identity("enterprise", "local-enterprise"),
        )

        profile_job = self._new_job(
            cycle_id=cycle_id,
            kind="profile",
            label="识别文件用途和所属月份",
        )
        self._run_job(
            profile_job,
            "正在识别订单、平台流水和费用文件",
            lambda: profile_snapshots(self.workbench),
        )
        target_plan = refresh_target_plan(self.config, self.workbench)
        scopes = self._period_scopes(target_plan)
        scopes_by_store: dict[str, list[dict[str, str]]] = {}
        for scope in scopes:
            scopes_by_store.setdefault(scope["store_id"], []).append(scope)
        succeeded_scopes = (
            self._succeeded_scope_results()
            if may_reuse_current_results
            else set()
        )

        normalization_results: dict[str, Any] = {}
        performed_normalization = False
        for store_id, store_scopes in scopes_by_store.items():
            if self._stop.is_set():
                return
            available_scopes = [
                scope
                for scope in store_scopes
                if scope.get("target_status") != "missing"
            ]
            if not available_scopes:
                continue
            if may_reuse_current_results:
                previous_result = self._latest_normalization_result(store_id)
                if previous_result is not None:
                    normalization_results[store_id] = previous_result
                    continue
            period_tokens = tuple(
                scope["period_token"] for scope in available_scopes
            )
            first_scope = available_scopes[0]
            normalize_job = self._new_job(
                cycle_id=cycle_id,
                kind="normalize",
                label=f"{first_scope['store_name']} · 整理 2026-02 至今原始记录",
                contract_id=first_scope["contract_id"],
                store_id=store_id,
            )
            try:
                normalization_results[store_id] = self._run_job(
                    normalize_job,
                    "正在按交易时间一次整理该店铺全部月份并去重",
                    partial(
                        normalize_workspace,
                        self.workbench,
                        periods=period_tokens,
                        store_id=store_id,
                    ),
                )
                performed_normalization = True
            except Exception:
                if not self.config.compute.continue_after_scope_failure:
                    raise

        has_current_revisions = False
        for result in (
            normalization_results.values() if performed_normalization else ()
        ):
            current_revisions = getattr(result, "current_revisions", None)
            if isinstance(result, dict):
                current_revisions = result.get(
                    "current_revisions",
                    current_revisions,
                )
            if current_revisions:
                has_current_revisions = True
                break
        if has_current_revisions:
            adjudicate_workspace(
                self.workbench,
                mode=self.config.reconciliation.mode,
            )

        for scope in scopes:
            if self._stop.is_set():
                return
            if (
                scope["contract_id"],
                scope["period_id"],
            ) in succeeded_scopes:
                continue
            label_prefix = (
                f"{scope['store_name']} · 20{scope['period_token'][:2]}-"
                f"{scope['period_token'][2:]}"
            )
            reconcile_job = self._new_job(
                cycle_id=cycle_id,
                kind="reconcile",
                label=f"{label_prefix} 计算经营结果",
                **{
                    key: scope[key]
                    for key in (
                        "contract_id",
                        "period_id",
                        "store_id",
                        "period_token",
                    )
                },
            )
            normalize_result = normalization_results.get(scope["store_id"])
            current_revisions = getattr(
                normalize_result,
                "current_revisions",
                None,
            )
            if isinstance(normalize_result, dict):
                current_revisions = normalize_result.get(
                    "current_revisions",
                    current_revisions,
                )
            if scope.get("target_status") == "missing" or current_revisions == 0:
                self._run_job(
                    reconcile_job,
                    "正在检查本月来源文件是否齐全",
                    lambda: {
                        "data_status": "waiting_for_input",
                        "certifiable": False,
                    },
                )
                self._mark_waiting_for_input(reconcile_job)
                continue
            if normalize_result is None:
                try:
                    self._run_job(
                        reconcile_job,
                        "该店铺原始记录整理失败，正在保留问题",
                        lambda: (_ for _ in ()).throw(
                            RuntimeError("该店铺原始记录整理失败")
                        ),
                    )
                except RuntimeError:
                    if not self.config.compute.continue_after_scope_failure:
                        raise
                continue
            try:
                self._run_job(
                    reconcile_job,
                    "正在核对订单与平台钱包并计算损益",
                    partial(self._reconcile_and_performance, scope),
                )
            except RuntimeError as exc:
                if str(exc) == "该账期没有已确认的标准化输入版本":
                    with DuckDBMemory(self.workbench.database) as database:
                        database.execute(
                            """
                            UPDATE compute_job
                            SET status = 'succeeded',
                                detail = '等待本月来源文件',
                                error_detail = NULL,
                                metrics_json = ?,
                                finished_at = current_timestamp
                            WHERE job_id = ?
                            """,
                            [
                                json.dumps(
                                    {
                                        "data_status": "waiting_for_input",
                                        "certifiable": False,
                                    }
                                ),
                                reconcile_job,
                            ],
                        )
                    continue
                if not self.config.compute.continue_after_scope_failure:
                    raise

    def _record_cycle_failure(self, exc: Exception) -> None:
        cycle_id = f"cycle_{uuid.uuid4().hex}"
        job_id = self._new_job(
            cycle_id=cycle_id,
            kind="inventory",
            label="自动计算运行状态",
        )
        with DuckDBMemory(self.workbench.database) as database:
            database.execute(
                """
                UPDATE compute_job
                SET status = 'failed', progress_percent = 100,
                    detail = '自动计算暂时停止，服务会继续重试',
                    started_at = current_timestamp,
                    finished_at = current_timestamp,
                    error_detail = ?
                WHERE job_id = ?
                """,
                [str(exc)[:4000], job_id],
            )
