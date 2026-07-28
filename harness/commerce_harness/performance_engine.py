"""Deterministic certified-ledger to personnel-performance calculation.

The engine intentionally has no API or scheduler integration.  Its public
entry point consumes only the current certifiable reconciliation run and
publishes immutable, checksummed performance-result versions behind
``performance_result_head``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

from .evidence_policy import (
    NORMALIZATION_RULE_VERSION,
    PERFORMANCE_ENGINE_VERSION,
)
from .freeze import _writer_lock
from .kernel.money import amount, subtract_money, sum_money
from .memory.database import DuckDBMemory
from .workbench import WorkbenchPaths

ENGINE_VERSION = PERFORMANCE_ENGINE_VERSION
POLICY_CODE = "certified_product_performance"
_MONEY_ZERO = Decimal("0.0000")
_RATIO_ONE = Decimal("1.0000")
_COMPLETE = Decimal("1.000000")
_PRODUCT_TOTAL_KEYS = frozenset(
    {"", "__store_total__", "__all__", "all", "store_total", "total"}
)
_REQUIRED_METRICS = frozenset(
    {
        "sales",
        "refund",
        "platform_fee",
        "freight",
        "cost",
        "advertising",
        "profit",
    }
)
_PROFIT_COMPONENTS = (
    "sales",
    "refund",
    "platform_fee",
    "freight",
    "cost",
    "advertising",
)


def builtin_performance_policy_definition() -> dict[str, object]:
    """Return the only policy contract executable by this engine version."""

    return {
        "engine_version": ENGINE_VERSION,
        "evidence_policy_version": NORMALIZATION_RULE_VERSION,
        "input_grain": "certified_store_product_month",
        "assignment_time_basis": "accounting_period",
        "minimum_cost_coverage_ratio": "1.000000",
        "metrics": {
            "collected_amount": ["sales"],
            "refund_amount": ["refund"],
            "direct_cost": ["cost"],
            "allocated_cost": ["platform_fee", "freight", "advertising"],
            "operating_profit": ["profit"],
        },
        "profit_identity": list(_PROFIT_COMPONENTS),
        "rounding": "decimal38_4_half_even_residual_to_last_person",
    }


def builtin_performance_policy_checksum() -> str:
    return _sha256_json(builtin_performance_policy_definition())


def ensure_builtin_performance_policy(
    workbench: WorkbenchPaths,
    *,
    enterprise_id: str,
    effective_from: date,
) -> str:
    """Install the immutable built-in policy without creating an editable DSL."""

    definition = builtin_performance_policy_definition()
    checksum = builtin_performance_policy_checksum()
    with _writer_lock(workbench.locks / "duckdb-writer.lock"):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            existing = database.execute(
                """
                SELECT policy_version_id, checksum_sha256, effective_from
                FROM performance_policy_version
                WHERE enterprise_id = ?
                  AND policy_code = ?
                  AND status = 'approved'
                ORDER BY version DESC
                LIMIT 1
                """,
                [enterprise_id, POLICY_CODE],
            ).fetchone()
            if (
                existing is not None
                and str(existing[1]) == checksum
                and existing[2] <= effective_from
            ):
                return str(existing[0])
            next_version = int(
                database.fetchone_required(
                    """
                    SELECT coalesce(max(version), 0) + 1
                    FROM performance_policy_version
                    WHERE enterprise_id = ? AND policy_code = ?
                    """,
                    [enterprise_id, POLICY_CODE],
                )[0]
            )
            policy_id = "performance_policy_" + hashlib.sha256(
                (
                    f"{enterprise_id}\0{POLICY_CODE}\0{ENGINE_VERSION}"
                    f"\0{next_version}\0{checksum}"
                ).encode()
            ).hexdigest()[:24]
            with database.transaction() as connection:
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE performance_policy_version
                        SET status = 'retired'
                        WHERE enterprise_id = ?
                          AND policy_code = ?
                          AND status = 'approved'
                        """,
                        [enterprise_id, POLICY_CODE],
                    )
                connection.execute(
                    """
                    INSERT INTO performance_policy_version (
                        policy_version_id, enterprise_id, policy_code, version,
                        effective_from, status, definition_json, checksum_sha256,
                        approved_by, approved_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, 'approved', ?, ?,
                        'builtin_verified_engine', current_timestamp
                    )
                    """,
                    [
                        policy_id,
                        enterprise_id,
                        POLICY_CODE,
                        next_version,
                        effective_from,
                        _canonical_json(definition),
                        checksum,
                    ],
                )
    return policy_id


