from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .analytics import (
    AnalyticsValidationError,
    analytics_catalog,
    analytics_overview,
)
from .auto_compute import AutoComputeRunner
from .bootstrap import stable_identity
from .config import HarnessConfig
from .evidence_policy import (
    LEARNING_POLICY_VERSION,
    NORMALIZATION_RULE_VERSION,
    PERFORMANCE_ENGINE_VERSION,
    evidence_binding_digest,
)
from .evidence_viewer import (
    EvidencePreviewError,
    EvidencePreviewNotFoundError,
    EvidencePreviewSecurityError,
    EvidenceViewer,
)
from .judgment.gateway import OpenAICompatibleGateway, business_failure_reason
from .judgment.orchestration import task_policies
from .learning import LearningEvaluator
from .llm_runtime import (
    LlmRuntimeError,
    RuntimeLlmStore,
    discover_models,
)
from .memory.database import DuckDBMemory
from .performance import sync_performance_sources
from .snapshot import SnapshotStore
from .trust import trust_matrix
from .workbench import require_initialized

logger = logging.getLogger(__name__)


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["explain", "defer", "reject"]
    reason: str = Field(min_length=2, max_length=2000)


class BusinessDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=2, max_length=4000)


class InputRevisionSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=1000)


class LlmDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol: Literal["auto", "openai_compatible", "anthropic"] = "auto"
    base_url: str = Field(alias="baseUrl", min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, alias="apiKey")


class LlmConfigurationRequest(LlmDiscoveryRequest):
    protocol: Literal["openai_compatible", "anthropic"]
    selected_model: str = Field(alias="selectedModel", min_length=1, max_length=300)
    reviewer_model: str | None = Field(
        default=None,
        alias="reviewerModel",
        max_length=300,
    )
    enabled: bool = True


def _decimal_text(value: object) -> str:
    if value is None:
        return "0.0000"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _required_row(cursor: Any) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("aggregate query unexpectedly returned no row")
    return cast(tuple[Any, ...], row)


def _source_basename(value: object) -> str:
    """Expose a useful file label without leaking a remote absolute path."""

    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def _source_kind_label(source_kind: str) -> str:
    return {
        "alipay_ledger": "支付宝流水",
        "wechat_ledger": "微信流水",
        "baobei_order": "订单明细",
        "taobao_platform_fee": "平台费用",
        "freight_statement": "物流费用",
        "product_cost": "商品成本",
        "advertising_statement": "广告费用",
    }.get(source_kind, "业务文件")


def _source_location_label(source_uri: object) -> str:
    normalized = str(source_uri or "").casefold()
    if any(token in normalized for token in ("修改后", "加工", "processed")):
        return "历史加工目录"
    if any(token in normalized for token in ("原始数据", "\\raw\\", "/raw/")):
        return "原始数据目录"
    if any(token in normalized for token in ("onedrive", "共享", "归档", "archive")):
        return "共享或归档目录"
    return "只读来源快照"


def _business_decision_answer(subject_kind: str, raw: object) -> str | None:
    if not raw:
        return None
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        return None
    answer = payload.get("answer")
    if subject_kind == "freight_period_attribution" and answer == "business_occurrence_date":
        return "按实际业务发生日归入原账期；原账期已关闭时，生成关联原账期的调整记录。"
    if (
        subject_kind == "shared_cost_attribution"
        and answer == "direct_then_positive_net_sales_share"
    ):
        return (
            "候选口径：优先按订单或商品直接归属；剩余共享成本可按当期正净销售额占比"
            "分配。当前版本尚未启用自动分配，无法直接归属的金额仍会阻断正式利润。"
        )
    if subject_kind == "fund_account_effectivity":
        if answer == "not_applicable":
            return "当前以支付宝和微信平台钱包为核对证据，银行账户映射不适用。"
        if answer == "explicit_effective_dated_mapping_required":
            return "银行三方模式必须先配置带生效日期的账户与店铺关系，否则阻断核对。"
    return str(answer) if answer is not None else None


