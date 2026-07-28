from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..audit import record_audit
from ..models import AIModelProfile, AIProvider, AITaskPolicy
from ..security import RequestContext, encrypt_secret, scoped_store_ids
from .agent_auth import AgentContext, secret_hash
from .model_registry import ReconciliationModels
from .schemas import (
    AgentEventCreate,
    AgentHeartbeatRequest,
    AgentJobFailure,
    AgentJobResult,
    AgentRegisterRequest,
    ConnectorCreate,
    ConnectorUpdate,
    DiscoveredFileBatch,
    DiscoveredFileInput,
    EnrollmentTokenCreate,
    LlmConfigUpdate,
    ReviewDecisionCreate,
    ScanRequest,
)
from .utils import aware, context_allows_object, make_model, model_columns, serialize, set_fields, utcnow


ACTIVE_JOB_STATES = {"claimed", "leased", "running", "waiting_review"}
FINAL_JOB_STATES = {"completed", "succeeded", "failed", "cancelled"}
REVIEW_ROLES = {"platform_admin", "admin", "implementer"}
ADMIN_ROLES = {"platform_admin", "admin"}


def _require_role(ctx: RequestContext, allowed: set[str], message: str) -> None:
    if ctx.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "operation_not_allowed", "message": message},
        )


def _require_requested_store_scope(ctx: RequestContext, store_ids: list[str] | None) -> None:
    if ctx.store_ids is not None and store_ids is not None:
        if not store_ids or not set(store_ids).issubset(ctx.store_ids):
            raise HTTPException(
                status_code=403,
                detail={"code": "store_scope_denied", "message": "所选店铺超出当前账号的经营范围。"},
            )


def _validate_store_scope(
    db: Session,
    ctx: RequestContext,
    store_ids: list[str] | None,
) -> None:
    _require_requested_store_scope(ctx, store_ids)
    if store_ids:
        scoped_store_ids(db, ctx, store_ids)


def _column(model: type[Any], *names: str) -> Any:
    columns = model_columns(model)
    for name in names:
        if name in columns:
            return getattr(model, name)
    raise RuntimeError(f"{model.__name__} is missing required columns: {', '.join(names)}")


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _job_state(job: Any) -> str:
    return str(_value(job, "status", "state", default="unknown"))


def _job_response(job: Any) -> dict[str, Any]:
    result = serialize(job)
    state = _job_state(job)
    result["status"] = state
    result["state"] = state
    current = int(_value(job, "progress", "progress_current", default=0) or 0)
    total = _value(job, "progress_total", default=100) or 100
    result["progress"] = min(100, round(current * 100 / total)) if total else 0
    return result


def host_agent_job_response(job: dict[str, Any]) -> dict[str, Any]:
    kind = {
        "source_scan": "scan",
        "scan": "scan",
        "profile": "profile",
        "recompute": "recompute",
    }.get(str(job.get("job_type")), str(job.get("job_type")))
    return {
        "id": job["id"],
        "kind": kind,
        "payload": job.get("payload") or {},
        "idempotency_key": (job.get("payload") or {}).get("idempotency_key") or job["id"],
    }


def _connector_response(connector: Any) -> dict[str, Any]:
    result = serialize(connector)
    result["name"] = _value(connector, "name", "logical_key", default=connector.id)
    result["status"] = (
        _value(connector, "status")
        or ("active" if getattr(connector, "enabled", False) else "disabled")
    )
    result["config"] = _value(connector, "config", "read_policy", default={}) or {}
    result["store_ids"] = list(
        _value(connector, "store_ids", default=None)
        or result["config"].get("store_ids", [])
    )
    return result