class PerformanceBlockCode(StrEnum):
    PERIOD_NOT_FOUND = "period_not_found"
    PERIOD_ENTERPRISE_MISMATCH = "period_enterprise_mismatch"
    POLICY_MISSING = "policy_missing"
    POLICY_CONFLICT = "policy_conflict"
    POLICY_DRIFT = "policy_drift"
    CERTIFIED_RUN_MISSING = "certified_run_missing"
    UNCERTIFIED_INPUT = "uncertified_input"
    PRODUCT_GRAIN_MISSING = "product_grain_missing"
    PRODUCT_METRIC_MISSING = "product_metric_missing"
    PRODUCT_METRIC_CONFLICT = "product_metric_conflict"
    PRODUCT_PROFIT_MISMATCH = "product_profit_mismatch"
    PRODUCT_IDENTITY_MISSING = "product_identity_missing"
    EVIDENCE_MISSING = "evidence_missing"
    COST_COVERAGE_INSUFFICIENT = "cost_coverage_insufficient"
    ASSIGNMENT_MISSING = "assignment_missing"
    ASSIGNMENT_CONFLICT = "assignment_conflict"
    ASSIGNMENT_RATIO_INVALID = "assignment_ratio_invalid"
    ASSIGNMENT_PERIOD_SPLIT = "assignment_period_split"
    LOCKED_PERIOD_CHANGE = "locked_period_change"


class PerformanceCalculationBlocked(RuntimeError):
    """A business-safety gate that prevents publishing performance results."""

    def __init__(
        self,
        code: PerformanceBlockCode,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class PerformanceCalculationResult:
    enterprise_id: str
    period_id: str
    store_id: str
    certified_run_id: str
    policy_version_id: str
    result_count: int
    created_count: int
    superseded_count: int
    idempotent: bool
    batch_checksum_sha256: str
    result_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PeriodContext:
    period_id: str
    enterprise_id: str
    store_id: str
    store_name: str
    period_start: date
    period_end: date
    status: str


@dataclass(frozen=True, slots=True)
class _CertifiedRun:
    run_id: str
    code_sha: str
    input_manifest_sha256: str
    rule_set_sha256: str
    metrics: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    content_sha256: str
    original_name: str
    source_uri: str


@dataclass(frozen=True, slots=True)
class _Policy:
    policy_version_id: str
    version: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class _PnlCell:
    pnl_cell_id: str
    sku_key: str
    metric: str
    definition_id: str
    value: Decimal
    evidence: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class _Assignment:
    assignment_id: str
    person_id: str
    allocation_ratio: Decimal
    version: int
    checksum_sha256: str
    effective_from: date
    effective_to: date
    source_snapshot_id: str
    source_sheet: str | None
    source_row_no: int
    source_content_sha256: str
    tier: int
    status: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    scope_key: str
    person_id: str
    product_id: str
    collected_amount: Decimal
    refund_amount: Decimal
    direct_cost: Decimal
    allocated_cost: Decimal
    operating_profit: Decimal
    evidence: Mapping[str, object]
    checksum_sha256: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): item for key, item in decoded.items()}


def _json_array(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _store_name(definition_json: object, store_id: str) -> str:
    definition = _json_object(definition_json)
    value = definition.get("store_name")
    return str(value).strip() if str(value or "").strip() else store_id


def _period_context(
    database: DuckDBMemory,
    *,
    enterprise_id: str,
    period_id: str,
) -> _PeriodContext:
    row = database.execute(
        """
        SELECT period.period_id, contract.enterprise_id, period.store_id,
               contract.definition_json, period.period_start, period.period_end,
               period.status
        FROM accounting_period period
        JOIN reconciliation_contract contract
          ON contract.contract_id = period.contract_id
        WHERE period.period_id = ?
        """,
        [period_id],
    ).fetchone()
    if row is None:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PERIOD_NOT_FOUND,
            "找不到指定账期，不能计算人员绩效。",
            details={"period_id": period_id},
        )
    actual_enterprise = str(row[1])
    if actual_enterprise != enterprise_id:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PERIOD_ENTERPRISE_MISMATCH,
            "指定账期不属于当前企业。",
            details={"period_id": period_id},
        )
    store_id = str(row[2])
    return _PeriodContext(
        period_id=str(row[0]),
        enterprise_id=actual_enterprise,
        store_id=store_id,
        store_name=_store_name(row[3], store_id),
        period_start=row[4],
        period_end=row[5],
        status=str(row[6]),
    )