def _review_payload(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _review_business_copy(
    reason_code: str,
    raw_explanation: object,
) -> tuple[str, str, str]:
    payload = _review_payload(raw_explanation)
    missing_sides = {
        str(side).casefold() for side in payload.get("missing_sides", []) if isinstance(side, str)
    }
    scope = str(payload.get("scope", "")).casefold()

    if reason_code == "amount_mismatch":
        return (
            "订单与平台钱包金额不一致",
            "同一笔业务在订单明细和平台钱包中的金额没有完全对上。",
            "核对退款、平台费用、优惠补贴和跨月结算，确认差额对应的真实业务事件。",
        )
    if reason_code == "ambiguous_bridge":
        return (
            "一笔平台结算对应到多笔资金记录",
            "系统找到了多个可能的对应项，现有证据不足以唯一确认。",
            "核对结算批次号、到账日期和收款账户，确认唯一对应关系后再记录原因。",
        )
    if reason_code in {"missing_cash_bridge_key", "missing_settlement_batch_key"}:
        return (
            "平台流水缺少可用于核对的结算编号",
            "这笔记录没有稳定的结算批次号，系统无法安全地与资金记录关联。",
            "重新导出包含结算批次号的账单，或核对平台是否提供了等价的唯一编号。",
        )
    if reason_code == "missing_cash_side" or "cash" in missing_sides:
        return (
            "平台有结算记录，但资金流水未找到",
            "平台侧显示已经结算，当前资金文件中没有找到对应到账记录。",
            "核对到账日期、收款账户和跨月到账情况；当前钱包模式下该项不参与正式门禁。",
        )
    if reason_code == "missing_order_side" or "order" in missing_sides:
        return (
            "平台钱包有记录，订单明细未找到",
            "平台钱包中存在这笔收支，但本月订单文件里没有找到对应订单。",
            "先检查订单文件是否完整，再核对退款、撤销订单或跨月到账情况。",
        )
    if reason_code == "missing_platform_side" or "platform" in missing_sides:
        return (
            "订单明细有记录，平台钱包未找到",
            "订单文件中存在这笔交易，但平台钱包流水里没有找到对应收支。",
            "先检查平台钱包文件是否完整，再核对尚未结算、跨月到账或平台侧冲销情况。",
        )
    if reason_code == "missing_side" and scope == "order_platform":
        return (
            "订单与平台钱包暂未对应",
            "订单明细与平台钱包之间缺少可以确认的对应记录。",
            "核对订单号、退款状态和交易日期，确认是否属于跨月或撤销业务。",
        )
    return (
        "这笔记录暂时无法自动对应",
        "现有文件没有提供足够信息，系统已保留原始记录并停止自动确认。",
        "先核对来源文件是否完整，再根据交易日期、订单号和业务状态补充说明。",
    )


def _llm_business_suggestion(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    suggestion = " ".join(value.strip().split())
    if not 2 <= len(suggestion) <= 300:
        return None
    return suggestion


def _llm_failure_message(value: str | None) -> str:
    reason = business_failure_reason(RuntimeError(value or "模型没有返回有效结果"))
    if "确定性" in reason:
        return reason
    return f"{reason}；确定性核对不受影响。"


def _compute_error_business_copy(raw_error: object) -> str | None:
    """Return a safe operator-facing error without leaking paths or SQL."""

    if not raw_error:
        return None
    detail = str(raw_error)
    if "服务重启前任务未正常结束" in detail:
        return "服务重启前任务未完成；原始文件已经保留，可以重新计算。"
    fault_id = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:8].upper()
    return f"这项处理没有完成；原始文件已经保留。故障编号：{fault_id}"


VISIBLE_RESULT_LIMIT = 100


def create_app(config: HarnessConfig, web_dist: Path | None = None) -> FastAPI:
    workbench = require_initialized(config)
    enterprise_id = stable_identity("enterprise", "local-enterprise")
    llm_store = RuntimeLlmStore(workbench.root)
    llm_gateway = OpenAICompatibleGateway(runtime_store=llm_store)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
    compute_runner = AutoComputeRunner(config, workbench)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        compute_runner.start()
        try:
            yield
        finally:
            compute_runner.stop()

    app = FastAPI(
        title="电商财务对账 Harness",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.compute_runner = compute_runner
    from .certify.router import build_certify_router
    from .claims.router import build_claims_router
    from .edge.receive import build_edge_router
    from .home import build_home_router
    from .intelligence_api import build_intelligence_router
    from .period.router import build_period_router

    app.include_router(build_edge_router(workbench))
    app.include_router(build_period_router(workbench))
    app.include_router(build_claims_router(workbench))
    app.include_router(build_home_router(workbench))
    app.include_router(build_intelligence_router(workbench))
    app.include_router(build_certify_router(workbench))

    @contextmanager
    def memory() -> Iterator[DuckDBMemory]:
        with DuckDBMemory(workbench.database) as database:
            yield database

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok\n"

    @app.get("/readyz", response_class=PlainTextResponse)
    def readyz() -> str:
        if not (workbench.root / ".fa-workbench.json").is_file():
            raise HTTPException(status_code=503, detail="工作台未初始化")
        with memory() as database:
            version = database.execute("SELECT max(version) FROM harness_schema_version").fetchone()
            database.execute("SELECT 1").fetchone()
        if not version or version[0] is None:
            raise HTTPException(status_code=503, detail="账本 schema 未就绪")
        if web_dist is not None and not (web_dist / "index.html").is_file():
            raise HTTPException(status_code=503, detail="前端产物未就绪")
        return "ready\n"

    @app.get("/api/v1/status")
    def status() -> dict[str, Any]:
        with memory() as database:
            version_row = database.execute(
                "SELECT max(version) FROM harness_schema_version"
            ).fetchone()
            source_rows = database.execute(
                "SELECT source_uri FROM source_snapshot ORDER BY captured_at DESC LIMIT 20"
            ).fetchall()
        if not source_rows:
            mode = "empty"
        elif all(str(row[0]).startswith("synthetic://") for row in source_rows):
            mode = "synthetic"
        else:
            mode = "real"
        runtime_llm = llm_store.public_status()
        return {
            "mode": mode,
            "workspace": str(workbench.root),
            "schemaVersion": int(version_row[0]) if version_row and version_row[0] else None,
            "llmEnabled": runtime_llm.enabled,
            "llmConfigured": runtime_llm.configured,
            "autonomyLevel": config.llm.autonomy_level,
            "reconciliationMode": config.reconciliation.mode,
            "bankCashStatus": (
                "required" if config.reconciliation.mode == "bank_three_way" else "not_applicable"
            ),
            "readOnlySourceEnforced": os.getenv("FA_SOURCE_READONLY_ENFORCED") == "1",
            "localComputeEnabled": compute_runner.enabled,
            "localComputeRunning": compute_runner.running,
            "configuredPeriods": config.source.scope.resolved_periods(),
            "updatedAt": datetime.now(UTC).isoformat(),
        }

    def public_llm_status() -> dict[str, Any]:
        state = llm_store.public_status()
        activity = llm_store.last_activity()
        activity_message = activity["message"] if activity else None
        if activity and activity["status"] == "error":
            activity_message = _llm_failure_message(activity_message)
        return {
            "enabled": state.enabled,
            "configured": state.configured,
            "protocol": state.protocol,
            "baseUrl": state.base_url or None,
            "selectedModel": state.selected_model or None,
            "reviewerModel": state.reviewer_model or None,
            "keyConfigured": state.key_configured,
            "completionSupported": state.completion_supported,
            "detail": state.detail,
            "updatedAt": (
                datetime.fromtimestamp(
                    llm_store.config_path.stat().st_mtime,
                    tz=UTC,
                ).isoformat()
                if llm_store.config_path.exists()
                else None
            ),
            "lastTaskStatus": activity["status"] if activity else None,
            "lastTaskPurpose": activity["purpose"] if activity else None,
            "lastTaskModel": activity["model"] if activity else None,
            "lastTaskMessage": activity_message,
            "lastTaskAt": activity["updated_at"] if activity else None,
        }

    def request_key(secret: SecretStr | None) -> str:
        if secret is not None and secret.get_secret_value():
            return secret.get_secret_value()
        try:
            current = llm_store.load()
        except LlmRuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if current is None:
            raise HTTPException(status_code=422, detail="需要提供模型服务密钥")
        return current.api_key

    @app.get("/api/v1/llm/config")
    def llm_configuration() -> dict[str, Any]:
        return public_llm_status()

    @app.post("/api/v1/llm/discover")
    def llm_discovery(request: LlmDiscoveryRequest) -> dict[str, Any]:
        try:
            result = discover_models(
                protocol=request.protocol,
                base_url=request.base_url,
                api_key=request_key(request.api_key),
            )
        except LlmRuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "protocol": result.protocol,
            "baseUrl": result.base_url,
            "models": list(result.models),
            "completionSupported": result.completion_supported,
        }

    @app.put("/api/v1/llm/config")
    def apply_llm_configuration(
        request: LlmConfigurationRequest,
    ) -> dict[str, Any]:
        key = request_key(request.api_key)
        try:
            discovery = discover_models(
                protocol=request.protocol,
                base_url=request.base_url,
                api_key=key,
            )
            if request.selected_model not in discovery.models:
                raise LlmRuntimeError("所选模型不在服务端当前返回的可用范围内")
            reviewer_model = (request.reviewer_model or "").strip()
            if reviewer_model and reviewer_model not in discovery.models:
                raise LlmRuntimeError("复核模型不在服务端当前返回的可用范围内")
            llm_store.save(
                protocol=discovery.protocol,
                base_url=discovery.base_url,
                api_key=key,
                selected_model=request.selected_model,
                reviewer_model=reviewer_model,
                enabled=request.enabled,
            )
            verification = llm_gateway.complete_json(
                purpose="configuration_verification",
                model="runtime-configured-model",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你只负责验证模型连接。不要推断业务或金额。"
                            '只返回 JSON：{"status":"ok"}。'
                        ),
                    },
                    {
                        "role": "user",
                        "content": "执行一次不包含业务数据的连接检查。",
                    },
                ],
            )
            if verification.status == "ok":
                llm_store.record_activity(
                    status="ok",
                    purpose="configuration_verification",
                    model=verification.model,
                    message="模型配置已生效，并已完成一次真实响应验证。",
                    request_id=verification.request_id,
                )
            else:
                llm_store.record_activity(
                    status=("disabled" if verification.status == "disabled" else "error"),
                    purpose="configuration_verification",
                    model=verification.model or request.selected_model,
                    message=_llm_failure_message(verification.reason),
                    request_id=verification.request_id,
                )
        except LlmRuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return public_llm_status()

    @app.delete("/api/v1/llm/config")
    def disable_llm_configuration() -> dict[str, Any]:
        try:
            llm_store.disable()
            state = llm_store.public_status()
            llm_store.record_activity(
                status="disabled",
                purpose="configuration",
                model=state.selected_model,
                message="模型服务已停用；确定性核对继续运行。",
            )
        except LlmRuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return public_llm_status()

    @app.post("/api/v1/llm/test")
    def test_llm_configuration() -> dict[str, Any]:
        result = llm_gateway.complete_json(
            purpose="connection_test",
            model="runtime-configured-model",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你只负责验证模型连接。不要推断业务、金额或原因。"
                        '只返回 JSON：{"status":"ok"}。'
                    ),
                },
                {
                    "role": "user",
                    "content": "执行一次无业务数据的连接检查。",
                },
            ],
        )
        if result.status != "ok":
            failure_message = _llm_failure_message(result.reason)
            llm_store.record_activity(
                status="disabled" if result.status == "disabled" else "error",
                purpose="connection_test",
                model=result.model,
                message=failure_message,
                request_id=result.request_id,
            )
            return {
                "status": result.status,
                "model": result.model,
                "message": failure_message,
                "requestId": result.request_id,
            }
        llm_store.record_activity(
            status="ok",
            purpose="connection_test",
            model=result.model,
            message="模型已实际响应，可用于生成业务说明草案。",
            request_id=result.request_id,
        )
        return {
            "status": "ok",
            "model": result.model,
            "message": "模型已实际响应，可用于生成业务说明草案。",
            "requestId": result.request_id,
        }

    @app.post("/api/v1/compute/run")
    def run_local_compute() -> dict[str, Any]:
        result = compute_runner.trigger()
        if not result.accepted:
            raise HTTPException(status_code=409, detail=result.message)
        return {
            "accepted": result.accepted,
            "running": result.running,
            "message": result.message,
        }

    @app.get("/api/v1/compute/jobs")
    def compute_jobs(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[dict[str, Any]]:
        with memory() as database:
            rows = _rows(
                database.execute(
                    """
                    SELECT job_id, cycle_id, job_kind, store_id, period_token,
                           status, progress_percent, business_label, detail,
                           strftime(
                               created_at AT TIME ZONE 'UTC',
                               '%Y-%m-%d %H:%M:%S'
                           ) || '+00' AS created_at,
                           CASE WHEN started_at IS NULL THEN NULL ELSE
                               strftime(
                                   started_at AT TIME ZONE 'UTC',
                                   '%Y-%m-%d %H:%M:%S'
                               ) || '+00'
                           END AS started_at,
                           CASE WHEN finished_at IS NULL THEN NULL ELSE
                               strftime(
                                   finished_at AT TIME ZONE 'UTC',
                                   '%Y-%m-%d %H:%M:%S'
                               ) || '+00'
                           END AS finished_at,
                           error_detail
                    FROM compute_job
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [limit],
                )
            )
        return [
            {
                "jobId": str(row["job_id"]),
                "cycleId": str(row["cycle_id"]),
                "kind": str(row["job_kind"]),
                "storeId": str(row["store_id"]) if row["store_id"] else None,
                "period": str(row["period_token"]) if row["period_token"] else None,
                "status": str(row["status"]),
                "progressPercent": int(row["progress_percent"]),
                "label": str(row["business_label"]),
                "detail": str(row["detail"]) if row["detail"] else None,
                "createdAt": str(row["created_at"]),
                "startedAt": str(row["started_at"]) if row["started_at"] else None,
                "finishedAt": str(row["finished_at"]) if row["finished_at"] else None,
                "error": _compute_error_business_copy(row["error_detail"]),
            }
            for row in rows
        ]

    @app.get("/api/v1/progress")
    def progress(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period_token: Annotated[
            str | None,
            Query(alias="period", pattern=r"^\d{4}$"),
        ] = None,
    ) -> dict[str, Any]:
        with memory() as database:
            period = database.execute(
                """
                SELECT p.period_id,
                       coalesce(
                           json_extract_string(c.definition_json, '$.store_name'),
                           p.store_id
                       ) AS store_name,
                       p.period_start, p.status, p.contract_id, p.store_id
                FROM accounting_period p
                JOIN reconciliation_contract c ON c.contract_id = p.contract_id
                WHERE (? IS NULL OR p.store_id = ?)
                  AND (
                      ? IS NULL
                      OR strftime(p.period_start, '%y%m') = ?
                  )
                ORDER BY p.period_start DESC, p.revision_no DESC
                LIMIT 1
                """,
                [store_id, store_id, period_token, period_token],
            ).fetchone()
            if period:
                source_count = int(
                    database.fetchone_required(
                        """
                        SELECT count(DISTINCT snapshot_id)
                        FROM input_revision
                        WHERE period_id = ?
                        """,
                        [period[0]],
                    )[0]
                )
            else:
                source_count = int(
                    database.fetchone_required("SELECT count(*) FROM source_snapshot")[0]
                )
            profile_counts: dict[str, int] = {}
            latest_parser = database.execute(
                """
                SELECT parser_version
                FROM source_profile
                ORDER BY created_at DESC, parser_version DESC
                LIMIT 1
                """
            ).fetchone()
            if latest_parser:
                profile_counts = {
                    str(row[0]): int(row[1])
                    for row in database.execute(
                        """
                        SELECT status, count(*)
                        FROM source_profile
                        WHERE parser_version = ?
                        GROUP BY status
                        """,
                        [latest_parser[0]],
                    ).fetchall()
                }
            approved_rules = int(
                database.fetchone_required(
                    "SELECT count(*) FROM rule_version WHERE status = 'approved'"
                )[0]
            )
            baseline_count = int(
                database.fetchone_required("SELECT count(*) FROM baseline WHERE status = 'frozen'")[
                    0
                ]
            )
            latest_recon = (
                database.execute(
                    """
                    SELECT run_id, metrics_json
                    FROM run_log
                    WHERE run_kind = 'reconcile'
                      AND status = 'succeeded'
                      AND contract_id = ?
                      AND period_id = ?
                    ORDER BY finished_at DESC NULLS LAST, started_at DESC,
                             run_id DESC
                    LIMIT 1
                    """,
                    [period[4], period[0]],
                ).fetchone()
                if period
                else None
            )
            recon_certifiable = bool(
                latest_recon
                and latest_recon[1]
                and json.loads(str(latest_recon[1])).get("certifiable")
            )
            successful_blind = int(
                database.fetchone_required(
                    """
                    SELECT count(*) FROM run_log
                    WHERE run_kind = 'blind' AND status = 'succeeded'
                    """
                )[0]
            )
            checklist = (
                _rows(
                    database.execute(
                        """
                        SELECT status, count(*) AS count
                        FROM (
                            SELECT result.status,
                                   row_number() OVER (
                                       PARTITION BY result.requirement_id
                                       ORDER BY result.checked_at DESC
                                   ) AS position
                            FROM checklist_result result
                            JOIN checklist_requirement requirement
                              ON requirement.requirement_id =
                                 result.requirement_id
                            JOIN accounting_period period
                              ON period.period_id = result.period_id
                            WHERE result.period_id = ?
                              AND requirement.required = true
                              AND requirement.effective_from <=
                                  period.period_end
                              AND (
                                  requirement.effective_to IS NULL
                                  OR requirement.effective_to >=
                                     period.period_start
                              )
                        ) latest
                        WHERE position = 1
                        GROUP BY status
                        """,
                        [period[0]],
                    )
                )
                if period
                else []
            )
            pending_business_decisions = int(
                database.fetchone_required(
                    """
                    SELECT count(*)
                    FROM (
                        SELECT DISTINCT subject_kind, question, business_impact,
                               coalesce(cast(decision_json AS VARCHAR), '')
                        FROM business_decision
                        WHERE status = 'pending'
                    ) current_business_policies
                    """
                )[0]
            )
            decided_business_decisions = int(
                database.fetchone_required(
                    """
                    SELECT count(*)
                    FROM (
                        SELECT DISTINCT subject_kind, question, business_impact,
                               coalesce(cast(decision_json AS VARCHAR), '')
                        FROM business_decision
                        WHERE status = 'decided'
                    ) current_business_policies
                    """
                )[0]
            )
            unresolved = (
                database.fetchone_required(
                    """
                    SELECT count(*), coalesce(sum(abs(u.amount)), 0)
                    FROM unresolved_balance u
                    JOIN reconciliation_balance b
                      ON b.balance_id = u.balance_id
                    WHERE u.status = 'open'
                      AND b.run_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM review_decision d
                          WHERE d.unresolved_id = u.unresolved_id
                            AND d.decision IN ('explain', 'reject')
                      )
                    """,
                    [latest_recon[0]],
                )
                if latest_recon
                else (0, Decimal("0"))
            )
            latest_cycle = database.execute(
                """
                SELECT cycle_id
                FROM compute_job
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            compute_counts: dict[str, int] = {}
            current_jobs: list[dict[str, Any]] = []
            if latest_cycle:
                compute_counts = {
                    str(row[0]): int(row[1])
                    for row in database.execute(
                        """
                        SELECT status, count(*)
                        FROM compute_job
                        WHERE cycle_id = ?
                          AND (? IS NULL OR store_id IS NULL OR store_id = ?)
                          AND (
                              ? IS NULL
                              OR period_token IS NULL
                              OR period_token = ?
                          )
                        GROUP BY status
                        """,
                        [
                            latest_cycle[0],
                            store_id,
                            store_id,
                            period_token,
                            period_token,
                        ],
                    ).fetchall()
                }
                current_jobs = _rows(
                    database.execute(
                        """
                        SELECT job_id, business_label, detail, status,
                               progress_percent, store_id, period_token
                        FROM compute_job
                        WHERE cycle_id = ?
                          AND status IN ('queued', 'running', 'failed')
                          AND (? IS NULL OR store_id IS NULL OR store_id = ?)
                          AND (
                              ? IS NULL
                              OR period_token IS NULL
                              OR period_token = ?
                          )
                        ORDER BY
                          CASE status
                            WHEN 'running' THEN 0
                            WHEN 'failed' THEN 1
                            ELSE 2
                          END,
                          created_at
                        LIMIT 20
                        """,
                        [
                            latest_cycle[0],
                            store_id,
                            store_id,
                            period_token,
                            period_token,
                        ],
                    )
                )
        checklist_counts = {str(item["status"]): int(item["count"]) for item in checklist}
        checklist_total = sum(checklist_counts.values())
        checklist_blocked = sum(
            checklist_counts.get(item, 0) for item in ("pending", "missing", "failed")
        )
        gates = [
            {
                "id": "freeze",
                "label": "文件准备",
                "state": "complete" if source_count else "active",
                "detail": (
                    (
                        f"已安全保存 {source_count} 份原始文件；"
                        f"{profile_counts.get('matched', 0)} 份已识别用途，"
                        f"{profile_counts.get('unsupported', 0)} 份需要另行确认。"
                    )
                    if source_count and profile_counts
                    else f"已安全保存 {source_count} 份原始文件。"
                    if source_count
                    else "尚未收到真实文件，不能开始本月核对。"
                ),
            },
            {
                "id": "checklist",
                "label": "本月文件完整性",
                "state": (
                    "complete"
                    if checklist_total and checklist_blocked == 0
                    else "blocked"
                    if checklist_blocked
                    else "pending"
                ),
                "detail": (
                    f"本月应有 {checklist_total} 项文件，仍有 {checklist_blocked} 项未满足。"
                    if checklist_total
                    else "尚未确认本月应收到哪些文件。"
                ),
            },
            {
                "id": "rules",
                "label": "口径与规则",
                "state": (
                    "blocked"
                    if pending_business_decisions
                    else "complete"
                    if approved_rules and decided_business_decisions
                    else "pending"
                ),
                "detail": (
                    f"已有 {decided_business_decisions} 项系统口径、"
                    f"{approved_rules} 组批准规则；"
                    f"仍有 {pending_business_decisions} 项需要负责人拍板。"
                    if pending_business_decisions
                    else (
                        f"已有 {decided_business_decisions} 项系统口径、"
                        f"{approved_rules} 组批准规则生效。"
                    )
                ),
            },
            {
                "id": "reconcile",
                "label": (
                    "订单与平台钱包核对"
                    if config.reconciliation.mode == "platform_wallet"
                    else "订单、平台与银行资金核对"
                ),
                "state": (
                    "complete" if recon_certifiable else "blocked" if latest_recon else "pending"
                ),
                "detail": (
                    (
                        "本月订单与支付宝/微信钱包金额均已核清；银行流水不在当前核对范围。"
                        if config.reconciliation.mode == "platform_wallet"
                        else "本月文件、业务口径和三方金额均已核清。"
                    )
                    if recon_certifiable
                    else "本月仍有无法对应的记录或未解释差额，暂不能确认结果。"
                    if latest_recon
                    else (
                        "尚未开始本月订单与平台钱包核对。"
                        if config.reconciliation.mode == "platform_wallet"
                        else "尚未开始本月订单、平台与银行资金核对。"
                    )
                ),
            },
            {
                "id": "baseline",
                "label": "历史结果确认",
                "state": "complete" if baseline_count else "pending",
                "detail": (
                    "历史差异已经逐项确认，可作为后续复核标准。"
                    if baseline_count
                    else "历史结果仍需逐项确认，尚不能作为正式复核标准。"
                ),
            },
            {
                "id": "blind",
                "label": "新月份验证",
                "state": "complete" if successful_blind else "pending",
                "detail": (
                    "新月份已按预先确定的检查标准通过验证。"
                    if successful_blind
                    else "2026-05 尚未完成独立验证。"
                ),
            },
        ]
        return {
            "shop": str(period[1]) if period else None,
            "period": str(period[2])[:7] if period else None,
            "periodState": str(period[3]) if period else None,
            "gates": gates,
            "sourceCount": source_count,
            "unresolvedCount": int(unresolved[0]),
            "unexplainedAmount": _decimal_text(unresolved[1]),
            "compute": {
                "enabled": compute_runner.enabled,
                "running": compute_runner.running,
                "cycleId": str(latest_cycle[0]) if latest_cycle else None,
                "total": sum(compute_counts.values()),
                "queued": compute_counts.get("queued", 0),
                "active": compute_counts.get("running", 0),
                "succeeded": compute_counts.get("succeeded", 0),
                "failed": compute_counts.get("failed", 0),
                "current": [
                    {
                        "jobId": str(item["job_id"]),
                        "label": str(item["business_label"]),
                        "detail": str(item["detail"]) if item["detail"] else None,
                        "status": str(item["status"]),
                        "progressPercent": int(item["progress_percent"]),
                        "storeId": (str(item["store_id"]) if item["store_id"] else None),
                        "period": (str(item["period_token"]) if item["period_token"] else None),
                    }
                    for item in current_jobs
                ],
            },
        }

    @app.get("/api/v1/business-decisions")
    def business_decisions() -> list[dict[str, Any]]:
        with memory() as database:
            records = _rows(
                database.execute(
                    """
                    SELECT decision_id, subject_kind, question, business_impact,
                           status, decision_json, decided_by,
                           cast(decided_at AS VARCHAR) AS decided_at
                    FROM (
                        SELECT *,
                               row_number() OVER (
                                   PARTITION BY status, subject_kind, question,
                                                business_impact,
                                                coalesce(
                                                    cast(decision_json AS VARCHAR),
                                                    ''
                                                )
                                   ORDER BY decided_at DESC NULLS LAST,
                                            created_at DESC,
                                            decision_id DESC
                               ) AS semantic_position
                        FROM business_decision
                    ) current_business_policies
                    WHERE semantic_position = 1
                    ORDER BY
                        CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                        subject_kind
                    """
                )
            )
        return [
            {
                "decisionId": str(item["decision_id"]),
                "subjectKind": str(item["subject_kind"]),
                "question": str(item["question"]),
                "businessImpact": str(item["business_impact"]),
                "status": str(item["status"]),
                "answer": _business_decision_answer(
                    str(item["subject_kind"]),
                    item["decision_json"],
                ),
                "decidedBy": item["decided_by"],
                "decidedAt": (str(item["decided_at"]) if item["decided_at"] is not None else None),
            }
            for item in records
        ]

    @app.get("/api/v1/input-revisions")
    def input_revisions() -> list[dict[str, Any]]:
        """Return only streams that still require an explicit file choice."""

        with memory() as database:
            records = _rows(
                database.execute(
                    """
                    SELECT revision.contract_id, revision.period_id,
                           revision.logical_input_key, revision.source_kind,
                           strftime(period.period_start, '%Y-%m') AS period,
                           revision.revision_id, snapshot.original_name,
                           snapshot.source_uri,
                           coalesce(state.status, revision.status) AS status,
                           coalesce(state.reason, revision.reason) AS reason,
                           coalesce(
                               (
                                   SELECT artifact.row_count
                                   FROM normalized_artifact artifact
                                   WHERE artifact.input_revision_id =
                                         revision.revision_id
                                   ORDER BY artifact.created_at DESC,
                                            artifact.artifact_id DESC
                                   LIMIT 1
                               ),
                               0
                           ) AS row_count
                    FROM input_revision revision
                    JOIN accounting_period period
                      ON period.period_id = revision.period_id
                    JOIN source_snapshot snapshot
                      ON snapshot.snapshot_id = revision.snapshot_id
                    LEFT JOIN input_revision_state state
                      ON state.revision_id = revision.revision_id
                    WHERE period.status = 'open'
                      AND coalesce(state.status, revision.status) <> 'rejected'
                      AND EXISTS (
                        SELECT 1
                        FROM normalized_artifact visible_artifact
                        WHERE visible_artifact.input_revision_id =
                              revision.revision_id
                          AND visible_artifact.rule_version = ?
                      )
                      AND EXISTS (
                        SELECT 1
                        FROM input_revision candidate
                        LEFT JOIN input_revision_state candidate_state
                          ON candidate_state.revision_id = candidate.revision_id
                        WHERE candidate.contract_id = revision.contract_id
                          AND candidate.period_id = revision.period_id
                          AND candidate.logical_input_key =
                              revision.logical_input_key
                          AND candidate.source_kind = revision.source_kind
                          AND coalesce(
                              candidate_state.status,
                              candidate.status
                          ) = 'candidate'
                          AND EXISTS (
                            SELECT 1
                            FROM normalized_artifact candidate_artifact
                            WHERE candidate_artifact.input_revision_id =
                                  candidate.revision_id
                              AND candidate_artifact.rule_version = ?
                          )
                    )
                    ORDER BY period.period_start DESC, revision.source_kind,
                             revision.logical_input_key, revision.revision_no
                    """,
                    [
                        NORMALIZATION_RULE_VERSION,
                        NORMALIZATION_RULE_VERSION,
                    ],
                )
            )
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in records:
            key = (
                str(item["contract_id"]),
                str(item["period_id"]),
                str(item["logical_input_key"]),
                str(item["source_kind"]),
            )
            group = grouped.setdefault(
                key,
                {
                    "groupId": hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:24],
                    "period": str(item["period"]),
                    "sourceKind": str(item["source_kind"]),
                    "label": _source_kind_label(str(item["source_kind"])),
                    "candidates": [],
                },
            )
            group["candidates"].append(
                {
                    "revisionId": str(item["revision_id"]),
                    "originalName": _source_basename(item["original_name"]),
                    "sourceLabel": _source_location_label(item["source_uri"]),
                    "status": str(item["status"]),
                    "reason": (str(item["reason"]) if item["reason"] is not None else None),
                    "rowCount": int(item["row_count"]),
                }
            )
        return list(grouped.values())

    @app.post("/api/v1/input-revisions/{revision_id}/select", status_code=204)
    def select_input_revision(
        revision_id: str,
        request: InputRevisionSelectionRequest,
    ) -> None:
        actor = "local_business_owner"
        reason = request.reason.strip()
        with memory() as database, database.transaction() as connection:
            target = connection.execute(
                """
                SELECT revision.contract_id, revision.period_id,
                       revision.logical_input_key, revision.source_kind,
                       coalesce(period_state.status, period.status),
                       coalesce(state.status, revision.status),
                       state.approved_by, snapshot.original_name
                FROM input_revision revision
                JOIN accounting_period period
                  ON period.period_id = revision.period_id
                JOIN source_snapshot snapshot
                  ON snapshot.snapshot_id = revision.snapshot_id
                LEFT JOIN input_revision_state state
                  ON state.revision_id = revision.revision_id
                LEFT JOIN accounting_period_state period_state
                  ON period_state.period_id = revision.period_id
                WHERE revision.revision_id = ?
                """,
                [revision_id],
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="候选文件版本不存在")
            if str(target[4]) != "open":
                raise HTTPException(
                    status_code=409,
                    detail="账期已关闭，不能更换当前文件版本",
                )
            if str(target[5]) == "rejected":
                raise HTTPException(
                    status_code=409,
                    detail="已拒绝的文件版本不能设为当前版本",
                )
            if str(target[5]) == "current" and target[6] is not None:
                return
            selected_label = _source_basename(target[7])
            superseded_reason = f"已人工选择 {selected_label}：{reason}"
            connection.execute(
                """
                UPDATE input_revision_state
                SET status = CASE
                        WHEN revision_id = ? THEN 'current'
                        ELSE 'superseded'
                    END,
                    reason = CASE
                        WHEN revision_id = ? THEN ?
                        ELSE ?
                    END,
                    approved_by = ?,
                    updated_at = current_timestamp
                WHERE revision_id IN (
                    SELECT candidate.revision_id
                    FROM input_revision candidate
                    JOIN input_revision_state candidate_state
                      ON candidate_state.revision_id = candidate.revision_id
                    WHERE candidate.contract_id = ?
                      AND candidate.period_id = ?
                      AND candidate.logical_input_key = ?
                      AND candidate.source_kind = ?
                      AND candidate_state.status <> 'rejected'
                )
                """,
                [
                    revision_id,
                    revision_id,
                    reason,
                    superseded_reason,
                    actor,
                    target[0],
                    target[1],
                    target[2],
                    target[3],
                ],
            )

    @app.post("/api/v1/business-decisions/{decision_id}", status_code=204)
    def decide_business_question(
        decision_id: str,
        request: BusinessDecisionRequest,
    ) -> None:
        payload = json.dumps(
            {"answer": request.answer.strip()},
            ensure_ascii=False,
            sort_keys=True,
        )
        with memory() as database, database.transaction() as connection:
            current = connection.execute(
                """
                SELECT status FROM business_decision WHERE decision_id = ?
                """,
                [decision_id],
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="业务裁决项不存在")
            if current[0] != "pending":
                raise HTTPException(status_code=409, detail="该业务口径已经拍板")
            connection.execute(
                """
                UPDATE business_decision
                SET status = 'decided', decision_json = ?,
                    decided_by = 'local_business_owner',
                    decided_at = current_timestamp
                WHERE decision_id = ?
                """,
                [payload, decision_id],
            )
            connection.execute(
                """
                INSERT INTO business_decision_event (
                    event_id, decision_id, action, payload_json, actor
                )
                VALUES (?, ?, 'decide', ?, 'local_business_owner')
                """,
                [f"event_{uuid.uuid4().hex}", decision_id, payload],
            )

    @app.get("/api/v1/balances")
    def balances(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
    ) -> list[dict[str, Any]]:
        with memory() as database:
            records = _rows(
                database.execute(
                    """
                    SELECT balance_id, balance_key, expected_amount, actual_amount,
                           matched_amount, difference_amount, status
                    FROM reconciliation_balance
                    WHERE run_id = (
                        SELECT run_id FROM run_log
                        JOIN accounting_period selected_period
                          ON selected_period.period_id = run_log.period_id
                        WHERE run_log.run_kind = 'reconcile'
                          AND run_log.status = 'succeeded'
                          AND (? IS NULL OR selected_period.store_id = ?)
                          AND (
                              ? IS NULL
                              OR strftime(
                                  selected_period.period_start,
                                  '%y%m'
                              ) = ?
                          )
                        ORDER BY finished_at DESC NULLS LAST, started_at DESC
                        LIMIT 1
                    )
                    ORDER BY abs(difference_amount) DESC, balance_key
                    LIMIT ?
                    """,
                    [store_id, store_id, period, period, VISIBLE_RESULT_LIMIT],
                )
            )
        return [
            {
                "balanceId": str(item["balance_id"]),
                "balanceKey": str(item["balance_key"]),
                "expectedAmount": _decimal_text(item["expected_amount"]),
                "actualAmount": _decimal_text(item["actual_amount"]),
                "matchedAmount": _decimal_text(item["matched_amount"]),
                "differenceAmount": _decimal_text(item["difference_amount"]),
                "status": str(item["status"]),
            }
            for item in records
        ]

    def review_records(
        *,
        store_id: str | None = None,
        period: str | None = None,
        reason_code: str | None = None,
        limit: int | None = VISIBLE_RESULT_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit_clause = "LIMIT ?" if limit is not None else ""
        parameters: list[object] = [
            store_id,
            store_id,
            period,
            period,
            reason_code,
            reason_code,
        ]
        if limit is not None:
            parameters.extend((limit, offset))
        with memory() as database:
            records = _rows(
                database.execute(
                    f"""
                    WITH latest_run AS (
                        SELECT run_id, contract_id, period_id,
                               row_number() OVER (
                                   PARTITION BY contract_id, period_id
                                   ORDER BY finished_at DESC NULLS LAST,
                                            started_at DESC,
                                            run_id DESC
                               ) AS run_rank
                        FROM run_log
                        WHERE run_kind = 'reconcile'
                          AND status = 'succeeded'
                    )
                    SELECT u.unresolved_id, u.reason_code, u.amount, u.status,
                           u.explanation, period.store_id,
                           coalesce(
                               json_extract_string(
                                   contract.definition_json,
                                   '$.store_name'
                               ),
                               period.store_id
                           ) AS store_name,
                           strftime(period.period_start, '%y%m') AS period_token,
                           CASE
                               WHEN u.evidence_id IS NULL THEN 0
                               ELSE coalesce(
                                   nullif((
                                       SELECT count(*)
                                       FROM evidence_binding binding
                                       WHERE binding.evidence_id = u.evidence_id
                                   ), 0),
                                   json_array_length(e.payload_json),
                                   1
                               )
                           END AS evidence_count
                    FROM unresolved_balance u
                    JOIN reconciliation_balance b
                      ON b.balance_id = u.balance_id
                    JOIN latest_run current_run
                      ON current_run.run_id = b.run_id
                     AND current_run.run_rank = 1
                    JOIN accounting_period period
                      ON period.period_id = b.period_id
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = b.contract_id
                    LEFT JOIN evidence_record e
                      ON e.evidence_id = u.evidence_id
                    WHERE u.status = 'open'
                      AND (? IS NULL OR period.store_id = ?)
                      AND (
                          ? IS NULL
                          OR strftime(period.period_start, '%y%m') = ?
                      )
                      AND (? IS NULL OR u.reason_code = ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM review_decision d
                          WHERE d.unresolved_id = u.unresolved_id
                            AND d.decision IN ('explain', 'reject')
                      )
                    ORDER BY abs(u.amount) DESC, u.unresolved_id
                    {limit_clause}
                    {"OFFSET ?" if limit is not None else ""}
                    """,
                    parameters,
                )
            )
        result: list[dict[str, Any]] = []
        for item in records:
            title, summary, action = _review_business_copy(
                str(item["reason_code"]),
                item["explanation"],
            )
            result.append(
                {
                    "unresolvedId": str(item["unresolved_id"]),
                    "reasonCode": str(item["reason_code"]),
                    "amount": _decimal_text(item["amount"]),
                    "status": str(item["status"]),
                    "businessTitle": title,
                    "businessSummary": summary,
                    "suggestedAction": action,
                    "evidenceCount": int(item["evidence_count"]),
                    "storeId": str(item["store_id"]),
                    "storeName": str(item["store_name"]),
                    "period": str(item["period_token"]),
                }
            )
        return result

    @app.get("/api/v1/reviews")
    def reviews(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
        reason_code: Annotated[str | None, Query(alias="reasonCode")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = VISIBLE_RESULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return review_records(
            store_id=store_id,
            period=period,
            reason_code=reason_code,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/reviews/page")
    def review_page(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
        reason_code: Annotated[str | None, Query(alias="reasonCode")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = VISIBLE_RESULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        with memory() as database:
            total = int(
                database.fetchone_required(
                    """
                    WITH latest_run AS (
                        SELECT run_id, contract_id, period_id,
                               row_number() OVER (
                                   PARTITION BY contract_id, period_id
                                   ORDER BY finished_at DESC NULLS LAST,
                                            started_at DESC,
                                            run_id DESC
                               ) AS run_rank
                        FROM run_log
                        WHERE run_kind = 'reconcile'
                          AND status = 'succeeded'
                    )
                    SELECT count(*)
                    FROM unresolved_balance unresolved
                    JOIN reconciliation_balance balance
                      ON balance.balance_id = unresolved.balance_id
                    JOIN latest_run current_run
                      ON current_run.run_id = balance.run_id
                     AND current_run.run_rank = 1
                    JOIN accounting_period period
                      ON period.period_id = balance.period_id
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = balance.contract_id
                    WHERE unresolved.status = 'open'
                      AND (? IS NULL OR period.store_id = ?)
                      AND (
                          ? IS NULL
                          OR strftime(period.period_start, '%y%m') = ?
                      )
                      AND (? IS NULL OR unresolved.reason_code = ?)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM review_decision decision
                          WHERE decision.unresolved_id =
                                unresolved.unresolved_id
                            AND decision.decision IN ('explain', 'reject')
                      )
                    """,
                    [
                        store_id,
                        store_id,
                        period,
                        period,
                        reason_code,
                        reason_code,
                    ],
                )[0]
            )
        items = review_records(
            store_id=store_id,
            period=period,
            reason_code=reason_code,
            limit=limit,
            offset=offset,
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
            "hasMore": offset + len(items) < total,
        }

    @app.get("/api/v1/reviews/groups")
    def review_groups(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        """Group row-level differences into plain-language business problems."""

        with memory() as database:
            records = _rows(
                database.execute(
                    """
                    WITH latest_run AS (
                        SELECT run_id, contract_id, period_id,
                               row_number() OVER (
                                   PARTITION BY contract_id, period_id
                                   ORDER BY finished_at DESC NULLS LAST,
                                            started_at DESC,
                                            run_id DESC
                               ) AS run_rank
                        FROM run_log
                        WHERE run_kind = 'reconcile'
                          AND status = 'succeeded'
                    ),
                    visible AS (
                        SELECT unresolved.reason_code, unresolved.amount,
                               unresolved.evidence_id, period.store_id,
                               coalesce(
                                   nullif(
                                       json_extract_string(
                                           contract.definition_json,
                                           '$.store_name'
                                       ),
                                       ''
                                   ),
                                   period.store_id
                               ) AS store_name,
                               strftime(
                                   period.period_start,
                                   '%y%m'
                               ) AS period_token
                        FROM unresolved_balance unresolved
                        JOIN reconciliation_balance balance
                          ON balance.balance_id = unresolved.balance_id
                        JOIN latest_run current_run
                          ON current_run.run_id = balance.run_id
                         AND current_run.run_rank = 1
                        JOIN accounting_period period
                          ON period.period_id = balance.period_id
                        JOIN reconciliation_contract contract
                          ON contract.contract_id = balance.contract_id
                        WHERE unresolved.status = 'open'
                          AND (? IS NULL OR period.store_id = ?)
                          AND (
                              ? IS NULL
                              OR strftime(period.period_start, '%y%m') = ?
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM review_decision decision
                              WHERE decision.unresolved_id =
                                    unresolved.unresolved_id
                                AND decision.decision IN ('explain', 'reject')
                          )
                    ),
                    grouped AS (
                        SELECT store_id, store_name, period_token, reason_code,
                               count(*) AS item_count,
                               sum(amount) AS total_amount,
                               sum(abs(amount)) AS absolute_amount,
                               count(evidence_id) AS evidence_count
                        FROM visible
                        GROUP BY store_id, store_name, period_token, reason_code
                    )
                    SELECT *,
                           count(*) OVER () AS group_count,
                           sum(item_count) OVER () AS record_count
                    FROM grouped
                    ORDER BY absolute_amount DESC, item_count DESC,
                             store_name, period_token, reason_code
                    LIMIT ?
                    """,
                    [store_id, store_id, period, period, limit],
                )
            )
        groups: list[dict[str, object]] = []
        for item in records:
            reason_code = str(item["reason_code"])
            title, summary, action = _review_business_copy(reason_code, None)
            group_key = "|".join(
                [
                    str(item["store_id"]),
                    str(item["period_token"]),
                    reason_code,
                ]
            )
            groups.append(
                {
                    "groupId": hashlib.sha256(
                        group_key.encode("utf-8")
                    ).hexdigest()[:24],
                    "storeId": str(item["store_id"]),
                    "storeName": str(item["store_name"]),
                    "period": str(item["period_token"]),
                    "reasonCode": reason_code,
                    "businessTitle": title,
                    "businessSummary": summary,
                    "suggestedAction": action,
                    "itemCount": int(item["item_count"]),
                    "totalAmount": _decimal_text(item["total_amount"]),
                    "absoluteAmount": _decimal_text(item["absolute_amount"]),
                    "evidenceCount": int(item["evidence_count"]),
                }
            )
        return {
            "groupCount": int(records[0]["group_count"]) if records else 0,
            "recordCount": int(records[0]["record_count"]) if records else 0,
            "groups": groups,
        }

    @app.get("/api/v1/reviews/{unresolved_id}/evidence")
    def review_evidence(unresolved_id: str) -> dict[str, Any]:
        with memory() as database:
            current = database.execute(
                """
                SELECT u.unresolved_id, u.balance_id, u.status, u.evidence_id,
                       b.run_id, e.payload_json
                FROM unresolved_balance u
                JOIN reconciliation_balance b
                  ON b.balance_id = u.balance_id
                LEFT JOIN evidence_record e
                  ON e.evidence_id = u.evidence_id
                WHERE u.unresolved_id = ?
                  AND b.run_id = (
                      SELECT run_id
                      FROM run_log
                      WHERE run_kind = 'reconcile'
                        AND status = 'succeeded'
                        AND contract_id = b.contract_id
                        AND period_id = b.period_id
                      ORDER BY finished_at DESC NULLS LAST,
                               started_at DESC,
                               run_id DESC
                      LIMIT 1
                  )
                """,
                [unresolved_id],
            ).fetchone()
            if current is None:
                exists = database.execute(
                    "SELECT status FROM unresolved_balance WHERE unresolved_id = ?",
                    [unresolved_id],
                ).fetchone()
                if exists is None:
                    raise HTTPException(status_code=404, detail="待处理记录不存在")
                raise HTTPException(
                    status_code=409,
                    detail="该记录不属于当前有效核对结果，请刷新后重新选择",
                )
            evidence_id = str(current[3] or "")
            if not evidence_id:
                return {
                    "unresolvedId": str(current[0]),
                    "balanceId": str(current[1]),
                    "lineageStatus": "unavailable",
                    "sources": [],
                }

            stored_bindings = _rows(
                database.execute(
                    """
                    SELECT binding.ordinal, binding.snapshot_id, binding.artifact_id,
                           binding.source_member, binding.source_sheet, binding.row_no,
                           binding.field, binding.source_value,
                           binding.normalization_version, binding.rule_version_id,
                           snapshot.original_name, snapshot.source_uri,
                           artifact.dataset_kind
                    FROM evidence_binding binding
                    JOIN source_snapshot snapshot
                      ON snapshot.snapshot_id = binding.snapshot_id
                    LEFT JOIN normalized_artifact artifact
                      ON artifact.artifact_id = binding.artifact_id
                    WHERE binding.evidence_id = ?
                    ORDER BY binding.ordinal
                    """,
                    [evidence_id],
                )
            )
            bindings: list[dict[str, Any]] = []
            all_bindings_valid = True
            for binding in stored_bindings:
                try:
                    row_number = int(binding["row_no"])
                except (TypeError, ValueError):
                    all_bindings_valid = False
                    continue
                source_name = str(
                    binding["source_member"]
                    or binding["original_name"]
                    or binding["source_uri"]
                )
                if row_number <= 0:
                    all_bindings_valid = False
                    continue
                if (
                    str(binding["normalization_version"])
                    != NORMALIZATION_RULE_VERSION
                ):
                    all_bindings_valid = False
                    continue
                if source_name.lower().endswith(".xlsx") and not binding["source_sheet"]:
                    all_bindings_valid = False
                    continue
                bindings.append(binding)

            lineage_status = "frozen"
            if stored_bindings and (
                not all_bindings_valid or len(bindings) != len(stored_bindings)
            ):
                bindings = []
                lineage_status = "unavailable"
            elif not bindings:
                lineage_status = "legacy_inferred"
                try:
                    payload = json.loads(str(current[5] or "[]"))
                except (TypeError, ValueError):
                    payload = []
                if not isinstance(payload, list):
                    payload = []
                for ordinal, raw in enumerate(payload):
                    if not isinstance(raw, Mapping):
                        continue
                    snapshot_id = str(raw.get("file_id") or "")
                    if not snapshot_id:
                        continue
                    row_value = raw.get("row_no")
                    if row_value is None:
                        continue
                    try:
                        row_number = int(row_value)
                    except (TypeError, ValueError):
                        continue
                    if row_number <= 0:
                        continue
                    snapshot = database.execute(
                        """
                        SELECT original_name, source_uri
                        FROM source_snapshot
                        WHERE snapshot_id = ?
                        """,
                        [snapshot_id],
                    ).fetchone()
                    if snapshot is None:
                        continue
                    artifact = database.execute(
                        """
                        SELECT artifact_id, parquet_uri, dataset_kind, rule_version
                        FROM normalized_artifact
                        WHERE source_snapshot_id = ?
                        ORDER BY created_at DESC, artifact_id DESC
                        LIMIT 1
                        """,
                        [snapshot_id],
                    ).fetchone()
                    member = str(raw.get("source_member") or "")
                    sheet = str(raw.get("source_sheet") or "")
                    if artifact is not None and not (member and sheet):
                        try:
                            located = database.execute(
                                """
                                SELECT source_member, source_sheet
                                FROM read_parquet(?)
                                WHERE evidence_file_id = ?
                                  AND evidence_row_no = ?
                                LIMIT 1
                                """,
                                [
                                    str(artifact[1]),
                                    snapshot_id,
                                    row_number,
                                ],
                            ).fetchone()
                        except Exception:
                            located = None
                        if located is not None:
                            member = str(located[0] or "")
                            sheet = str(located[1] or "")
                    bindings.append(
                        {
                            "ordinal": ordinal,
                            "snapshot_id": snapshot_id,
                            "artifact_id": (str(artifact[0]) if artifact is not None else None),
                            "source_member": member,
                            "source_sheet": sheet,
                            "row_no": row_number,
                            "field": str(raw.get("field") or ""),
                            "source_value": str(raw.get("source_value") or ""),
                            "normalization_version": str(
                                raw.get("rule_version")
                                or (artifact[3] if artifact is not None else "")
                            ),
                            "rule_version_id": str(raw.get("rule_version_id") or ""),
                            "original_name": str(snapshot[0] or ""),
                            "source_uri": str(snapshot[1] or ""),
                            "dataset_kind": (str(artifact[2]) if artifact is not None else ""),
                        }
                    )
                if not bindings:
                    lineage_status = "unavailable"

        return {
            "unresolvedId": str(current[0]),
            "balanceId": str(current[1]),
            "lineageStatus": lineage_status,
            "sources": [
                {
                    "snapshotId": str(item["snapshot_id"]),
                    "artifactId": (str(item["artifact_id"]) if item["artifact_id"] else None),
                    "originalName": _source_basename(item["original_name"] or item["source_uri"]),
                    "sourceMember": str(item["source_member"] or "") or None,
                    "sourceSheet": str(item["source_sheet"] or "") or None,
                    "rowNumber": int(item["row_no"]),
                    "field": str(item["field"] or "") or None,
                    "normalizedValue": str(item["source_value"] or "") or None,
                    "normalizationVersion": (str(item["normalization_version"] or "") or None),
                    "ruleVersionId": str(item["rule_version_id"] or "") or None,
                    "sourceKind": str(item["dataset_kind"] or "") or None,
                }
                for item in bindings
            ],
        }

    @app.get("/api/v1/reviews/{unresolved_id}/evidence/{snapshot_id}/preview")
    def review_evidence_preview(
        unresolved_id: str,
        snapshot_id: str,
        radius: Annotated[int, Query(ge=0, le=100)] = 20,
        max_columns: Annotated[int, Query(alias="maxColumns", ge=1, le=80)] = 60,
    ) -> dict[str, Any]:
        """Return the bounded source window frozen for this review."""

        with memory() as database:
            evidence_integrity = database.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (
                        WHERE binding.normalization_version IS DISTINCT FROM ?
                           OR binding.row_no IS NULL
                           OR binding.row_no <= 0
                           OR (
                               lower(
                                   coalesce(
                                       binding.source_member,
                                       snapshot.original_name,
                                       snapshot.source_uri,
                                       ''
                                   )
                               ) LIKE '%.xlsx'
                               AND nullif(trim(binding.source_sheet), '') IS NULL
                           )
                    )
                FROM unresolved_balance unresolved
                JOIN evidence_binding binding
                  ON binding.evidence_id = unresolved.evidence_id
                JOIN source_snapshot snapshot
                  ON snapshot.snapshot_id = binding.snapshot_id
                WHERE unresolved.unresolved_id = ?
                """,
                [NORMALIZATION_RULE_VERSION, unresolved_id],
            ).fetchone()
            if evidence_integrity is None or int(evidence_integrity[0]) == 0:
                raise HTTPException(
                    status_code=404,
                    detail="这份原始依据不属于当前待确认记录",
                )
            if int(evidence_integrity[1]) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="这组原始依据包含旧版本或缺少精确位置，暂不能作为完整证据打开",
                )
            binding = database.execute(
                """
                SELECT binding.source_member, binding.source_sheet, binding.row_no,
                       binding.field, binding.source_value,
                       binding.normalization_version, binding.rule_version_id,
                       snapshot.content_sha256, snapshot.original_name,
                       snapshot.source_uri, artifact.dataset_kind,
                       u.reason_code, u.explanation, u.amount,
                       b.balance_key, b.expected_amount, b.actual_amount,
                       b.matched_amount, b.difference_amount, b.run_id,
                       coalesce(
                           nullif(
                               json_extract_string(
                                   contract.definition_json,
                                   '$.store_name'
                               ),
                               ''
                           ),
                           contract.store_id
                       ),
                       period.period_start, period.period_end,
                       strftime(run.finished_at, '%Y-%m-%d %H:%M:%S UTC')
                FROM unresolved_balance u
                JOIN reconciliation_balance b ON b.balance_id = u.balance_id
                JOIN reconciliation_contract contract
                  ON contract.contract_id = b.contract_id
                JOIN accounting_period period ON period.period_id = b.period_id
                JOIN run_log run ON run.run_id = b.run_id
                JOIN evidence_binding binding
                  ON binding.evidence_id = u.evidence_id
                 AND binding.snapshot_id = ?
                JOIN source_snapshot snapshot
                  ON snapshot.snapshot_id = binding.snapshot_id
                LEFT JOIN normalized_artifact artifact
                  ON artifact.artifact_id = binding.artifact_id
                WHERE u.unresolved_id = ?
                  AND binding.normalization_version = ?
                  AND b.run_id = (
                      SELECT run_id FROM run_log
                      WHERE run_kind = 'reconcile' AND status = 'succeeded'
                        AND contract_id = b.contract_id
                        AND period_id = b.period_id
                      ORDER BY finished_at DESC NULLS LAST, started_at DESC
                      LIMIT 1
                  )
                ORDER BY binding.ordinal
                LIMIT 1
                """,
                [snapshot_id, unresolved_id, NORMALIZATION_RULE_VERSION],
            ).fetchone()
        if binding is None:
            raise HTTPException(
                status_code=404,
                detail="这份原始依据不属于当前待确认记录",
            )

        target_column = str(binding[3] or "") or None
        viewer = EvidenceViewer(SnapshotStore(workbench.snapshots))
        member_name = str(binding[0] or "") or None
        sheet_name = str(binding[1] or "") or None
        target_row_number = int(binding[2])
        try:
            try:
                workbook = viewer.preview(
                    str(binding[7]),
                    member_name=member_name,
                    sheet_name=sheet_name,
                    target_row_number=target_row_number,
                    window_radius=radius,
                    max_columns=max_columns,
                    target_column=target_column,
                )
            except EvidencePreviewNotFoundError:
                # Older rows stored the normalized field rather than the source
                # header. Exact row positioning remains available in that case.
                workbook = viewer.preview(
                    str(binding[7]),
                    member_name=member_name,
                    sheet_name=sheet_name,
                    target_row_number=target_row_number,
                    window_radius=radius,
                    max_columns=max_columns,
                    target_column=None,
                )
        except EvidencePreviewSecurityError as exc:
            raise HTTPException(
                status_code=422,
                detail="原文件未通过安全检查，不能在页面中打开",
            ) from exc
        except EvidencePreviewNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="原文件中的对应位置已经无法读取",
            ) from exc
        except EvidencePreviewError as exc:
            raise HTTPException(
                status_code=422,
                detail="这份原始依据暂时无法生成安全预览",
            ) from exc

        title, summary, action = _review_business_copy(str(binding[11]), binding[12])
        result = workbook.to_dict()
        result.update(
            {
                "unresolvedId": unresolved_id,
                "snapshotId": snapshot_id,
                "lineageStatus": "frozen",
                "originalName": _source_basename(binding[8] or binding[9]),
                "context": {
                    "storeName": str(binding[20]),
                    "period": f"{binding[21]:%Y-%m-%d} 至 {binding[22]:%Y-%m-%d}",
                    "businessTitle": title,
                    "whatHappened": summary,
                    "whatItAffects": (
                        f"当前差额为 ¥{_decimal_text(binding[13])}；"
                        "在确认前不会把这条记录当作已经核对正确。"
                    ),
                    "suggestedAction": action,
                },
                "comparison": {
                    "businessKey": str(binding[14]),
                    "expectedAmount": _decimal_text(binding[15]),
                    "actualAmount": _decimal_text(binding[16]),
                    "matchedAmount": _decimal_text(binding[17]),
                    "differenceAmount": _decimal_text(binding[18]),
                },
                "trace": [
                    {
                        "label": "保存原文件",
                        "detail": "系统按文件内容生成不可覆盖的指纹。",
                    },
                    {
                        "label": "整理这条记录",
                        "detail": (f"采用整理规则 {str(binding[5] or '内置稳定规则')}。"),
                    },
                    {
                        "label": "按经营规则核对",
                        "detail": (
                            f"核对规则 {str(binding[6] or '当前内置规则')}，运行于 {binding[23]}。"
                        ),
                    },
                    {
                        "label": "等待业务确认",
                        "detail": "系统保留原值和差额，不会替你改写金额。",
                    },
                ],
                "sourceField": target_column,
                "sourceValue": str(binding[4] or "") or None,
            }
        )
        return result

    @app.get("/api/v1/reviews/{unresolved_id}/evidence/{snapshot_id}/original")
    def download_review_evidence(
        unresolved_id: str,
        snapshot_id: str,
    ) -> StreamingResponse:
        with memory() as database:
            snapshot = database.execute(
                """
                SELECT snapshot.content_sha256, snapshot.original_name,
                       snapshot.source_uri, snapshot.media_type
                FROM unresolved_balance u
                JOIN evidence_binding binding ON binding.evidence_id = u.evidence_id
                JOIN source_snapshot snapshot
                  ON snapshot.snapshot_id = binding.snapshot_id
                WHERE u.unresolved_id = ?
                  AND binding.snapshot_id = ?
                LIMIT 1
                """,
                [unresolved_id, snapshot_id],
            ).fetchone()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="原文件依据不存在")
        try:
            source = SnapshotStore(workbench.snapshots).open_object(str(snapshot[0]))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="原文件内容不可用") from exc
        file_name = _source_basename(snapshot[1] or snapshot[2]) or "source-data"

        def stream_source() -> Iterator[bytes]:
            with source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        safe_name = file_name.replace('"', "")
        ascii_name = safe_name.encode("ascii", "ignore").decode() or "source-data"
        return StreamingResponse(
            stream_source(),
            media_type=str(snapshot[3] or "application/octet-stream"),
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{safe_name}"
                )
            },
        )

    @app.post("/api/v1/reviews/{unresolved_id}/suggestion")
    def suggest_review_explanation(unresolved_id: str) -> dict[str, Any]:
        with memory() as database:
            item = database.execute(
                """
                SELECT u.reason_code, u.explanation, u.amount, u.evidence_id
                FROM unresolved_balance u
                JOIN reconciliation_balance b
                  ON b.balance_id = u.balance_id
                WHERE u.unresolved_id = ?
                  AND u.status = 'open'
                  AND b.run_id = (
                      SELECT run_id
                      FROM run_log
                      WHERE run_kind = 'reconcile'
                        AND status = 'succeeded'
                        AND contract_id = b.contract_id
                        AND period_id = b.period_id
                      ORDER BY finished_at DESC NULLS LAST,
                               started_at DESC,
                               run_id DESC
                      LIMIT 1
                  )
                """,
                [unresolved_id],
            ).fetchone()
            evidence_rows: list[dict[str, object]] = []
            binding_records: list[dict[str, object]] = []
            evidence_integrity_error = False
            evidence_id = str(item[3] or "") if item else ""
            if evidence_id:
                binding_rows = _rows(
                    database.execute(
                    """
                    SELECT binding.ordinal, binding.snapshot_id,
                           binding.artifact_id, snapshot.original_name,
                           snapshot.source_uri, binding.row_no, binding.field,
                           binding.source_value, binding.source_member,
                           binding.source_sheet, binding.normalization_version,
                           binding.rule_version_id
                    FROM evidence_binding binding
                    JOIN source_snapshot snapshot
                      ON snapshot.snapshot_id = binding.snapshot_id
                    WHERE binding.evidence_id = ?
                    ORDER BY binding.ordinal
                    """,
                    [evidence_id],
                    )
                )
                for binding in binding_rows:
                    try:
                        row_number = int(binding["row_no"])
                    except (TypeError, ValueError):
                        evidence_integrity_error = True
                        continue
                    source_name = str(
                        binding["source_member"]
                        or binding["original_name"]
                        or binding["source_uri"]
                    )
                    if (
                        row_number <= 0
                        or str(binding["normalization_version"])
                        != NORMALIZATION_RULE_VERSION
                        or (
                            source_name.lower().endswith(".xlsx")
                            and not binding["source_sheet"]
                        )
                    ):
                        evidence_integrity_error = True
                        continue
                    binding_records.append(
                        {
                            "ordinal": binding["ordinal"],
                            "snapshot_id": binding["snapshot_id"],
                            "artifact_id": binding["artifact_id"],
                            "source_member": binding["source_member"],
                            "source_sheet": binding["source_sheet"],
                            "row_no": row_number,
                            "field": binding["field"],
                            "source_value": binding["source_value"],
                            "normalization_version": binding[
                                "normalization_version"
                            ],
                            "rule_version_id": binding["rule_version_id"],
                        }
                    )
                    if len(evidence_rows) < 12:
                        evidence_rows.append(
                            {
                                "snapshotId": str(binding["snapshot_id"]),
                                "file": _source_basename(
                                    binding["original_name"]
                                    or binding["source_uri"]
                                ),
                                "rowNumber": row_number,
                                "field": str(binding["field"] or ""),
                                "normalizedValue": str(
                                    binding["source_value"] or ""
                                ),
                            }
                        )
        if item is None:
            raise HTTPException(status_code=404, detail="待处理记录不存在或已经完成")
        if (
            not evidence_id
            or not binding_records
            or evidence_integrity_error
            or len(binding_records) != len(binding_rows)
        ):
            raise HTTPException(
                status_code=409,
                detail="原始依据不完整或版本过期，已停止调用模型；请先重新处理该范围。",
            )
        binding_sha256 = evidence_binding_digest(binding_records)
        title, summary, action = _review_business_copy(str(item[0]), item[1])
        result = llm_gateway.complete_json(
            purpose="review_explanation_suggestion",
            model="runtime-configured-model",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是电商财务核对助手，只能生成待人工确认的业务说明草案。"
                        "不得修改金额，不得断言未经证据确认的原因，不得输出技术字段名。"
                        "用简体中文，最多两句话，说明建议优先核查什么。"
                        "每个判断必须引用输入中真实存在的 snapshotId 和 rowNumber；"
                        "引用不足时明确说证据不足。"
                        "只返回 JSON："
                        '{"suggestion":"...","category":"...","action":"...",'
                        '"rationale":"...","confidence":0.0,'
                        '"citations":[{"snapshotId":"...","rowNumber":1}]}。'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "问题": title,
                            "系统已确认的现象": summary,
                            "确定性检查建议": action,
                            "差额": _decimal_text(item[2]),
                            "原始依据": evidence_rows,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
        )
        if result.status == "disabled":
            llm_store.record_activity(
                status="disabled",
                purpose="review_explanation_suggestion",
                model=result.model,
                message=result.reason or "模型尚未启用，确定性核对不受影响",
                request_id=result.request_id,
            )
            raise HTTPException(
                status_code=409,
                detail=result.reason or "模型尚未启用，确定性核对不受影响",
            )
        if result.status != "ok" or not isinstance(result.content, Mapping):
            failure_message = _llm_failure_message(result.reason)
            llm_store.record_activity(
                status="error",
                purpose="review_explanation_suggestion",
                model=result.model,
                message=failure_message,
                request_id=result.request_id,
            )
            raise HTTPException(
                status_code=502,
                detail=failure_message,
            )
        suggestion = _llm_business_suggestion(result.content.get("suggestion"))
        if suggestion is None:
            llm_store.record_activity(
                status="error",
                purpose="review_explanation_suggestion",
                model=result.model,
                message="模型返回的业务说明格式不正确",
                request_id=result.request_id,
            )
            raise HTTPException(status_code=502, detail="模型返回的业务说明格式不正确")
        category = _llm_business_suggestion(result.content.get("category")) or str(item[0])
        candidate_action = _llm_business_suggestion(result.content.get("action")) or suggestion
        rationale = _llm_business_suggestion(result.content.get("rationale")) or summary
        try:
            confidence = Decimal(str(result.content.get("confidence", "0.5")))
        except InvalidOperation:
            confidence = Decimal("0.5")
        confidence = min(Decimal("1"), max(Decimal("0"), confidence)).quantize(Decimal("0.000001"))
        valid_citations = {
            (str(row["snapshotId"]), int(str(row["rowNumber"])))
            for row in evidence_rows
        }
        raw_citations = result.content.get("citations")
        citation_keys: set[tuple[str, int]] = set()
        if isinstance(raw_citations, list):
            for citation in raw_citations:
                if not isinstance(citation, Mapping):
                    continue
                try:
                    citation_keys.add(
                        (
                            str(citation.get("snapshotId") or ""),
                            int(citation.get("rowNumber") or 0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        guard_status = (
            "passed"
            if citation_keys and citation_keys <= valid_citations
            else "failed"
            if citation_keys
            else "missing_citations"
        )
        candidate = {
            "suggestion": suggestion,
            "category": category,
            "action": candidate_action,
            "rationale": rationale,
            "confidence": format(confidence, "f"),
            "citations": [
                {"snapshotId": snapshot_id, "rowNumber": row_number}
                for snapshot_id, row_number in sorted(citation_keys)
            ],
            "mayWriteLedger": False,
        }
        reviewer_status = "not_configured"
        reviewer_model: str | None = None
        reviewer_reason: str | None = None
        if guard_status != "passed":
            reviewer_status = "not_run_evidence_failed"
            reviewer_reason = "主模型没有通过原始引用核验，未调用独立复核。"
        runtime_config = None
        if guard_status == "passed":
            try:
                runtime_config = llm_store.load()
            except LlmRuntimeError:
                reviewer_status = "configuration_unavailable"
        if (
            guard_status == "passed"
            and runtime_config
            and runtime_config.reviewer_model
        ):
            reviewer_model = runtime_config.reviewer_model
            if reviewer_model == result.model:
                reviewer_status = "not_independent"
                reviewer_reason = "复核模型与主模型相同，不能算独立复核。"
            else:
                review_result = llm_gateway.complete_json(
                    purpose="review_explanation_review",
                    model="runtime-reviewer-model",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是独立复核员，只核查业务说明是否被给定证据支持。"
                                "不得补造原因、不得修改金额、不得建议写账。"
                                "不同意或证据不足时 agree 必须为 false。"
                                "每个判断必须引用输入中真实存在的 snapshotId 和 rowNumber。"
                                "只返回 JSON："
                                '{"agree":false,"reason":"...",'
                                '"citations":[{"snapshotId":"...","rowNumber":1}]}。'
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "主模型草案": candidate,
                                    "允许引用的原始依据": evidence_rows,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        },
                    ],
                )
                reviewer_model = review_result.model or reviewer_model
                if (
                    review_result.status == "ok"
                    and isinstance(review_result.content, Mapping)
                ):
                    raw_review_citations = review_result.content.get("citations")
                    review_citations: set[tuple[str, int]] = set()
                    if isinstance(raw_review_citations, list):
                        for citation in raw_review_citations:
                            if not isinstance(citation, Mapping):
                                continue
                            try:
                                review_citations.add(
                                    (
                                        str(citation.get("snapshotId") or ""),
                                        int(citation.get("rowNumber") or 0),
                                    )
                                )
                            except (TypeError, ValueError):
                                continue
                    agrees = review_result.content.get("agree") is True
                    citations_valid = (
                        bool(review_citations)
                        and review_citations <= valid_citations
                        and guard_status == "passed"
                    )
                    reviewer_status = (
                        "passed" if agrees and citations_valid else "failed"
                    )
                    reviewer_reason = _llm_business_suggestion(
                        review_result.content.get("reason")
                    )
                else:
                    reviewer_status = "failed"
                    reviewer_reason = _llm_failure_message(review_result.reason)
        candidate_json = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_sha = hashlib.sha256(candidate_json.encode("utf-8")).hexdigest()
        suggestion_id = f"suggestion_{uuid.uuid4().hex}"
        with memory() as database:
            database.execute(
                """
                INSERT INTO residual_suggestion (
                    suggestion_id, unresolved_id, suggestion_kind, category,
                    action, rationale, confidence, source_model, candidate_json,
                    candidate_sha256, guard_status, critic_status,
                    evidence_policy_version, evidence_binding_sha256, status
                )
                VALUES (
                    ?, ?, 'difference_explanation', ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, 'suggestion'
                )
                """,
                [
                    suggestion_id,
                    unresolved_id,
                    category,
                    candidate_action,
                    rationale,
                    confidence,
                    result.model,
                    candidate_json,
                    candidate_sha,
                    guard_status,
                    reviewer_status,
                    NORMALIZATION_RULE_VERSION,
                    binding_sha256,
                ],
            )
        llm_store.record_activity(
            status="ok",
            purpose="review_explanation_suggestion",
            model=result.model,
            message="最近一次业务说明草案已生成，等待人工确认。",
            request_id=result.request_id,
        )
        return {
            "status": "suggestion",
            "suggestion": suggestion,
            "model": result.model,
            "requestId": result.request_id,
            "suggestionId": suggestion_id,
            "evidenceGuard": guard_status,
            "reviewerModel": reviewer_model,
            "reviewerStatus": reviewer_status,
            "reviewerReason": reviewer_reason,
            "mayWriteLedger": False,
            "requiresHumanReview": True,
        }

    @app.get("/api/v1/reviews.csv")
    def reviews_csv(
        store_id: Annotated[str | None, Query(alias="storeId")] = None,
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
    ) -> StreamingResponse:
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=[
                "item_type",
                "item_id",
                "subject",
                "store",
                "period",
                "amount",
                "decision",
                "business_reason",
            ],
        )
        writer.writeheader()
        with memory() as database:
            pending_decisions = _rows(
                database.execute(
                    """
                    SELECT decision.decision_id, decision.question,
                           contract.store_id
                    FROM business_decision decision
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = decision.contract_id
                    WHERE decision.status = 'pending'
                      AND (? IS NULL OR contract.store_id = ?)
                    ORDER BY subject_kind
                    """,
                    [store_id, store_id],
                )
            )
        for item in pending_decisions:
            writer.writerow(
                {
                    "item_type": "business_decision",
                    "item_id": item["decision_id"],
                    "subject": item["question"],
                    "store": item["store_id"],
                    "period": period or "",
                    "amount": "",
                    "decision": "",
                    "business_reason": "",
                }
            )
        for item in review_records(store_id=store_id, period=period, limit=None):
            writer.writerow(
                {
                    "item_type": "unresolved_balance",
                    "item_id": item["unresolvedId"],
                    "subject": item["businessTitle"],
                    "store": item["storeName"],
                    "period": item["period"],
                    "amount": item["amount"],
                    "decision": "",
                    "business_reason": item["suggestedAction"],
                }
            )
        payload = buffer.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            iter([payload]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="reconciliation-reviews.csv"'},
        )

    @app.post("/api/v1/reviews/{unresolved_id}", status_code=204)
    def decide_review(unresolved_id: str, request: ReviewDecisionRequest) -> None:
        with memory() as database, database.transaction() as connection:
            current = connection.execute(
                """
                SELECT u.status,
                       EXISTS (
                           SELECT 1 FROM review_decision d
                           WHERE d.unresolved_id = u.unresolved_id
                             AND d.decision IN ('explain', 'reject')
                       ) AS finalized,
                       u.reason_code,
                       u.amount
                FROM unresolved_balance u
                WHERE u.unresolved_id = ?
                """,
                [unresolved_id],
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="待确认差额不存在")
            if current[0] != "open" or current[1]:
                raise HTTPException(status_code=409, detail="该差额已经处理")
            suggestion = connection.execute(
                """
                SELECT suggestion_id, candidate_sha256, candidate_json
                FROM residual_suggestion
                WHERE unresolved_id = ? AND status = 'suggestion'
                ORDER BY created_at DESC, suggestion_id DESC
                LIMIT 1
                """,
                [unresolved_id],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO review_decision (
                    decision_id, unresolved_id, suggestion_id, decision,
                    final_action, reason, decided_by, candidate_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    f"decision_{uuid.uuid4().hex}",
                    unresolved_id,
                    str(suggestion[0]) if suggestion else None,
                    request.decision,
                    request.reason,
                    request.reason,
                    "local_operator",
                    str(suggestion[1]) if suggestion else None,
                ],
            )
            if suggestion and request.decision == "explain":
                model_outcome = json.loads(str(suggestion[2]))
                model_text = " ".join(
                    str(model_outcome.get(key) or "")
                    for key in ("suggestion", "action", "rationale")
                )
                if request.reason.strip() not in model_text:
                    connection.execute(
                        """
                        INSERT INTO correction (
                            correction_id, suggestion_id, unresolved_id,
                            feature_json, model_outcome_json, human_outcome_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"correction_{uuid.uuid4().hex}",
                            str(suggestion[0]),
                            unresolved_id,
                            json.dumps(
                                {
                                    "reasonCode": str(current[2]),
                                    "amount": _decimal_text(current[3]),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            str(suggestion[2]),
                            json.dumps(
                                {
                                    "decision": request.decision,
                                    "reason": request.reason,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ],
                    )
        try:
            with memory() as database:
                LearningEvaluator(database).evaluate_and_persist()
        except Exception:
            logger.exception(
                "review decision was saved but the learning evaluation failed",
                extra={"unresolved_id": unresolved_id},
            )

    @app.get("/api/v1/analytics/overview")
    def analytics_overview_endpoint(
        platform_id: Annotated[
            str,
            Query(alias="platformId", max_length=100),
        ] = "all",
        store_id: Annotated[str, Query(alias="storeId", max_length=200)] = "all",
        period: Annotated[str, Query(max_length=20)] = "all",
        from_date: Annotated[date | None, Query(alias="fromDate")] = None,
        to_date: Annotated[date | None, Query(alias="toDate")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, object]:
        try:
            with memory() as database:
                return analytics_overview(
                    database,
                    enterprise_id=enterprise_id,
                    platform_id=platform_id,
                    store_id=store_id,
                    period=period,
                    from_date=from_date,
                    to_date=to_date,
                    limit=limit,
                )
        except AnalyticsValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/trust/matrix")
    def trust_matrix_endpoint() -> dict[str, object]:
        with memory() as database:
            return trust_matrix(database, enterprise_id=enterprise_id)

    @app.get("/api/v1/analytics/catalog")
    def analytics_catalog_endpoint() -> dict[str, object]:
        with memory() as database:
            contract_rows = database.execute(
                """
                WITH classified AS (
                    SELECT
                        store_id,
                        enterprise_id,
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
                        END AS platform_id,
                        definition_json,
                        created_at,
                        contract_version,
                        effective_from,
                        contract_id
                    FROM reconciliation_contract
                    WHERE status = 'active'
                      AND enterprise_id = ?
                ),
                prepared AS (
                    SELECT
                        store_id,
                        platform_id,
                        definition_json,
                        row_number() OVER (
                            PARTITION BY
                                enterprise_id,
                                platform_id,
                                lower(
                                    trim(
                                        coalesce(
                                            nullif(
                                                json_extract_string(
                                                    definition_json,
                                                    '$.store_name'
                                                ),
                                                ''
                                            ),
                                            store_id
                                        )
                                    )
                                )
                            ORDER BY
                                created_at DESC,
                                contract_version DESC,
                                effective_from DESC,
                                contract_id DESC
                        ) AS position
                    FROM classified
                )
                SELECT store_id, platform_id, definition_json
                FROM prepared
                WHERE position = 1
                """,
                [enterprise_id],
            ).fetchall()
            known_stores: dict[str, str] = {}
            known_store_platforms: dict[str, str] = {}
            known_store_ids: dict[tuple[str, str], str] = {}
            for store_id, platform_id, definition_json in contract_rows:
                try:
                    definition = json.loads(str(definition_json or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    definition = {}
                if isinstance(definition, dict):
                    store_name = definition.get("store_name")
                    if isinstance(store_name, str) and store_name.strip():
                        known_stores[store_name.strip()] = str(store_id)
                        known_store_platforms[store_name.strip()] = str(platform_id)
                        known_store_ids[(str(platform_id), store_name.strip().casefold())] = str(
                            store_id
                        )
            processed_sources = [
                row[0]
                for row in database.execute(
                    "SELECT DISTINCT source_uri FROM source_snapshot"
                ).fetchall()
            ]
        catalog = analytics_catalog(
            workbench.reports / "source-inventory.json",
            processed_source_uris=processed_sources,
            known_stores=known_stores,
            known_store_platforms=known_store_platforms,
        )
        try:
            target_payload = json.loads(
                (workbench.reports / "target-plan.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return catalog
        raw_targets = target_payload.get("targets", []) if isinstance(target_payload, dict) else []
        if not isinstance(raw_targets, list):
            return catalog
        raw_platforms = catalog.get("platforms", [])
        if not isinstance(raw_platforms, list):
            raw_platforms = []
        platform_names = {
            str(item.get("id")): str(item.get("name"))
            for item in raw_platforms
            if isinstance(item, dict)
        }
        authoritative: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_target in raw_targets:
            if not isinstance(raw_target, dict):
                continue
            platform_id = str(raw_target.get("platform") or "").strip()
            store_name = str(raw_target.get("logical_store") or "").strip()
            period = str(raw_target.get("period") or "").strip()
            if not platform_id or not store_name:
                continue
            key = (platform_id, store_name.casefold())
            fallback_store_id = (
                "configured_"
                + hashlib.sha256(f"{platform_id}:{store_name}".encode()).hexdigest()[:16]
            )
            current = authoritative.setdefault(
                key,
                {
                    "id": known_store_ids.get(key) or fallback_store_id,
                    "name": store_name,
                    "platformId": platform_id,
                    "platformName": platform_names.get(platform_id, platform_id),
                    "periods": set(),
                    "sourceIds": set(),
                },
            )
            periods = current["periods"]
            source_ids = current["sourceIds"]
            assert isinstance(periods, set)
            assert isinstance(source_ids, set)
            if period:
                periods.add(period)
            raw_source_ids = raw_target.get("source_ids", [])
            if isinstance(raw_source_ids, list):
                source_ids.update(str(value) for value in raw_source_ids)
        if not authoritative:
            return catalog
        stores: list[dict[str, object]] = []
        for current in authoritative.values():
            periods = current.pop("periods")
            source_ids = current.pop("sourceIds")
            assert isinstance(periods, set)
            assert isinstance(source_ids, set)
            stores.append(
                {
                    **current,
                    "periods": sorted(periods),
                    "fileCount": len(source_ids),
                    "processed": bool(source_ids),
                }
            )
        stores.sort(
            key=lambda item: (
                str(item["platformId"]),
                str(item["name"]).casefold(),
            )
        )
        platform_ids = sorted({str(item["platformId"]) for item in stores})
        return {
            **catalog,
            "discoveredStoreCount": len(stores),
            "processedStoreCount": sum(1 for item in stores if item["processed"]),
            "platforms": [
                {
                    "id": platform_id,
                    "name": platform_names.get(platform_id, platform_id),
                }
                for platform_id in platform_ids
            ],
            "stores": stores,
        }

    @app.get("/api/v1/capabilities")
    def capabilities_endpoint() -> dict[str, object]:
        runtime_llm = llm_store.public_status()
        with memory() as database:
            suggestion_count = int(
                _required_row(
                    database.execute("SELECT count(*) FROM residual_suggestion")
                )[0]
            )
            reviewed_count = int(
                _required_row(
                    database.execute(
                        """
                        SELECT count(*)
                        FROM review_decision
                        WHERE suggestion_id IS NOT NULL
                          AND decision IN ('explain', 'reject', 'replace', 'approve')
                        """
                    )
                )[0]
            )
            correction_count = int(
                _required_row(database.execute("SELECT count(*) FROM correction"))[0]
            )
            guarded_count = int(
                _required_row(
                    database.execute(
                        """
                        SELECT count(*) FROM residual_suggestion
                        WHERE guard_status = 'passed'
                          AND evidence_policy_version = ?
                        """,
                        [NORMALIZATION_RULE_VERSION],
                    )
                )[0]
            )
            latest_evaluation = database.execute(
                """
                SELECT eligible, reason_json, metrics_json, proposed_level,
                       category, model_version,
                       strftime(
                           evaluated_at AT TIME ZONE 'UTC',
                           '%Y-%m-%d %H:%M:%S'
                       ) || '+00' AS evaluated_at
                FROM autonomy_evaluation
                WHERE enterprise_id = ?
                  AND policy_version = ?
                ORDER BY evaluated_at DESC, evaluation_id DESC
                LIMIT 1
                """,
                [enterprise_id, LEARNING_POLICY_VERSION],
            ).fetchone()
        promotion_eligible = bool(latest_evaluation and latest_evaluation[0])
        promotion_reason = "尚无经过人工复核并带原始证据的跨账期学习样本。"
        learning_evaluation: dict[str, object] | None = None
        if latest_evaluation is not None:
            reasons = json.loads(str(latest_evaluation[1] or "[]"))
            metrics = json.loads(str(latest_evaluation[2] or "{}"))
            reason_labels = {
                "minimum_samples_not_met": "至少需要 20 条已复核样本",
                "minimum_periods_not_met": "至少需要覆盖 2 个账期",
                "evidence_citations_not_all_verified": "仍有建议未通过原始证据核验",
                "accuracy_not_assessed_for_all_samples": "仍有样本没有独立事实判定",
                "accuracy_below_threshold": "独立核验准确率尚未达到 99.5%",
                "major_amount_error_observed": "曾出现重大金额错误",
                "eligible_for_governance_review_only": "已满足进入规则治理评审的门槛",
            }
            readable_reasons = [
                reason_labels.get(str(reason), str(reason)) for reason in reasons
            ]
            promotion_reason = "；".join(readable_reasons)
            learning_evaluation = {
                "proposedLevel": str(latest_evaluation[3]),
                "category": str(latest_evaluation[4]),
                "modelVersion": str(latest_evaluation[5]),
                "evaluatedAt": str(latest_evaluation[6]),
                "metrics": metrics,
                "reasons": readable_reasons,
            }
        model_enabled = bool(runtime_llm.enabled)
        policy_labels = {
            "structure_identification": "判断文件属于哪类业务资料",
            "field_mapping": "建议字段与业务含义的对应关系",
            "linkage_candidate": "寻找可能属于同一笔业务的记录",
            "difference_explanation": "解释已确定差额的可能原因",
            "rule_draft": "把人工结论整理成候选规则",
        }
        # effectiveLevel is sourced from LearningEvaluator: it remains L0
        # unless a completed evaluation has proposed_level > L0. The
        # AutonomyEvaluator L2 is advisory-only and does NOT affect this field.
        effective_level = (
            str(latest_evaluation[3])
            if latest_evaluation is not None and latest_evaluation[0]
            else "L0"
        )
        return {
            "effectiveLevel": effective_level,
            "levelReason": (
                "能力范围可以扩展，但模型仍只生成建议；金额、规则发布和绩效结果由确定性代码控制。"
            ),
            "modelEnabled": model_enabled,
            "orchestration": {
                "proposerModel": runtime_llm.selected_model or None,
                "reviewerModel": runtime_llm.reviewer_model or None,
                "independentReviewerConfigured": bool(
                    runtime_llm.reviewer_model
                    and runtime_llm.reviewer_model != runtime_llm.selected_model
                ),
                "policies": [
                    {
                        "id": policy.task_category.value,
                        "name": policy_labels[policy.task_category.value],
                        "cloudAllowed": policy.cloud_models_allowed,
                        "redactionRequired": policy.redaction_required,
                        "maxEvidenceRows": policy.max_row_window,
                        "risk": policy.risk_level.value,
                        "release": (
                            "满足证据与回归门槛后可撤销应用"
                            if policy.reversible_apply_allowed
                            else "只能建议，必须进入治理复核"
                        ),
                        "mayWriteAmounts": False,
                        "mayWriteLedger": False,
                    }
                    for policy in task_policies()
                ],
            },
            "tasks": [
                {
                    "id": "read_only_inventory",
                    "name": "只读发现新增文件",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "finite_template_routing",
                    "name": "有限模板识别文件用途",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "period_store_detection",
                    "name": "识别月份与店铺范围",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "deterministic_normalization",
                    "name": "金额标准化与跨文件去重",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "wallet_reconciliation",
                    "name": "订单与平台钱包核对",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "evidence_locator",
                    "name": "定位原始文件与行",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "difference_diagnosis",
                    "name": "差额原因草案",
                    "state": "active" if model_enabled else "model_disabled",
                    "usesModel": True,
                    "mayWriteLedger": False,
                },
                {
                    "id": "evidence_citation_guard",
                    "name": "模型引用核验",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "review_learning",
                    "name": "人工修正学习记录",
                    "state": "active",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "historical_comparison",
                    "name": "与历史计算结果逐项对比",
                    "state": "reference_validation",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "performance_attribution",
                    "name": "人员、店铺与商品归属",
                    "state": "reference_validation",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
                {
                    "id": "rule_promotion",
                    "name": "规则自动晋升",
                    "state": "evaluation_only",
                    "usesModel": False,
                    "mayWriteLedger": False,
                },
            ],
            "learning": {
                "suggestionCount": suggestion_count,
                "reviewedCount": reviewed_count,
                "correctionCount": correction_count,
                "evidenceGuardedCount": guarded_count,
                "promotionEligible": promotion_eligible,
                "promotionReason": promotion_reason,
                "latestEvaluation": learning_evaluation,
            },
        }

    @app.post("/api/v1/performance/sync")
    def performance_sync_endpoint() -> dict[str, object]:
        result = sync_performance_sources(workbench, enterprise_id=enterprise_id)
        return {
            "importedSnapshots": result.imported_snapshots,
            "skippedSnapshots": result.skipped_snapshots,
            "employeeRows": result.employee_rows,
            "assignmentRows": result.assignment_rows,
            "referenceRows": result.reference_rows,
            "issueCount": result.issue_count,
        }

    @app.get("/api/v1/performance/overview")
    def performance_overview_endpoint(
        calculation_mode: Annotated[
            Literal["single", "combined"],
            Query(alias="calculationMode"),
        ] = "single",
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
        store: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, object]:
        with memory() as database:
            latest_period = _required_row(
                database.execute(
                    """
                    SELECT max(period_token)
                    FROM performance_reference_fact
                    WHERE enterprise_id = ? AND calculation_mode = ?
                      AND (? IS NULL OR store_name = ?)
                    """,
                    [enterprise_id, calculation_mode, store, store],
                )
            )[0]
            selected_period = period or (str(latest_period) if latest_period is not None else None)
            period_clause = "AND period_token = ?" if selected_period else ""
            store_clause = "AND store_name = ?" if store else ""
            parameters: list[object] = [enterprise_id, calculation_mode]
            if selected_period:
                parameters.append(selected_period)
            if store:
                parameters.append(store)
            summary = _required_row(
                database.execute(
                    f"""
                    SELECT count(*),
                           count(DISTINCT store_name),
                           count(DISTINCT person_id),
                           count(DISTINCT product_id),
                           count(*) FILTER (WHERE validation_status = 'passed'),
                           coalesce(sum(collected_amount), 0),
                           coalesce(sum(refund_amount), 0),
                           coalesce(sum(product_cost), 0),
                           coalesce(sum(advertising_fee), 0),
                           coalesce(sum(store_profit), 0)
                    FROM performance_reference_fact
                    WHERE enterprise_id = ? AND calculation_mode = ?
                      {period_clause}
                      {store_clause}
                    """,
                    parameters,
                )
            )
            assignment = _required_row(
                database.execute(
                    """
                    SELECT count(*) FILTER (WHERE status = 'active'),
                           count(*) FILTER (WHERE status = 'conflict'),
                           max(effective_to)
                    FROM responsibility_assignment_version
                    WHERE enterprise_id = ?
                    """,
                    [enterprise_id],
                )
            )
            provisional = int(
                _required_row(
                    database.execute(
                        """
                        SELECT count(*)
                        FROM person_identity
                        WHERE enterprise_id = ? AND status = 'provisional'
                        """,
                        [enterprise_id],
                    )
                )[0]
            )
            imports = _rows(
                database.execute(
                    """
                    SELECT source_kind, status, count(*) AS snapshot_count,
                           coalesce(sum(row_count), 0) AS row_count,
                           coalesce(sum(issue_count), 0) AS issue_count
                    FROM performance_source_import
                    WHERE enterprise_id = ?
                    GROUP BY source_kind, status
                    ORDER BY source_kind, status
                    """,
                    [enterprise_id],
                )
            )
            expected_scope_rows = database.execute(
                """
                WITH classified AS (
                    SELECT
                        period.period_id,
                        period.period_start,
                        coalesce(
                            nullif(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                ''
                            ),
                            period.store_id
                        ) AS store_name,
                        CASE
                            WHEN lower(trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                            ))) LIKE 'pdd%'
                              OR trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                              )) LIKE '拼多多%'
                                THEN 'pinduoduo'
                            WHEN trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                            )) LIKE '抖店%'
                              OR trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                              )) LIKE '抖音%'
                                THEN 'douyin'
                            WHEN trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                            )) LIKE '京东%'
                                THEN 'jd'
                            WHEN lower(trim(coalesce(
                                json_extract_string(
                                    contract.definition_json,
                                    '$.store_name'
                                ),
                                period.store_id
                            ))) LIKE '%1688'
                                THEN '1688'
                            WHEN lower(contract.platform_code)
                                 IN ('pdd', 'pinduoduo')
                                THEN 'pinduoduo'
                            ELSE lower(contract.platform_code)
                        END AS platform_code,
                        contract.enterprise_id,
                        contract.created_at,
                        contract.contract_version,
                        period.revision_no,
                        contract.contract_id
                    FROM accounting_period period
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = period.contract_id
                    WHERE contract.enterprise_id = ?
                      AND contract.status = 'active'
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
                SELECT period_id, store_name
                FROM ranked
                WHERE position = 1
                  AND (
                      ? IS NULL
                      OR strftime(period_start, '%y%m') = ?
                  )
                  AND (? IS NULL OR store_name = ?)
                """,
                [enterprise_id, selected_period, selected_period, store, store],
            ).fetchall()
            expected_scope_ids = {str(row[0]) for row in expected_scope_rows}
            expected_scope_names = {
                str(row[0]): str(row[1]) for row in expected_scope_rows
            }
            certified_scope_ids = {
                str(row[0])
                for row in database.execute(
                    """
                    SELECT DISTINCT result.period_id
                    FROM performance_result_head head
                    JOIN performance_result result
                      ON result.result_id = head.result_id
                    JOIN accounting_period period
                      ON period.period_id = result.period_id
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = period.contract_id
                    WHERE result.enterprise_id = ?
                      AND contract.enterprise_id = ?
                      AND result.evidence_policy_version = ?
                      AND result.engine_version = ?
                      AND result.status = 'complete'
                      AND (
                          ? IS NULL
                          OR strftime(period.period_start, '%y%m') = ?
                      )
                      AND (
                          ? IS NULL
                          OR coalesce(
                              nullif(
                                  json_extract_string(
                                      contract.definition_json,
                                      '$.store_name'
                                  ),
                                  ''
                              ),
                              result.store_id
                          ) = ?
                      )
                    """,
                    [
                        enterprise_id,
                        enterprise_id,
                        NORMALIZATION_RULE_VERSION,
                        PERFORMANCE_ENGINE_VERSION,
                        selected_period,
                        selected_period,
                        store,
                        store,
                    ],
                ).fetchall()
            }
            latest_performance_gates = database.execute(
                """
                SELECT period_id, status, metrics_json
                FROM (
                    SELECT job.period_id, job.status, job.metrics_json,
                           row_number() OVER (
                               PARTITION BY job.period_id
                               ORDER BY job.finished_at DESC NULLS LAST,
                                        job.created_at DESC,
                                        job.job_id DESC
                           ) AS rank
                    FROM compute_job job
                    JOIN reconciliation_contract contract
                      ON contract.contract_id = job.contract_id
                    WHERE job.job_kind = 'reconcile'
                      AND job.period_id IS NOT NULL
                      AND contract.enterprise_id = ?
                      AND (? IS NULL OR job.period_token = ?)
                      AND (
                          ? IS NULL
                          OR coalesce(
                              nullif(
                                  json_extract_string(
                                      contract.definition_json,
                                      '$.store_name'
                                  ),
                                  ''
                              ),
                              job.store_id
                          ) = ?
                      )
                )
                WHERE rank = 1
                """,
                [enterprise_id, selected_period, selected_period, store, store],
            ).fetchall()
        gate_payload: dict[str, object] = {
            "status": "waiting",
            "message": "当前范围尚未完成商品级认证绩效计算。",
            "code": None,
            "details": {},
        }
        certified_in_scope = expected_scope_ids & certified_scope_ids
        gate_by_scope = {
            str(row[0]): (str(row[1]), row[2])
            for row in latest_performance_gates
            if row[0] is not None
        }
        blocked_scopes: list[str] = []
        waiting_scopes: list[str] = []
        certified_scopes: list[str] = []
        blocked_codes: set[str] = set()
        for scope_id in sorted(expected_scope_ids):
            job = gate_by_scope.get(scope_id)
            if job is None:
                waiting_scopes.append(scope_id)
                continue
            job_status, raw_metrics = job
            raw_gate: Mapping[str, object] = {}
            if raw_metrics:
                parsed_metrics = json.loads(str(raw_metrics))
                candidate_gate = parsed_metrics.get("performance")
                if isinstance(candidate_gate, dict):
                    raw_gate = candidate_gate
            gate_status = str(raw_gate.get("status") or "waiting")
            if (
                job_status == "succeeded"
                and gate_status == "certified"
                and scope_id in certified_in_scope
            ):
                certified_scopes.append(scope_id)
            elif job_status == "failed" or gate_status == "blocked":
                blocked_scopes.append(scope_id)
                code = str(raw_gate.get("code") or "")
                if code:
                    blocked_codes.add(code)
            else:
                waiting_scopes.append(scope_id)
        if expected_scope_ids and len(certified_scopes) == len(expected_scope_ids):
            gate_payload = {
                "status": "certified",
                "message": "当前选择范围内的全部店铺账期均已形成认证绩效。",
                "code": None,
                "details": {
                    "scopeCount": len(expected_scope_ids),
                    "certifiedScopeCount": len(certified_scopes),
                },
            }
        elif blocked_scopes:
            affected = [
                expected_scope_names.get(scope_id, scope_id)
                for scope_id in blocked_scopes[:5]
            ]
            gate_payload = {
                "status": "blocked",
                "message": (
                    f"当前范围有 {len(blocked_scopes)} 个店铺账期未通过认证门禁。"
                ),
                "code": (
                    next(iter(blocked_codes))
                    if len(blocked_codes) == 1
                    else "scope_incomplete"
                ),
                "details": {
                    "scopeCount": len(expected_scope_ids),
                    "certifiedScopeCount": len(certified_scopes),
                    "blockedScopeCount": len(blocked_scopes),
                    "waitingScopeCount": len(waiting_scopes),
                    "affectedStores": affected,
                },
            }
        elif expected_scope_ids:
            gate_payload = {
                "status": "waiting",
                "message": (
                    f"当前范围仍有 {len(waiting_scopes)} 个店铺账期等待认证。"
                ),
                "code": "scope_incomplete",
                "details": {
                    "scopeCount": len(expected_scope_ids),
                    "certifiedScopeCount": len(certified_scopes),
                    "waitingScopeCount": len(waiting_scopes),
                },
            }
        row_count = int(summary[0])
        passed_count = int(summary[4])
        return {
            "status": "reference_ready" if row_count else "waiting_sources",
            "calculationMode": calculation_mode,
            "period": selected_period,
            "referenceOnly": True,
            "certifiedPerformanceAvailable": bool(expected_scope_ids)
            and expected_scope_ids.issubset(certified_scope_ids),
            "engineGate": gate_payload,
            "rowCount": row_count,
            "storeCount": int(summary[1]),
            "personCount": int(summary[2]),
            "productCount": int(summary[3]),
            "formulaPassCount": passed_count,
            "formulaPassRate": (
                format(Decimal(passed_count) / Decimal(row_count), ".6f")
                if row_count
                else "0.000000"
            ),
            "metrics": {
                "collectedAmount": _decimal_text(summary[5]),
                "refundAmount": _decimal_text(summary[6]),
                "productCost": _decimal_text(summary[7]),
                "advertisingFee": _decimal_text(summary[8]),
                "storeProfit": _decimal_text(summary[9]),
            },
            "assignment": {
                "activeCount": int(assignment[0]),
                "conflictCount": int(assignment[1]),
                "latestEffectiveDate": (
                    assignment[2].isoformat() if assignment[2] is not None else None
                ),
                "provisionalPersonCount": provisional,
            },
            "imports": [
                {
                    "sourceKind": str(item["source_kind"]),
                    "status": str(item["status"]),
                    "snapshotCount": int(item["snapshot_count"]),
                    "rowCount": int(item["row_count"]),
                    "issueCount": int(item["issue_count"]),
                }
                for item in imports
            ],
        }

    @app.get("/api/v1/performance/people")
    def performance_people_endpoint(
        calculation_mode: Annotated[
            Literal["single", "combined"],
            Query(alias="calculationMode"),
        ] = "single",
        period: Annotated[str | None, Query(pattern=r"^\d{4}$")] = None,
        store: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> dict[str, object]:
        with memory() as database:
            selected_period = period
            if selected_period is None:
                value = _required_row(
                    database.execute(
                        """
                        SELECT max(period_token)
                        FROM performance_reference_fact
                        WHERE enterprise_id = ? AND calculation_mode = ?
                          AND (? IS NULL OR store_name = ?)
                        """,
                        [enterprise_id, calculation_mode, store, store],
                    )
                )[0]
                selected_period = str(value) if value is not None else None
            if selected_period is None:
                return {
                    "period": None,
                    "calculationMode": calculation_mode,
                    "referenceOnly": True,
                    "rows": [],
                }
            store_clause = "AND fact.store_name = ?" if store else ""
            parameters: list[object] = [
                enterprise_id,
                calculation_mode,
                selected_period,
            ]
            if store:
                parameters.append(store)
            parameters.append(limit)
            rows = _rows(
                database.execute(
                    f"""
                    SELECT person.person_id, person.display_name, fact.store_name,
                           count(DISTINCT fact.product_id) AS product_count,
                           sum(fact.collected_amount) AS collected_amount,
                           sum(fact.refund_amount) AS refund_amount,
                           sum(fact.product_cost) AS product_cost,
                           sum(fact.advertising_fee) AS advertising_fee,
                           sum(fact.store_profit) AS store_profit,
                           count(*) FILTER (
                               WHERE fact.validation_status = 'failed'
                           ) AS failed_formula_rows
                    FROM performance_reference_fact fact
                    JOIN person_identity person
                      ON person.person_id = fact.person_id
                    WHERE fact.enterprise_id = ?
                      AND fact.calculation_mode = ?
                      AND fact.period_token = ?
                      {store_clause}
                    GROUP BY person.person_id, person.display_name, fact.store_name
                    ORDER BY sum(fact.store_profit) DESC,
                             person.display_name, fact.store_name
                    LIMIT ?
                    """,
                    parameters,
                )
            )
        return {
            "period": selected_period,
            "calculationMode": calculation_mode,
            "referenceOnly": True,
            "rows": [
                {
                    "personId": str(item["person_id"]),
                    "personName": str(item["display_name"]),
                    "storeName": str(item["store_name"]),
                    "productCount": int(item["product_count"]),
                    "collectedAmount": _decimal_text(item["collected_amount"]),
                    "refundAmount": _decimal_text(item["refund_amount"]),
                    "productCost": _decimal_text(item["product_cost"]),
                    "advertisingFee": _decimal_text(item["advertising_fee"]),
                    "storeProfit": _decimal_text(item["store_profit"]),
                    "failedFormulaRows": int(item["failed_formula_rows"]),
                }
                for item in rows
            ],
        }

    @app.get("/api/v1/compute/targets")
    def compute_targets() -> dict[str, object]:
        report_path = workbench.reports / "target-plan.json"
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "scope_start": "2026-02-01",
                "scope_end": date.today().isoformat(),
                "targets": [],
                "review_required": [],
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="店铺与月份计划暂时不可读取",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=503, detail="店铺与月份计划格式无效")
        return payload

    if web_dist is not None and web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