def _browser_audit(
    db: Session,
    ctx: RequestContext,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    record_audit(db, ctx, action, resource_type, resource_id, details)


def _agent_audit(
    db: Session,
    agent: AgentContext,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    record_audit(
        db,
        RequestContext(agent.enterprise_id, agent.id, "external_agent"),
        action,
        resource_type,
        resource_id,
        details,
    )


def create_enrollment_token(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    body: EnrollmentTokenCreate,
) -> dict[str, Any]:
    _require_role(ctx, ADMIN_ROLES, "只有管理员可以创建执行器注册令牌。")
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="当前账号没有企业范围。")
    plain_token = secrets.token_urlsafe(48)
    expires_at = utcnow() + timedelta(minutes=body.expires_in_minutes)
    token = make_model(
        models.AgentEnrollmentToken,
        enterprise_id=ctx.enterprise_id,
        label=body.label,
        token_hash=secret_hash(plain_token),
        expires_at=expires_at,
        created_by=ctx.user_id,
        created_at=utcnow(),
    )
    db.add(token)
    db.flush()
    _browser_audit(
        db,
        ctx,
        "create",
        "agent_enrollment_token",
        token.id,
        {"label": body.label, "expires_at": expires_at.isoformat()},
    )
    db.commit()
    return {
        "id": token.id,
        "enrollment_token": plain_token,
        "expires_at": expires_at.isoformat(),
        "message": "注册令牌只显示这一次，请立即交给目标执行器。",
    }


def register_agent(
    db: Session,
    models: ReconciliationModels,
    body: AgentRegisterRequest,
) -> dict[str, Any]:
    token = db.scalar(
        select(models.AgentEnrollmentToken).where(
            models.AgentEnrollmentToken.token_hash == secret_hash(body.enrollment_token)
        )
    )
    if (
        not token
        or getattr(token, "used_at", None) is not None
        or aware(token.expires_at) <= utcnow()
    ):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_enrollment_token", "message": "注册令牌无效、已使用或已过期。"},
        )
    machine_column = _column(models.ExternalAgent, "machine_name", "machine_key")
    existing_agent = db.scalar(
        select(models.ExternalAgent.id).where(
            models.ExternalAgent.enterprise_id == token.enterprise_id,
            machine_column == body.machine_name,
        )
    )
    if existing_agent:
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_already_enrolled", "message": "这台主机已经注册，请停用旧执行器后再重新注册。"},
        )
    plain_secret = secrets.token_urlsafe(64)
    agent = make_model(
        models.ExternalAgent,
        enterprise_id=token.enterprise_id,
        name=body.name,
        display_name=body.name,
        machine_name=body.machine_name,
        machine_key=body.machine_name,
        secret_hash=secret_hash(plain_secret),
        agent_key_hash=secret_hash(plain_secret),
        status="offline",
        capabilities=body.capabilities,
        agent_version=body.agent_version,
        version=body.agent_version,
        last_heartbeat_at=utcnow(),
        created_by=getattr(token, "created_by", "system"),
        created_at=utcnow(),
    )
    db.add(agent)
    db.flush()
    used_at = utcnow()
    token_update_values: dict[str, Any] = {"used_at": used_at}
    if "used_by_agent_id" in model_columns(models.AgentEnrollmentToken):
        token_update_values["used_by_agent_id"] = agent.id
    claim_result = db.execute(
        update(models.AgentEnrollmentToken)
        .where(
            models.AgentEnrollmentToken.id == token.id,
            models.AgentEnrollmentToken.used_at.is_(None),
            models.AgentEnrollmentToken.expires_at > used_at,
        )
        .values(**token_update_values)
        .execution_options(synchronize_session=False)
    )
    if claim_result.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_enrollment_token", "message": "注册令牌已被其他执行器使用。"},
        )
    _agent_audit(
        db,
        AgentContext(
            agent.id,
            agent.enterprise_id,
            _value(agent, "name", "display_name", default=agent.id),
        ),
        "register",
        "external_agent",
        agent.id,
        {"machine_name": body.machine_name, "capabilities": body.capabilities},
    )
    db.commit()
    return {
        "agent": serialize(agent),
        "agent_secret": plain_secret,
        "message": "代理密钥只显示这一次，服务端不会保存明文。",
    }


def heartbeat_agent(
    db: Session,
    models: ReconciliationModels,
    agent_ctx: AgentContext,
    body: AgentHeartbeatRequest,
) -> dict[str, Any]:
    agent = db.get(models.ExternalAgent, agent_ctx.id)
    if not agent or agent.enterprise_id != agent_ctx.enterprise_id:
        raise HTTPException(status_code=401, detail="执行器身份已失效。")
    values: dict[str, Any] = {
        "last_heartbeat_at": utcnow(),
        "status": body.status if "secret_hash" in model_columns(models.ExternalAgent) else "online",
        "diagnostics": body.diagnostics,
    }
    if body.capabilities is not None:
        values["capabilities"] = body.capabilities
    if body.agent_version is not None:
        values["agent_version"] = body.agent_version
        values["version"] = body.agent_version
    set_fields(agent, **values)
    db.commit()
    return {"status": "ok", "server_time": utcnow().isoformat()}