def _policy(database: DuckDBMemory, context: _PeriodContext) -> _Policy:
    rows = database.execute(
        """
        SELECT policy_version_id, version, definition_json, checksum_sha256
        FROM performance_policy_version
        WHERE enterprise_id = ?
          AND policy_code = ?
          AND status = 'approved'
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to >= ?)
        ORDER BY version DESC, policy_version_id
        """,
        [
            context.enterprise_id,
            POLICY_CODE,
            context.period_start,
            context.period_end,
        ],
    ).fetchall()
    if not rows:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.POLICY_MISSING,
            "当前账期没有经过批准、且覆盖整月的人员绩效规则。",
            details={"policy_code": POLICY_CODE},
        )
    if len(rows) != 1:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.POLICY_CONFLICT,
            "当前账期同时命中多个人员绩效规则版本。",
            details={"policy_version_ids": [str(row[0]) for row in rows]},
        )
    policy_id, version, raw_definition, stored_checksum = rows[0]
    definition = _json_object(raw_definition)
    expected_definition = builtin_performance_policy_definition()
    actual_checksum = _sha256_json(definition)
    expected_checksum = builtin_performance_policy_checksum()
    if (
        definition != expected_definition
        or str(stored_checksum) != actual_checksum
        or actual_checksum != expected_checksum
    ):
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.POLICY_DRIFT,
            "数据库中的绩效规则与当前确定性执行器不一致，已停止计算。",
            details={
                "policy_version_id": str(policy_id),
                "stored_checksum": str(stored_checksum),
                "actual_checksum": actual_checksum,
                "expected_checksum": expected_checksum,
            },
        )
    return _Policy(
        policy_version_id=str(policy_id),
        version=int(version),
        checksum_sha256=actual_checksum,
    )


def _certified_run(
    database: DuckDBMemory,
    context: _PeriodContext,
) -> _CertifiedRun:
    row = database.execute(
        """
        SELECT run_id, code_sha, input_manifest_sha256, rule_set_sha256,
               metrics_json
        FROM run_log
        WHERE period_id = ?
          AND run_kind = 'reconcile'
          AND status = 'succeeded'
        ORDER BY finished_at DESC NULLS LAST, started_at DESC, run_id DESC
        LIMIT 1
        """,
        [context.period_id],
    ).fetchone()
    if row is None:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.CERTIFIED_RUN_MISSING,
            "当前账期还没有完成核对，不能计算人员绩效。",
        )
    metrics = _json_object(row[4])
    certifiable = metrics.get("certifiable") is True
    identities = tuple(str(value or "").strip() for value in row[1:4])
    if not certifiable or not all(identities):
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.UNCERTIFIED_INPUT,
            "当前最新核对结果尚未认证，不能进入人员绩效。",
            details={"run_id": str(row[0]), "certifiable": certifiable},
        )
    return _CertifiedRun(
        run_id=str(row[0]),
        code_sha=identities[0],
        input_manifest_sha256=identities[1],
        rule_set_sha256=identities[2],
        metrics=metrics,
    )


def _source_snapshots(database: DuckDBMemory) -> dict[str, _SourceSnapshot]:
    return {
        str(snapshot_id): _SourceSnapshot(
            content_sha256=str(content_sha256),
            original_name=str(original_name or ""),
            source_uri=str(source_uri or ""),
        )
        for snapshot_id, content_sha256, original_name, source_uri in database.execute(
            """
            SELECT snapshot_id, content_sha256, original_name, source_uri
            FROM source_snapshot
            """
        ).fetchall()
    }


def _normalized_cell_evidence(
    raw_evidence: object,
    *,
    pnl_cell_id: str,
    snapshots: Mapping[str, _SourceSnapshot],
) -> tuple[Mapping[str, object], ...]:
    normalized: list[Mapping[str, object]] = []
    for evidence_index, raw_item in enumerate(_json_array(raw_evidence)):
        if not isinstance(raw_item, dict):
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.EVIDENCE_MISSING,
                "认证金额包含无法验证的来源证据，已停止计算。",
                details={
                    "pnl_cell_id": pnl_cell_id,
                    "evidence_index": evidence_index,
                    "reason": "invalid_evidence_shape",
                },
            )
        snapshot_id = str(
            raw_item.get("snapshot_id") or raw_item.get("file_id") or ""
        ).strip()
        try:
            row_no = int(raw_item.get("row_no") or 0)
        except (TypeError, ValueError):
            row_no = 0
        snapshot = snapshots.get(snapshot_id)
        source_member = str(raw_item.get("source_member") or "")
        source_sheet = str(raw_item.get("source_sheet") or "")
        source_name = source_member or (snapshot.original_name if snapshot else "")
        rule_version = str(raw_item.get("rule_version") or "")
        invalid_reason = (
            "snapshot_missing"
            if snapshot is None
            else "row_missing"
            if row_no <= 0
            else "obsolete_evidence_version"
            if rule_version != NORMALIZATION_RULE_VERSION
            else "xlsx_sheet_missing"
            if source_name.lower().endswith(".xlsx") and not source_sheet
            else None
        )
        if invalid_reason is not None:
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.EVIDENCE_MISSING,
                "认证金额包含无法定位或版本过期的来源证据，已停止计算。",
                details={
                    "pnl_cell_id": pnl_cell_id,
                    "evidence_index": evidence_index,
                    "reason": invalid_reason,
                },
            )
        assert snapshot is not None
        normalized.append(
            {
                "snapshot_id": snapshot_id,
                "content_sha256": snapshot.content_sha256,
                "source_member": source_member,
                "source_sheet": source_sheet,
                "row_no": row_no,
                "field": str(raw_item.get("field") or ""),
                "rule_version": rule_version,
            }
        )
    if not normalized:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.EVIDENCE_MISSING,
            "认证金额缺少可定位到原文件行的来源证据。",
            details={"pnl_cell_id": pnl_cell_id},
        )
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                str(item["snapshot_id"]),
                str(item["source_member"]),
                str(item["source_sheet"]),
                int(str(item["row_no"])),
                str(item["field"]),
            ),
        )
    )


def _product_cells(
    database: DuckDBMemory,
    *,
    context: _PeriodContext,
    certified_run: _CertifiedRun,
    snapshots: Mapping[str, _SourceSnapshot],
) -> dict[str, dict[str, _PnlCell]]:
    rows = database.execute(
        """
        SELECT pnl_cell_id, sku_key, metric, definition_id, value, evidence_json
        FROM pnl_cell
        WHERE run_id = ? AND period_id = ? AND store_id = ?
        ORDER BY sku_key, metric, definition_id, pnl_cell_id
        """,
        [certified_run.run_id, context.period_id, context.store_id],
    ).fetchall()
    product_rows = [
        row for row in rows if str(row[1]).strip().casefold() not in _PRODUCT_TOTAL_KEYS
    ]
    if not product_rows:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PRODUCT_GRAIN_MISSING,
            "当前认证结果只有店铺汇总，没有商品明细，不能计算人员绩效。",
            details={"run_id": certified_run.run_id},
        )
    grouped: dict[str, dict[str, _PnlCell]] = defaultdict(dict)
    for row in product_rows:
        cell_id = str(row[0])
        sku_key = str(row[1]).strip()
        metric = str(row[2]).strip()
        if metric not in _REQUIRED_METRICS:
            continue
        if metric in grouped[sku_key]:
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.PRODUCT_METRIC_CONFLICT,
                "同一商品的同一指标存在多个认证值。",
                details={"sku_key": sku_key, "metric": metric},
            )
        grouped[sku_key][metric] = _PnlCell(
            pnl_cell_id=cell_id,
            sku_key=sku_key,
            metric=metric,
            definition_id=str(row[3]),
            value=amount(Decimal(str(row[4]))),
            evidence=_normalized_cell_evidence(
                row[5],
                pnl_cell_id=cell_id,
                snapshots=snapshots,
            ),
        )
    if not grouped:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PRODUCT_GRAIN_MISSING,
            "当前认证结果没有可执行的商品指标。",
        )
    return dict(grouped)


def _validate_product_metrics(
    grouped: Mapping[str, Mapping[str, _PnlCell]],
) -> None:
    missing_cost = sorted(
        sku_key for sku_key, metrics in grouped.items() if "cost" not in metrics
    )
    if missing_cost:
        sales_total = sum_money(
            abs(metrics["sales"].value)
            for metrics in grouped.values()
            if "sales" in metrics
        )
        covered_sales = sum_money(
            abs(metrics["sales"].value)
            for metrics in grouped.values()
            if "sales" in metrics and "cost" in metrics
        )
        coverage = (
            Decimal("0.000000")
            if sales_total == 0
            else (covered_sales / sales_total).quantize(Decimal("0.000001"))
        )
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.COST_COVERAGE_INSUFFICIENT,
            "部分商品没有认证成本，人员利润会失真，已停止计算。",
            details={
                "coverage_ratio": format(coverage, ".6f"),
                "required_ratio": "1.000000",
                "sku_keys": missing_cost,
            },
        )
    missing_metrics = {
        sku_key: sorted(_REQUIRED_METRICS - set(metrics))
        for sku_key, metrics in grouped.items()
        if _REQUIRED_METRICS - set(metrics)
    }
    if missing_metrics:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PRODUCT_METRIC_MISSING,
            "部分商品缺少完整的认证经营指标。",
            details={"missing_metrics": missing_metrics},
        )
    for sku_key, metrics in grouped.items():
        expected_profit = sum_money(
            metrics[metric].value for metric in _PROFIT_COMPONENTS
        )
        actual_profit = metrics["profit"].value
        if expected_profit != actual_profit:
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.PRODUCT_PROFIT_MISMATCH,
                "商品利润与认证组成项不一致，已停止计算。",
                details={
                    "sku_key": sku_key,
                    "expected_profit": format(expected_profit, ".4f"),
                    "actual_profit": format(actual_profit, ".4f"),
                },
            )


def _product_id(
    database: DuckDBMemory,
    *,
    enterprise_id: str,
    sku_key: str,
) -> str:
    rows = database.execute(
        """
        SELECT product_id, status
        FROM canonical_product
        WHERE enterprise_id = ?
          AND lower(trim(merchant_product_code)) = lower(trim(?))
        """,
        [enterprise_id, sku_key],
    ).fetchall()
    usable = [row for row in rows if str(row[1]) == "active"]
    if len(usable) != 1:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.PRODUCT_IDENTITY_MISSING,
            "商品编码无法唯一对应到商品主数据。",
            details={"sku_key": sku_key, "match_count": len(usable)},
        )
    return str(usable[0][0])