def claim_next_job(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
) -> dict[str, Any] | None:
    """Claim one job with a compare-and-set update.

    PostgreSQL additionally locks the candidate row with SKIP LOCKED. The
    status predicate on UPDATE keeps SQLite tests and accidental duplicate
    pollers safe as well.
    """

    state_column = _column(models.AgentJob, "status", "state")
    for _ in range(3):
        candidate_stmt = (
            select(models.AgentJob.id)
            .where(
                models.AgentJob.enterprise_id == agent.enterprise_id,
                state_column == "queued",
                or_(models.AgentJob.agent_id.is_(None), models.AgentJob.agent_id == agent.id),
            )
            .order_by(models.AgentJob.priority.desc(), models.AgentJob.created_at.asc())
            .limit(1)
        )
        if db.get_bind().dialect.name == "postgresql":
            candidate_stmt = candidate_stmt.with_for_update(skip_locked=True)
        candidate_id = db.scalar(candidate_stmt)
        if not candidate_id:
            return None
        claimed_at = utcnow()
        claim_values: dict[str, Any] = {
            "agent_id": agent.id,
            "status": "claimed",
            "state": "leased",
            "claimed_at": claimed_at,
            "started_at": claimed_at,
            "updated_at": claimed_at,
            "lease_owner": agent.id,
            "lease_expires_at": claimed_at + timedelta(minutes=5),
        }
        claim_values = {
            key: value
            for key, value in claim_values.items()
            if key in model_columns(models.AgentJob)
        }
        result = db.execute(
            update(models.AgentJob)
            .where(
                models.AgentJob.id == candidate_id,
                models.AgentJob.enterprise_id == agent.enterprise_id,
                state_column == "queued",
                or_(models.AgentJob.agent_id.is_(None), models.AgentJob.agent_id == agent.id),
            )
            .values(**claim_values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            job = db.get(models.AgentJob, candidate_id)
            _agent_audit(db, agent, "claim", "agent_job", job.id)
            db.commit()
            return _job_response(job)
        db.rollback()
    return None


def _owned_job(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    *,
    final_allowed: bool = False,
) -> Any:
    job = db.scalar(
        select(models.AgentJob).where(
            models.AgentJob.id == job_id,
            models.AgentJob.enterprise_id == agent.enterprise_id,
            models.AgentJob.agent_id == agent.id,
        )
    )
    if not job:
        raise HTTPException(status_code=404, detail="没有找到分配给当前执行器的任务。")
    if not final_allowed and _job_state(job) in FINAL_JOB_STATES:
        raise HTTPException(status_code=409, detail="任务已经结束，不能继续写入。")
    return job


def append_job_event(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: AgentEventCreate,
) -> dict[str, Any]:
    job = _owned_job(db, models, agent, job_id)
    event_columns = model_columns(models.AgentJobEvent)
    if "external_event_id" in event_columns:
        existing = db.scalar(
            select(models.AgentJobEvent).where(
                models.AgentJobEvent.enterprise_id == agent.enterprise_id,
                models.AgentJobEvent.job_id == job_id,
                models.AgentJobEvent.external_event_id == body.external_event_id,
            )
        )
        if existing:
            return serialize(existing)
    else:
        existing_events = db.scalars(
            select(models.AgentJobEvent).where(
                models.AgentJobEvent.enterprise_id == agent.enterprise_id,
                models.AgentJobEvent.job_id == job_id,
            )
        ).all()
        existing = next(
            (
                event
                for event in existing_events
                if (getattr(event, "details", {}) or {}).get("external_event_id")
                == body.external_event_id
            ),
            None,
        )
        if existing:
            return serialize(existing)
    next_sequence = (
        db.scalar(
            select(func.max(models.AgentJobEvent.sequence)).where(
                models.AgentJobEvent.enterprise_id == agent.enterprise_id,
                models.AgentJobEvent.job_id == job_id,
            )
        )
        or 0
    ) + 1
    event_details = dict(body.details)
    event_details.update(
        {
            "external_event_id": body.external_event_id,
            "stage": body.stage,
            "progress": body.progress,
        }
    )
    event = make_model(
        models.AgentJobEvent,
        enterprise_id=agent.enterprise_id,
        job_id=job_id,
        agent_id=agent.id,
        external_event_id=body.external_event_id,
        sequence=next_sequence,
        event_type=body.event_type,
        stage=body.stage,
        progress=body.progress,
        message=body.message,
        details=event_details,
        created_at=utcnow(),
    )
    db.add(event)
    current_state = _job_state(job)
    job_status = {
        "started": "running",
        "progress": "running",
        "resumed": "running",
        "waiting_review": "waiting_review",
    }.get(body.event_type, current_state)
    set_fields(
        job,
        status=job_status,
        state=job_status,
        stage=body.stage,
        progress=body.progress,
        progress_current=body.progress,
        progress_total=100,
        updated_at=utcnow(),
        lease_expires_at=utcnow() + timedelta(minutes=5),
    )
    db.commit()
    db.refresh(event)
    return serialize(event)


def _event_with_key(
    db: Session,
    models: ReconciliationModels,
    enterprise_id: str,
    job_id: str,
    event_key: str,
) -> Any | None:
    events = db.scalars(
        select(models.AgentJobEvent).where(
            models.AgentJobEvent.enterprise_id == enterprise_id,
            models.AgentJobEvent.job_id == job_id,
        )
    ).all()
    for event in events:
        if getattr(event, "external_event_id", None) == event_key:
            return event
        if (getattr(event, "details", {}) or {}).get("external_event_id") == event_key:
            return event
    return None


def finish_job_idempotently(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: AgentJobResult,
    event_key: str,
) -> dict[str, Any]:
    job = _owned_job(db, models, agent, job_id, final_allowed=True)
    existing_event = _event_with_key(db, models, agent.enterprise_id, job_id, event_key)
    if existing_event and _job_state(job) in FINAL_JOB_STATES:
        return _job_response(job)
    if _job_state(job) in FINAL_JOB_STATES:
        raise HTTPException(status_code=409, detail="任务已由另一条终态事件结束。")
    raw_files = body.result.get("files")
    if isinstance(raw_files, list) and raw_files:
        discovered: list[DiscoveredFileInput] = []
        for raw in raw_files[:1000]:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "")
            source_id = str(raw.get("source_id") or "")
            if not path or len(source_id) < 16:
                continue
            attributes = {
                str(flag): True for flag in (raw.get("attributes") or []) if str(flag)
            }
            attributes["purpose"] = raw.get("purpose")
            discovered.append(
                DiscoveredFileInput(
                    fingerprint=source_id,
                    source_path=path,
                    file_name=PureWindowsPath(path).name,
                    size_bytes=int(raw.get("size") or 0),
                    modified_at=raw.get("mtime_utc") or utcnow(),
                    attributes=attributes,
                    content_sha256=raw.get("sha256"),
                )
            )
        if discovered:
            report_discovered_files(
                db,
                models,
                agent,
                job_id,
                DiscoveredFileBatch(files=discovered),
            )
    append_job_event(
        db,
        models,
        agent,
        job_id,
        AgentEventCreate(
            external_event_id=event_key,
            event_type="completed",
            stage="completed",
            progress=100,
            message=body.message or "外部执行器已完成任务",
            details={"replayed": body.replayed},
        ),
    )
    return finish_job(db, models, agent, job_id, body)


def fail_job_idempotently(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: AgentJobFailure,
    event_key: str,
) -> dict[str, Any]:
    job = _owned_job(db, models, agent, job_id, final_allowed=True)
    existing_event = _event_with_key(db, models, agent.enterprise_id, job_id, event_key)
    if existing_event and _job_state(job) in FINAL_JOB_STATES:
        return _job_response(job)
    if _job_state(job) in FINAL_JOB_STATES:
        raise HTTPException(status_code=409, detail="任务已由另一条终态事件结束。")
    append_job_event(
        db,
        models,
        agent,
        job_id,
        AgentEventCreate(
            external_event_id=event_key,
            event_type="failed",
            stage="failed",
            progress=int(_value(job, "progress", "progress_current", default=0) or 0),
            message=body.message,
            details={"error_code": body.error_code, **body.details},
        ),
    )
    return fail_job(db, models, agent, job_id, body)


def report_discovered_files(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: DiscoveredFileBatch,
) -> dict[str, int]:
    job = _owned_job(db, models, agent, job_id)
    connector_id = getattr(job, "connector_id", None)
    fingerprint_column = _column(models.DiscoveredFile, "fingerprint", "path_key")
    created = 0
    duplicates = 0
    for item in body.files:
        existing = db.scalar(
            select(models.DiscoveredFile.id).where(
                models.DiscoveredFile.enterprise_id == agent.enterprise_id,
                models.DiscoveredFile.connector_id == connector_id,
                fingerprint_column == item.fingerprint,
            )
        )
        if existing:
            duplicates += 1
            continue
        db.add(
            make_model(
                models.DiscoveredFile,
                enterprise_id=agent.enterprise_id,
                job_id=job_id,
                connector_id=connector_id,
                agent_id=agent.id,
                fingerprint=item.fingerprint,
                path_key=item.fingerprint,
                source_path=item.source_path,
                full_path=item.source_path,
                file_name=item.file_name,
                extension=PureWindowsPath(item.file_name).suffix.lower(),
                size_bytes=item.size_bytes,
                modified_at=item.modified_at,
                observed_mtime=item.modified_at,
                attributes=item.attributes,
                safety_flags=[
                    str(key)
                    for key, value in item.attributes.items()
                    if value is True and key in {"offline", "unpinned", "recall_on_data_access", "unstable"}
                ],
                content_sha256=item.content_sha256,
                sha256=item.content_sha256,
                status="discovered",
                last_seen_at=utcnow(),
                created_at=utcnow(),
            )
        )
        created += 1
    _agent_audit(
        db,
        agent,
        "report_files",
        "agent_job",
        job_id,
        {"created": created, "duplicates": duplicates},
    )
    db.commit()
    return {"created": created, "duplicates": duplicates}


def finish_job(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: AgentJobResult,
) -> dict[str, Any]:
    job = _owned_job(db, models, agent, job_id)
    now = utcnow()
    set_fields(
        job,
        status="completed",
        state="succeeded",
        stage="completed",
        progress=100,
        progress_current=100,
        progress_total=100,
        result=body.result,
        result_payload=body.result,
        finished_at=now,
        updated_at=now,
        error_message=None,
        lease_owner=None,
        lease_expires_at=None,
    )
    _agent_audit(db, agent, "complete", "agent_job", job_id, {"message": body.message})
    db.commit()
    return _job_response(job)


def fail_job(
    db: Session,
    models: ReconciliationModels,
    agent: AgentContext,
    job_id: str,
    body: AgentJobFailure,
) -> dict[str, Any]:
    job = _owned_job(db, models, agent, job_id)
    now = utcnow()
    status = "queued" if body.retryable else "failed"
    set_fields(
        job,
        status=status,
        state=status,
        stage="retry_wait" if body.retryable else "failed",
        agent_id=None if body.retryable else agent.id,
        error_code=body.error_code,
        error_message=body.message,
        error_details=body.details,
        finished_at=None if body.retryable else now,
        updated_at=now,
        result={
            "error_code": body.error_code,
            "message": body.message,
            "details": body.details,
            "retryable": body.retryable,
        },
        lease_owner=None,
        lease_expires_at=None,
    )
    _agent_audit(
        db,
        agent,
        "fail",
        "agent_job",
        job_id,
        {"error_code": body.error_code, "retryable": body.retryable},
    )
    db.commit()
    return _job_response(job)


def _validate_connector_agent(
    db: Session,
    models: ReconciliationModels,
    enterprise_id: str,
    agent_id: str | None,
) -> None:
    if not agent_id:
        return
    agent = db.get(models.ExternalAgent, agent_id)
    if not agent or agent.enterprise_id != enterprise_id:
        raise HTTPException(status_code=422, detail="所选执行器不属于当前企业。")


def create_connector(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    body: ConnectorCreate,
) -> dict[str, Any]:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能配置数据来源。")
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="当前账号没有企业范围。")
    _validate_store_scope(db, ctx, body.store_ids)
    if "agent_id" in model_columns(models.SourceConnector) and not body.agent_id:
        raise HTTPException(status_code=422, detail="请选择负责读取该来源的外部执行器。")
    _validate_connector_agent(db, models, ctx.enterprise_id, body.agent_id)
    name_column = _column(models.SourceConnector, "name", "logical_key")
    if db.scalar(
        select(models.SourceConnector.id).where(
            models.SourceConnector.enterprise_id == ctx.enterprise_id,
            name_column == body.name,
        )
    ):
        raise HTTPException(status_code=409, detail="当前企业已经存在同名数据来源。")
    connector_type = {
        "finance_win": "directory",
        "filesystem": "directory",
        "powerbi_activity": "bi_activity",
        "pbix_inventory": "pbix_inventory",
    }[body.connector_type]
    roots = body.config.get("roots") or []
    root_path = body.config.get("root_path") or (roots[0] if roots else None)
    if "root_path" in model_columns(models.SourceConnector) and not root_path:
        raise HTTPException(status_code=422, detail="请配置只读数据目录。")
    read_policy = {**body.config, "store_ids": body.store_ids}
    connector = make_model(
        models.SourceConnector,
        enterprise_id=ctx.enterprise_id,
        name=body.name,
        logical_key=body.name,
        connector_type=connector_type,
        purpose=body.config.get("purpose", body.connector_type),
        root_path=root_path,
        agent_id=body.agent_id,
        store_ids=body.store_ids,
        config=read_policy,
        read_policy=read_policy,
        enabled=body.enabled,
        status="active" if body.enabled else "disabled",
        created_by=ctx.user_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(connector)
    db.flush()
    _browser_audit(db, ctx, "create", "source_connector", connector.id)
    db.commit()
    return _connector_response(connector)


def list_connectors(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    *,
    require_manage: bool = True,
) -> list[dict[str, Any]]:
    if require_manage:
        _require_role(ctx, REVIEW_ROLES, "当前账号不能查看数据来源设置。")
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="当前账号没有企业范围。")
    items = db.scalars(
        select(models.SourceConnector)
        .where(models.SourceConnector.enterprise_id == ctx.enterprise_id)
        .order_by(models.SourceConnector.created_at.desc())
    ).all()
    return [_connector_response(item) for item in items if context_allows_object(ctx, item)]