def _assignment_tier(
    *,
    assignment_store_id: object,
    assignment_store_name: object,
    context: _PeriodContext,
) -> int:
    store_id = str(assignment_store_id or "").strip()
    store_name = str(assignment_store_name or "").strip().casefold()
    if store_id and store_id == context.store_id:
        return 3
    if store_name and store_name == context.store_name.strip().casefold():
        return 2
    if not store_id and not store_name:
        return 1
    return 0


def _assignments(
    database: DuckDBMemory,
    *,
    context: _PeriodContext,
    product_id: str,
    snapshots: Mapping[str, _SourceSnapshot],
) -> tuple[_Assignment, ...]:
    rows = database.execute(
        """
        SELECT assignment.assignment_id, assignment.person_id,
               assignment.allocation_ratio, assignment.version,
               assignment.checksum_sha256, assignment.effective_from,
               assignment.effective_to, assignment.source_snapshot_id,
               assignment.source_sheet, assignment.source_row_no,
               assignment.store_id, assignment.store_name, assignment.status
        FROM responsibility_assignment_version assignment
        JOIN person_identity person
          ON person.person_id = assignment.person_id
         AND person.enterprise_id = assignment.enterprise_id
        LEFT JOIN performance_source_import reference_import
          ON reference_import.enterprise_id = assignment.enterprise_id
         AND reference_import.snapshot_id = assignment.source_snapshot_id
         AND reference_import.source_kind = 'performance_reference'
         AND reference_import.status = 'succeeded'
        WHERE assignment.enterprise_id = ?
          AND assignment.product_id = ?
          AND assignment.status IN ('active', 'conflict')
          AND person.status = 'active'
          AND reference_import.snapshot_id IS NULL
          AND assignment.effective_from <= ?
          AND assignment.effective_to >= ?
        ORDER BY assignment.effective_from, assignment.effective_to,
                 assignment.version, assignment.assignment_id
        """,
        [
            context.enterprise_id,
            product_id,
            context.period_end,
            context.period_start,
        ],
    ).fetchall()
    candidates: list[_Assignment] = []
    for row in rows:
        tier = _assignment_tier(
            assignment_store_id=row[10],
            assignment_store_name=row[11],
            context=context,
        )
        if tier == 0:
            continue
        snapshot_id = str(row[7])
        row_no = int(row[9])
        snapshot = snapshots.get(snapshot_id)
        source_sheet = str(row[8] or "").strip()
        if (
            snapshot is None
            or row_no <= 0
            or (
                snapshot.original_name.lower().endswith(".xlsx")
                and not source_sheet
            )
        ):
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.EVIDENCE_MISSING,
                "商品归属缺少可定位到原文件行的来源证据。",
                details={"assignment_id": str(row[0])},
            )
        candidates.append(
            _Assignment(
                assignment_id=str(row[0]),
                person_id=str(row[1]),
                allocation_ratio=Decimal(str(row[2])).quantize(
                    Decimal("0.0001")
                ),
                version=int(row[3]),
                checksum_sha256=str(row[4]),
                effective_from=row[5],
                effective_to=row[6],
                source_snapshot_id=snapshot_id,
                source_sheet=source_sheet or None,
                source_row_no=row_no,
                source_content_sha256=snapshot.content_sha256,
                tier=tier,
                status=str(row[12]),
            )
        )
    if not candidates:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.ASSIGNMENT_MISSING,
            "该商品在当前店铺和账期没有负责人归属。",
            details={"product_id": product_id},
        )

    signatures: set[tuple[tuple[str, str], ...]] = set()
    selected_versions: dict[str, _Assignment] = {}
    day = context.period_start
    while day <= context.period_end:
        applicable = [
            item
            for item in candidates
            if item.effective_from <= day <= item.effective_to
        ]
        if not applicable:
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.ASSIGNMENT_MISSING,
                "商品负责人归属没有覆盖完整账期。",
                details={"product_id": product_id, "uncovered_date": day.isoformat()},
            )
        highest_tier = max(item.tier for item in applicable)
        selected = [item for item in applicable if item.tier == highest_tier]
        if any(item.status == "conflict" for item in selected):
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.ASSIGNMENT_CONFLICT,
                "该商品存在尚未解决的负责人归属冲突。",
                details={
                    "product_id": product_id,
                    "date": day.isoformat(),
                    "assignment_ids": [
                        item.assignment_id for item in selected
                    ],
                },
            )
        active = [item for item in selected if item.status == "active"]
        person_ids = [item.person_id for item in active]
        if not active or len(set(person_ids)) != len(person_ids):
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.ASSIGNMENT_CONFLICT,
                "同一天存在重复或无法唯一解释的负责人归属版本。",
                details={"product_id": product_id, "date": day.isoformat()},
            )
        ratio_total = sum(
            (item.allocation_ratio for item in active),
            _MONEY_ZERO,
        )
        if ratio_total != _RATIO_ONE:
            raise PerformanceCalculationBlocked(
                PerformanceBlockCode.ASSIGNMENT_RATIO_INVALID,
                "商品负责人分配比例合计不等于 100%。",
                details={
                    "product_id": product_id,
                    "date": day.isoformat(),
                    "ratio_total": format(ratio_total, ".4f"),
                },
            )
        signature = tuple(
            sorted(
                (item.person_id, format(item.allocation_ratio, ".4f"))
                for item in active
            )
        )
        signatures.add(signature)
        for item in active:
            selected_versions[item.assignment_id] = item
        day += timedelta(days=1)
    if len(signatures) != 1:
        raise PerformanceCalculationBlocked(
            PerformanceBlockCode.ASSIGNMENT_PERIOD_SPLIT,
            "负责人或分配比例在月中发生变化；当前只有月度商品金额，无法安全拆分。",
            details={"product_id": product_id},
        )
    return tuple(
        sorted(
            selected_versions.values(),
            key=lambda item: (
                item.person_id,
                item.effective_from,
                item.version,
                item.assignment_id,
            ),
        )
    )


def _ratios(assignments: Iterable[_Assignment]) -> tuple[tuple[str, Decimal], ...]:
    by_person: dict[str, Decimal] = {}
    for item in assignments:
        by_person.setdefault(item.person_id, item.allocation_ratio)
    return tuple(sorted(by_person.items()))


def _allocate(
    total: Decimal,
    ratios: tuple[tuple[str, Decimal], ...],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    allocated = _MONEY_ZERO
    for index, (person_id, ratio) in enumerate(ratios):
        if index == len(ratios) - 1:
            value = subtract_money(total, allocated)
        else:
            with localcontext() as context:
                context.prec = 48
                value = amount(total * ratio)
            allocated = sum_money((allocated, value))
        result[person_id] = value
    return result


def _scope_key(
    *,
    enterprise_id: str,
    period_id: str,
    store_id: str,
    product_id: str,
    person_id: str,
) -> str:
    payload = "\x1f".join(
        (enterprise_id, period_id, store_id, product_id, person_id)
    )
    return f"performance-v1:{hashlib.sha256(payload.encode()).hexdigest()}"


def _assignment_evidence(
    assignments: Iterable[_Assignment],
    *,
    person_id: str,
) -> list[dict[str, object]]:
    return [
        {
            "assignment_id": item.assignment_id,
            "version": item.version,
            "allocation_ratio": format(item.allocation_ratio, ".4f"),
            "checksum_sha256": item.checksum_sha256,
            "effective_from": item.effective_from.isoformat(),
            "effective_to": item.effective_to.isoformat(),
            "source": {
                "snapshot_id": item.source_snapshot_id,
                "content_sha256": item.source_content_sha256,
                "source_sheet": item.source_sheet or "",
                "row_no": item.source_row_no,
            },
        }
        for item in assignments
        if item.person_id == person_id
    ]


def _candidate_checksum_payload(
    *,
    context: _PeriodContext,
    certified_run: _CertifiedRun,
    policy: _Policy,
    person_id: str,
    product_id: str,
    values: Mapping[str, Decimal],
    pnl_evidence: list[dict[str, object]],
    assignment_evidence: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "engine_version": ENGINE_VERSION,
        "enterprise_id": context.enterprise_id,
        "period_id": context.period_id,
        "store_id": context.store_id,
        "person_id": person_id,
        "product_id": product_id,
        "certified_run": {
            "run_id": certified_run.run_id,
            "code_sha": certified_run.code_sha,
            "input_manifest_sha256": certified_run.input_manifest_sha256,
            "rule_set_sha256": certified_run.rule_set_sha256,
        },
        "policy": {
            "policy_version_id": policy.policy_version_id,
            "version": policy.version,
            "checksum_sha256": policy.checksum_sha256,
        },
        "values": {
            key: format(value, ".4f") for key, value in sorted(values.items())
        },
        "pnl_cells": pnl_evidence,
        "assignments": assignment_evidence,
    }


def _candidates(
    database: DuckDBMemory,
    *,
    context: _PeriodContext,
    certified_run: _CertifiedRun,
    policy: _Policy,
    grouped: Mapping[str, Mapping[str, _PnlCell]],
    snapshots: Mapping[str, _SourceSnapshot],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for sku_key, metrics in sorted(grouped.items()):
        product_id = _product_id(
            database,
            enterprise_id=context.enterprise_id,
            sku_key=sku_key,
        )
        assignments = _assignments(
            database,
            context=context,
            product_id=product_id,
            snapshots=snapshots,
        )
        ratios = _ratios(assignments)
        totals = {
            "collected_amount": metrics["sales"].value,
            "refund_amount": metrics["refund"].value,
            "direct_cost": metrics["cost"].value,
            "allocated_cost": sum_money(
                metrics[metric].value
                for metric in ("platform_fee", "freight", "advertising")
            ),
        }
        allocations = {
            name: _allocate(value, ratios) for name, value in totals.items()
        }
        pnl_evidence: list[dict[str, object]] = [
            {
                "pnl_cell_id": cell.pnl_cell_id,
                "metric": cell.metric,
                "definition_id": cell.definition_id,
                "value": format(cell.value, ".4f"),
                "sources": list(cell.evidence),
            }
            for _, cell in sorted(metrics.items())
        ]
        for person_id, _ in ratios:
            values = {
                name: allocated[person_id]
                for name, allocated in allocations.items()
            }
            values["operating_profit"] = sum_money(
                (
                    values["collected_amount"],
                    values["refund_amount"],
                    values["direct_cost"],
                    values["allocated_cost"],
                )
            )
            assignment_evidence = _assignment_evidence(
                assignments,
                person_id=person_id,
            )
            payload = _candidate_checksum_payload(
                context=context,
                certified_run=certified_run,
                policy=policy,
                person_id=person_id,
                product_id=product_id,
                values=values,
                pnl_evidence=pnl_evidence,
                assignment_evidence=assignment_evidence,
            )
            candidates.append(
                _Candidate(
                    scope_key=_scope_key(
                        enterprise_id=context.enterprise_id,
                        period_id=context.period_id,
                        store_id=context.store_id,
                        product_id=product_id,
                        person_id=person_id,
                    ),
                    person_id=person_id,
                    product_id=product_id,
                    collected_amount=values["collected_amount"],
                    refund_amount=values["refund_amount"],
                    direct_cost=values["direct_cost"],
                    allocated_cost=values["allocated_cost"],
                    operating_profit=values["operating_profit"],
                    evidence=payload,
                    checksum_sha256=_sha256_json(payload),
                )
            )
    return tuple(sorted(candidates, key=lambda item: item.scope_key))


def _current_heads(
    database: DuckDBMemory,
    *,
    context: _PeriodContext,
) -> dict[str, tuple[str, str]]:
    return {
        str(scope_key): (str(result_id), str(checksum))
        for scope_key, result_id, checksum in database.execute(
            """
            SELECT head.scope_key, result.result_id, result.checksum_sha256
            FROM performance_result_head head
            JOIN performance_result result ON result.result_id = head.result_id
            WHERE head.scope_key LIKE 'performance-v1:%'
              AND result.enterprise_id = ?
              AND result.period_id = ?
              AND result.store_id = ?
              AND result.evidence_policy_version = ?
              AND result.engine_version = ?
            """,
            [
                context.enterprise_id,
                context.period_id,
                context.store_id,
                NORMALIZATION_RULE_VERSION,
                ENGINE_VERSION,
            ],
        ).fetchall()
    }


def _next_version(
    database: DuckDBMemory,
    *,
    context: _PeriodContext,
    candidate: _Candidate,
) -> int:
    row = database.execute(
        """
        SELECT coalesce(
            max(
                try_cast(
                    json_extract_string(evidence_json, '$.result_version')
                    AS INTEGER
                )
            ),
            0
        )
        FROM performance_result
        WHERE enterprise_id = ?
          AND period_id = ?
          AND store_id = ?
          AND person_id = ?
          AND product_id = ?
        """,
        [
            context.enterprise_id,
            context.period_id,
            context.store_id,
            candidate.person_id,
            candidate.product_id,
        ],
    ).fetchone()
    return int(row[0] if row else 0) + 1


def _batch_checksum(
    *,
    context: _PeriodContext,
    certified_run: _CertifiedRun,
    policy: _Policy,
    candidates: Iterable[_Candidate],
) -> str:
    return _sha256_json(
        {
            "engine_version": ENGINE_VERSION,
            "enterprise_id": context.enterprise_id,
            "period_id": context.period_id,
            "store_id": context.store_id,
            "certified_run_id": certified_run.run_id,
            "policy_version_id": policy.policy_version_id,
            "results": [
                [candidate.scope_key, candidate.checksum_sha256]
                for candidate in candidates
            ],
        }
    )


def calculate_certified_performance(
    workbench: WorkbenchPaths,
    *,
    enterprise_id: str,
    period_id: str,
) -> PerformanceCalculationResult:
    """Calculate and publish current personnel performance for one store-period.

    A closed/restated period may be read idempotently, but any absent, added,
    removed, or changed result is rejected.  Corrections therefore require an
    explicit upstream restatement workflow before this function is called.
    """

    lock_path = workbench.locks / "duckdb-writer.lock"
    with _writer_lock(lock_path):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            context = _period_context(
                database,
                enterprise_id=enterprise_id,
                period_id=period_id,
            )
            policy = _policy(database, context)
            certified_run = _certified_run(database, context)
            snapshots = _source_snapshots(database)
            grouped = _product_cells(
                database,
                context=context,
                certified_run=certified_run,
                snapshots=snapshots,
            )
            _validate_product_metrics(grouped)
            candidates = _candidates(
                database,
                context=context,
                certified_run=certified_run,
                policy=policy,
                grouped=grouped,
                snapshots=snapshots,
            )
            candidate_heads = {
                item.scope_key: item.checksum_sha256 for item in candidates
            }
            current_heads = _current_heads(database, context=context)
            current_checksums = {
                scope_key: checksum
                for scope_key, (_, checksum) in current_heads.items()
            }
            batch_checksum = _batch_checksum(
                context=context,
                certified_run=certified_run,
                policy=policy,
                candidates=candidates,
            )
            if context.status in {"closed", "restated"}:
                if candidate_heads != current_checksums:
                    raise PerformanceCalculationBlocked(
                        PerformanceBlockCode.LOCKED_PERIOD_CHANGE,
                        "账期已经锁定；人员绩效发生变化，必须先走更正和重述流程。",
                        details={
                            "period_status": context.status,
                            "current_scope_count": len(current_checksums),
                            "candidate_scope_count": len(candidate_heads),
                        },
                    )
                return PerformanceCalculationResult(
                    enterprise_id=context.enterprise_id,
                    period_id=context.period_id,
                    store_id=context.store_id,
                    certified_run_id=certified_run.run_id,
                    policy_version_id=policy.policy_version_id,
                    result_count=len(candidates),
                    created_count=0,
                    superseded_count=0,
                    idempotent=True,
                    batch_checksum_sha256=batch_checksum,
                    result_ids=tuple(
                        sorted(result_id for result_id, _ in current_heads.values())
                    ),
                )

            created = 0
            superseded = 0
            result_ids: list[str] = []
            candidate_scopes = set(candidate_heads)
            with database.transaction() as connection:
                for candidate in candidates:
                    existing = current_heads.get(candidate.scope_key)
                    if existing is not None and existing[1] == candidate.checksum_sha256:
                        result_ids.append(existing[0])
                        continue
                    version = _next_version(
                        database,
                        context=context,
                        candidate=candidate,
                    )
                    evidence = dict(candidate.evidence)
                    evidence["result_version"] = version
                    result_id = (
                        "performance_result_"
                        + hashlib.sha256(
                            (
                                candidate.scope_key
                                + "\x1f"
                                + str(version)
                                + "\x1f"
                                + candidate.checksum_sha256
                            ).encode()
                        ).hexdigest()[:32]
                    )
                    if existing is not None:
                        connection.execute(
                            """
                            UPDATE performance_result
                            SET status = 'superseded'
                            WHERE result_id = ?
                            """,
                            [existing[0]],
                        )
                        superseded += 1
                    connection.execute(
                        """
                        INSERT INTO performance_result (
                            result_id, run_id, enterprise_id, period_id,
                            person_id, store_id, product_id, policy_version_id,
                            collected_amount, refund_amount, direct_cost,
                            allocated_cost, operating_profit, completeness_ratio,
                            status, evidence_policy_version, evidence_json,
                            checksum_sha256, engine_version
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                'complete', ?, ?, ?, ?)
                        """,
                        [
                            result_id,
                            certified_run.run_id,
                            context.enterprise_id,
                            context.period_id,
                            candidate.person_id,
                            context.store_id,
                            candidate.product_id,
                            policy.policy_version_id,
                            candidate.collected_amount,
                            candidate.refund_amount,
                            candidate.direct_cost,
                            candidate.allocated_cost,
                            candidate.operating_profit,
                            _COMPLETE,
                            NORMALIZATION_RULE_VERSION,
                            _canonical_json(evidence),
                            candidate.checksum_sha256,
                            ENGINE_VERSION,
                        ],
                    )
                    connection.execute(
                        """
                        INSERT INTO performance_result_head (
                            scope_key, result_id, updated_at
                        )
                        VALUES (?, ?, now())
                        ON CONFLICT (scope_key) DO UPDATE SET
                            result_id = excluded.result_id,
                            updated_at = now()
                        """,
                        [candidate.scope_key, result_id],
                    )
                    created += 1
                    result_ids.append(result_id)

                for scope_key in sorted(set(current_heads) - candidate_scopes):
                    old_result_id = current_heads[scope_key][0]
                    connection.execute(
                        """
                        UPDATE performance_result
                        SET status = 'superseded'
                        WHERE result_id = ?
                        """,
                        [old_result_id],
                    )
                    connection.execute(
                        "DELETE FROM performance_result_head WHERE scope_key = ?",
                        [scope_key],
                    )
                    superseded += 1

            return PerformanceCalculationResult(
                enterprise_id=context.enterprise_id,
                period_id=context.period_id,
                store_id=context.store_id,
                certified_run_id=certified_run.run_id,
                policy_version_id=policy.policy_version_id,
                result_count=len(candidates),
                created_count=created,
                superseded_count=superseded,
                idempotent=created == 0 and superseded == 0,
                batch_checksum_sha256=batch_checksum,
                result_ids=tuple(sorted(result_ids)),
            )