def get_connector(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    connector_id: str,
) -> Any:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能查看数据来源设置。")
    connector = db.scalar(
        select(models.SourceConnector).where(
            models.SourceConnector.id == connector_id,
            models.SourceConnector.enterprise_id == ctx.enterprise_id,
        )
    )
    if not connector or not context_allows_object(ctx, connector):
        raise HTTPException(status_code=404, detail="没有找到这个数据来源。")
    return connector


def update_connector(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    connector_id: str,
    body: ConnectorUpdate,
) -> dict[str, Any]:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能修改数据来源。")
    connector = get_connector(db, ctx, models, connector_id)
    changes = body.model_dump(exclude_unset=True)
    if "store_ids" in changes:
        _validate_store_scope(db, ctx, changes["store_ids"])
    if "agent_id" in changes:
        _validate_connector_agent(db, models, ctx.enterprise_id or "", changes["agent_id"])
    if "name" in changes:
        changes["logical_key"] = changes["name"]
    if "store_ids" in changes:
        config = dict(getattr(connector, "config", {}) or {})
        config["store_ids"] = changes["store_ids"]
        changes["config"] = {**config, **(changes.get("config") or {})}
    elif "config" in changes:
        config = dict(changes["config"] or {})
        config["store_ids"] = list(getattr(connector, "store_ids", None) or [])
        changes["config"] = config
    if "config" in changes:
        read_policy = dict(changes["config"] or {})
        changes["read_policy"] = read_policy
        roots = read_policy.get("roots") or []
        if read_policy.get("root_path") or roots:
            changes["root_path"] = read_policy.get("root_path") or roots[0]
    if "enabled" in changes:
        changes["status"] = "active" if changes["enabled"] else "disabled"
    changes["updated_at"] = utcnow()
    set_fields(connector, **changes)
    _browser_audit(db, ctx, "update", "source_connector", connector.id, {"fields": sorted(changes)})
    db.commit()
    return _connector_response(connector)


def archive_connector(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    connector_id: str,
) -> dict[str, Any]:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能停用数据来源。")
    connector = get_connector(db, ctx, models, connector_id)
    set_fields(
        connector,
        status="archived",
        enabled=False,
        archived_at=utcnow(),
        updated_at=utcnow(),
    )
    _browser_audit(db, ctx, "archive", "source_connector", connector.id)
    db.commit()
    return {"id": connector.id, "status": "archived", "enabled": False}


def enqueue_scan(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    connector_id: str,
    body: ScanRequest,
) -> dict[str, Any]:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能开始数据扫描。")
    connector = get_connector(db, ctx, models, connector_id)
    if not getattr(connector, "enabled", True) or getattr(connector, "status", "active") in {"archived", "disabled"}:
        raise HTTPException(status_code=409, detail="数据来源已停用，不能开始扫描。")
    active = db.scalar(
        select(models.AgentJob).where(
            models.AgentJob.enterprise_id == ctx.enterprise_id,
            models.AgentJob.connector_id == connector.id,
            _column(models.AgentJob, "status", "state").in_(ACTIVE_JOB_STATES | {"queued"}),
        )
    )
    if active:
        return {"job": _job_response(active), "already_queued": True}
    payload = {
        "connector_type": connector.connector_type,
        "connector_config": _value(connector, "config", "read_policy", default={}) or {},
        "store_ids": list(
            _value(connector, "store_ids", default=None)
            or (_value(connector, "config", "read_policy", default={}) or {}).get("store_ids", [])
        ),
        "reason": body.reason,
        "full_scan": body.full_scan,
    }
    job = make_model(
        models.AgentJob,
        enterprise_id=ctx.enterprise_id,
        connector_id=connector.id,
        agent_id=getattr(connector, "agent_id", None),
        job_type="scan",
        status="queued",
        state="queued",
        priority=50,
        stage="queued",
        progress=0,
        payload=payload,
        created_by=ctx.user_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(job)
    db.flush()
    _browser_audit(db, ctx, "enqueue_scan", "agent_job", job.id, {"connector_id": connector.id})
    db.commit()
    return {"job": _job_response(job), "already_queued": False}


def list_operations(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    state_column = _column(models.AgentJob, "status", "state")
    stmt = select(models.AgentJob).where(models.AgentJob.enterprise_id == ctx.enterprise_id)
    if status:
        normalized = "succeeded" if status == "completed" and "state" in model_columns(models.AgentJob) else status
        stmt = stmt.where(state_column == normalized)
    items = db.scalars(stmt.order_by(models.AgentJob.created_at.desc()).limit(limit)).all()
    return [_public_operation(item) for item in items if context_allows_object(ctx, item)]


def get_operation(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    job_id: str,
) -> Any:
    job = db.scalar(
        select(models.AgentJob).where(
            models.AgentJob.id == job_id,
            models.AgentJob.enterprise_id == ctx.enterprise_id,
        )
    )
    if not job or not context_allows_object(ctx, job):
        raise HTTPException(status_code=404, detail="没有找到这个处理任务。")
    return job


def _public_operation(job: Any) -> dict[str, Any]:
    result = _job_response(job)
    payload = result.get("payload") or {}
    result["payload"] = {
        key: payload[key]
        for key in {"store_ids", "reason", "full_scan", "connector_type"}
        if key in payload
    }
    return result


def list_operation_events(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    job_id: str | None = None,
    after_sequence: int = 0,
) -> list[dict[str, Any]]:
    if job_id:
        get_operation(db, ctx, models, job_id)
    stmt = select(models.AgentJobEvent).where(
        models.AgentJobEvent.enterprise_id == ctx.enterprise_id,
        models.AgentJobEvent.sequence > after_sequence,
    )
    if job_id:
        stmt = stmt.where(models.AgentJobEvent.job_id == job_id)
    events = db.scalars(stmt.order_by(models.AgentJobEvent.created_at, models.AgentJobEvent.sequence)).all()
    if ctx.store_ids is None:
        visible = [serialize(event) for event in events]
    else:
        allowed_job_ids = {
            job.id
            for job in db.scalars(
                select(models.AgentJob).where(
                    models.AgentJob.enterprise_id == ctx.enterprise_id,
                    models.AgentJob.id.in_({event.job_id for event in events}),
                )
            ).all()
            if context_allows_object(ctx, job)
        }
        visible = [serialize(event) for event in events if event.job_id in allowed_job_ids]
    if ctx.role in {"analyst", "viewer"}:
        for event in visible:
            event["details"] = {}
    return visible


def workbench_overview(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
) -> dict[str, Any]:
    connectors = list_connectors(db, ctx, models, require_manage=False)
    operations = list_operations(db, ctx, models, limit=20)
    can_review = ctx.role in REVIEW_ROLES
    reviews = list_review_items(db, ctx, models, status="open") if can_review else []
    agents = db.scalars(
        select(models.ExternalAgent).where(models.ExternalAgent.enterprise_id == ctx.enterprise_id)
    ).all()
    visible_agent_ids = {item.get("agent_id") for item in connectors}
    visible_agents = []
    for agent in agents:
        if ctx.store_ids is not None and agent.id not in visible_agent_ids:
            continue
        serialized = serialize(agent)
        visible_agents.append(
            {
                "id": agent.id,
                "name": _value(agent, "name", "display_name", default=agent.id),
                "status": agent.status,
                "last_heartbeat_at": serialized.get("last_heartbeat_at"),
                "agent_version": _value(agent, "agent_version", "version"),
            }
        )
    return {
        "connectors": {
            "total": len(connectors),
            "active": sum(item.get("status") == "active" for item in connectors),
        },
        "agents": visible_agents,
        "current_operation": next(
            (item for item in operations if item.get("status") not in FINAL_JOB_STATES),
            None,
        ),
        "pending_review_count": len(reviews) if can_review else None,
        "can_review": can_review,
        "recent_operations": operations[:5],
        "generated_at": utcnow().isoformat(),
    }


def list_review_items(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    *,
    status: str | None = "open",
) -> list[dict[str, Any]]:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能查看确认收件箱。")
    stmt = select(models.ReviewItem).where(models.ReviewItem.enterprise_id == ctx.enterprise_id)
    if status and status != "all":
        if "requested_by" in model_columns(models.ReviewItem):
            statuses = {"pending", "claimed"} if status == "open" else {status}
        else:
            statuses = {"open", "claimed", "pending_approval"} if status == "open" else {status}
        stmt = stmt.where(models.ReviewItem.status.in_(statuses))
    items = db.scalars(stmt.order_by(models.ReviewItem.created_at.desc())).all()
    return [_review_response(item) for item in items if context_allows_object(ctx, item)]


def _review_response(item: Any) -> dict[str, Any]:
    result = serialize(item)
    result["claimed_by"] = _value(item, "claimed_by", "assigned_to")
    result["created_by"] = _value(item, "created_by", "requested_by")
    result["requires_approval"] = bool(
        _value(item, "requires_approval", default=False)
        or getattr(item, "risk_level", "normal") in {"high", "critical"}
    )
    return result


def get_review_item(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    item_id: str,
) -> Any:
    _require_role(ctx, REVIEW_ROLES, "当前账号不能处理确认事项。")
    item = db.scalar(
        select(models.ReviewItem).where(
            models.ReviewItem.id == item_id,
            models.ReviewItem.enterprise_id == ctx.enterprise_id,
        )
    )
    if not item or not context_allows_object(ctx, item):
        raise HTTPException(status_code=404, detail="没有找到这个待确认事项。")
    return item


def claim_review_item(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    item_id: str,
) -> dict[str, Any]:
    item = get_review_item(db, ctx, models, item_id)
    if item.status not in {"open", "pending", "claimed"}:
        raise HTTPException(status_code=409, detail="这个事项当前不能领取。")
    assignee_column = _column(models.ReviewItem, "claimed_by", "assigned_to")
    claim_values: dict[str, Any] = {"status": "claimed"}
    claim_values["claimed_by" if "claimed_by" in model_columns(models.ReviewItem) else "assigned_to"] = ctx.user_id
    if "claimed_at" in model_columns(models.ReviewItem):
        claim_values["claimed_at"] = utcnow()
    result = db.execute(
        update(models.ReviewItem)
        .where(
            models.ReviewItem.id == item.id,
            models.ReviewItem.enterprise_id == ctx.enterprise_id,
            models.ReviewItem.status.in_({"open", "pending", "claimed"}),
            or_(assignee_column.is_(None), assignee_column == ctx.user_id),
        )
        .values(**claim_values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="这个事项已由其他同事领取。")
    _browser_audit(db, ctx, "claim", "review_item", item.id)
    db.commit()
    return _review_response(db.get(models.ReviewItem, item.id))


def decide_review_item(
    db: Session,
    ctx: RequestContext,
    models: ReconciliationModels,
    item_id: str,
    body: ReviewDecisionCreate,
) -> dict[str, Any]:
    item = get_review_item(db, ctx, models, item_id)
    if item.status in {"confirmed", "rejected", "closed", "decided", "cancelled"}:
        raise HTTPException(status_code=409, detail="这个事项已经处理完成。")
    claimed_by = _value(item, "claimed_by", "assigned_to")
    if claimed_by and claimed_by != ctx.user_id and ctx.role not in ADMIN_ROLES:
        raise HTTPException(status_code=409, detail="这个事项已由其他同事领取。")
    requires_approval = bool(
        getattr(item, "requires_approval", False)
        or getattr(item, "risk_level", "normal") in {"high", "critical"}
    )
    maker_id = _value(item, "created_by", "proposed_by", "requested_by")
    if requires_approval and body.action == "confirm":
        if ctx.role not in ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="高影响事项需要管理员复核。")
        if maker_id and maker_id == ctx.user_id:
            raise HTTPException(status_code=409, detail="提出人不能审批自己提交的高影响事项。")
    if body.action == "request_approval" and ctx.role not in REVIEW_ROLES:
        raise HTTPException(status_code=403, detail="当前账号不能提交复核。")
    decision = make_model(
        models.ReviewDecision,
        enterprise_id=ctx.enterprise_id,
        review_item_id=item.id,
        action=body.action,
        decision={
            "confirm": "accept",
            "reject": "reject",
            "request_approval": "escalate",
        }[body.action],
        reason_code=str(body.payload.get("reason_code") or body.action),
        note=body.note,
        rationale=body.note,
        payload=body.payload,
        decision_payload=body.payload,
        disposition=body.payload,
        decided_by=ctx.user_id,
        decided_role=ctx.role,
        created_at=utcnow(),
    )
    db.add(decision)
    actual_review_schema = "requested_by" in model_columns(models.ReviewItem)
    target_status = (
        {
            "confirm": "decided",
            "reject": "decided",
            "request_approval": "pending",
        }
        if actual_review_schema
        else {
            "confirm": "confirmed",
            "reject": "rejected",
            "request_approval": "pending_approval",
        }
    )[body.action]
    set_fields(
        item,
        status=target_status,
        assigned_to=None if body.action == "request_approval" else claimed_by,
        decided_by=ctx.user_id if body.action != "request_approval" else None,
        decided_at=utcnow() if body.action != "request_approval" else None,
    )
    _browser_audit(
        db,
        ctx,
        body.action,
        "review_item",
        item.id,
        {"requires_approval": requires_approval},
    )
    db.commit()
    return {"item": _review_response(item), "decision": serialize(decision)}


def _active_ai_provider(db: Session, enterprise_id: str) -> AIProvider | None:
    return db.scalar(
        select(AIProvider)
        .where(
            AIProvider.enterprise_id == enterprise_id,
            AIProvider.archived_at.is_(None),
            AIProvider.status != "archived",
        )
        .order_by(AIProvider.created_at.desc())
    )


def get_llm_config(db: Session, ctx: RequestContext) -> dict[str, Any]:
    _require_role(ctx, ADMIN_ROLES, "只有管理员可以查看 LLM 绑定配置。")
    provider = _active_ai_provider(db, ctx.enterprise_id or "")
    if not provider:
        return {
            "provider": None,
            "models": [],
            "task_policies": [],
            "deterministic_processing_available": True,
        }
    models = db.scalars(
        select(AIModelProfile).where(
            AIModelProfile.enterprise_id == ctx.enterprise_id,
            AIModelProfile.provider_id == provider.id,
            AIModelProfile.archived_at.is_(None),
        )
    ).all()
    policies = db.scalars(
        select(AITaskPolicy).where(
            AITaskPolicy.enterprise_id == ctx.enterprise_id,
            AITaskPolicy.archived_at.is_(None),
        )
    ).all()
    model_by_id = {model.id: model.name for model in models}
    return {
        "provider": {
            "id": provider.id,
            "name": provider.name,
            "mode": provider.mode,
            "api_base": provider.api_base,
            "has_api_key": bool(provider.encrypted_api_key),
            "status": provider.status,
        },
        "models": [
            {
                "id": model.id,
                "name": model.name,
                "model_name": model.model_name,
                "timeout_seconds": model.timeout_seconds,
                "max_retries": model.max_retries,
                "budget_cents": model.budget_cents,
            }
            for model in models
        ],
        "task_policies": [
            {
                "id": policy.id,
                "task": policy.task,
                "primary_model": model_by_id.get(policy.primary_model_id),
                "fallback_model": model_by_id.get(policy.fallback_model_id),
                "enabled": policy.enabled,
                "redaction_policy": policy.redaction_policy,
            }
            for policy in policies
        ],
        "deterministic_processing_available": True,
    }


def update_llm_config(
    db: Session,
    ctx: RequestContext,
    body: LlmConfigUpdate,
) -> dict[str, Any]:
    _require_role(ctx, ADMIN_ROLES, "只有管理员可以修改 LLM 绑定配置。")
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="当前账号没有企业范围。")
    names = [model.name for model in body.models]
    if len(names) != len(set(names)):
        raise HTTPException(status_code=422, detail="模型名称不能重复。")
    known = set(names)
    for policy in body.task_policies:
        if policy.primary_model and policy.primary_model not in known:
            raise HTTPException(status_code=422, detail=f"任务 {policy.task} 的主模型不存在。")
        if policy.fallback_model and policy.fallback_model not in known:
            raise HTTPException(status_code=422, detail=f"任务 {policy.task} 的备用模型不存在。")
    current = _active_ai_provider(db, ctx.enterprise_id)
    parsed_base = urlparse(str(body.provider.api_base)) if body.provider.api_base else None
    if parsed_base and (parsed_base.username or parsed_base.password):
        raise HTTPException(status_code=422, detail="模型服务地址不能包含用户名或密码。")
    encrypted_key = (
        None
        if body.provider.clear_api_key
        else encrypt_secret(body.provider.api_key)
        if body.provider.api_key
        else current.encrypted_api_key
        if current
        else None
    )
    if body.provider.mode == "cloud" and not encrypted_key:
        raise HTTPException(status_code=422, detail="云模型需要配置 API Key。")
    now = utcnow()
    old_models = db.scalars(
        select(AIModelProfile).where(
            AIModelProfile.enterprise_id == ctx.enterprise_id,
            AIModelProfile.archived_at.is_(None),
        )
    ).all()
    old_policies = db.scalars(
        select(AITaskPolicy).where(
            AITaskPolicy.enterprise_id == ctx.enterprise_id,
            AITaskPolicy.archived_at.is_(None),
        )
    ).all()
    for obj in [value for value in [current] if value] + list(old_models) + list(old_policies):
        obj.status = "archived"
        obj.archived_at = now
        obj.effective_to = now
    provider = AIProvider(
        enterprise_id=ctx.enterprise_id,
        name=body.provider.name,
        mode=body.provider.mode,
        api_base=str(body.provider.api_base).rstrip("/") if body.provider.api_base else None,
        encrypted_api_key=encrypted_key,
        status="active",
        version=(current.version + 1) if current else 1,
        effective_from=now,
        created_by=ctx.user_id,
        approved_by=ctx.user_id,
    )
    db.add(provider)
    db.flush()
    new_models: dict[str, AIModelProfile] = {}
    for definition in body.models:
        model = AIModelProfile(
            enterprise_id=ctx.enterprise_id,
            provider_id=provider.id,
            name=definition.name,
            model_name=definition.model_name,
            timeout_seconds=definition.timeout_seconds,
            max_retries=definition.max_retries,
            budget_cents=definition.budget_cents,
            status="active",
            effective_from=now,
            created_by=ctx.user_id,
            approved_by=ctx.user_id,
        )
        db.add(model)
        db.flush()
        new_models[definition.name] = model
    for definition in body.task_policies:
        db.add(
            AITaskPolicy(
                enterprise_id=ctx.enterprise_id,
                name=definition.task,
                task=definition.task,
                primary_model_id=new_models.get(definition.primary_model).id if definition.primary_model else None,
                fallback_model_id=new_models.get(definition.fallback_model).id if definition.fallback_model else None,
                enabled=definition.enabled,
                redaction_policy=definition.redaction_policy,
                status="active",
                effective_from=now,
                created_by=ctx.user_id,
                approved_by=ctx.user_id,
            )
        )
    _browser_audit(
        db,
        ctx,
        "replace",
        "llm_config",
        provider.id,
        {"mode": body.provider.mode, "model_count": len(body.models)},
    )
    db.commit()
    return get_llm_config(db, ctx)


def validate_llm_config(db: Session, ctx: RequestContext) -> dict[str, Any]:
    _require_role(ctx, ADMIN_ROLES, "只有管理员可以验证 LLM 绑定配置。")
    provider = _active_ai_provider(db, ctx.enterprise_id or "")
    if not provider or provider.mode == "disabled":
        return {
            "status": "disabled",
            "message": "LLM 未启用；确定性读取、对账和经营结果不受影响。",
            "deterministic_processing_available": True,
        }
    if not provider.api_base:
        return {
            "status": "invalid",
            "message": "尚未配置模型服务地址。",
            "deterministic_processing_available": True,
        }
    parsed = urlparse(provider.api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return {
            "status": "invalid",
            "message": "模型服务地址必须是无内嵌凭据的 HTTP 或 HTTPS 地址。",
            "deterministic_processing_available": True,
        }
    headers: dict[str, str] = {}
    if provider.encrypted_api_key:
        from ..security import decrypt_secret

        headers["Authorization"] = f"Bearer {decrypt_secret(provider.encrypted_api_key)}"
    try:
        response = httpx.get(
            urljoin(provider.api_base.rstrip("/") + "/", "health"),
            headers=headers,
            timeout=5.0,
            follow_redirects=False,
        )
        available = response.status_code < 400
        return {
            "status": "available" if available else "unavailable",
            "message": "模型网关可访问。" if available else "模型网关当前不可用。",
            "http_status": response.status_code,
            "deterministic_processing_available": True,
        }
    except httpx.HTTPError:
        return {
            "status": "unavailable",
            "message": "无法连接模型网关；确定性处理仍可继续。",
            "deterministic_processing_available": True,
        }
