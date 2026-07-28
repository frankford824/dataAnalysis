from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import RequestContext, get_context
from .agent_auth import AgentContext, get_agent_context
from .model_registry import ReconciliationModels, get_reconciliation_models
from .schemas import (
    AgentEventCreate,
    AgentHeartbeatRequest,
    AgentJobFailure,
    AgentJobResult,
    AgentRegisterRequest,
    ConnectorCreate,
    ConnectorUpdate,
    DiscoveredFileBatch,
    EnrollmentTokenCreate,
    HostAgentClaimRequest,
    HostAgentHeartbeatRequest,
    HostAgentProgressRequest,
    HostAgentRegisterRequest,
    LlmConfigUpdate,
    ReviewDecisionCreate,
    ScanRequest,
)
from .services import (
    _connector_response,
    _public_operation,
    append_job_event,
    archive_connector,
    claim_next_job,
    claim_review_item,
    create_connector,
    create_enrollment_token,
    decide_review_item,
    enqueue_scan,
    fail_job,
    fail_job_idempotently,
    finish_job,
    finish_job_idempotently,
    get_connector,
    get_llm_config,
    get_operation,
    heartbeat_agent,
    host_agent_job_response,
    list_connectors,
    list_operation_events,
    list_operations,
    list_review_items,
    register_agent,
    report_discovered_files,
    update_connector,
    update_llm_config,
    validate_llm_config,
    workbench_overview,
)
router = APIRouter(prefix="/api/v1", tags=["control-plane"])


def _bearer_value(authorization: str | None, purpose: str) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail=f"缺少{purpose}令牌。")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail=f"{purpose}令牌格式无效。")
    return value.strip()


@router.post("/agent-enrollment-tokens", tags=["external-agents"])
def issue_agent_enrollment_token(
    body: EnrollmentTokenCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return create_enrollment_token(db, ctx, models, body)


@router.post("/agent/register", tags=["external-agents"])
def agent_register(
    body: AgentRegisterRequest,
    db: Session = Depends(get_db),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return register_agent(db, models, body)


@router.post("/agents/register", tags=["external-agents"])
def host_agent_register(
    body: HostAgentRegisterRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    enrollment_token = _bearer_value(authorization, "注册")
    result = register_agent(
        db,
        models,
        AgentRegisterRequest(
            enrollment_token=enrollment_token,
            name=body.name,
            machine_name=body.name,
            capabilities=[
                *body.capabilities,
                f"connector:{body.connector}",
                f"protocol:{body.protocol_version}",
            ],
            agent_version=body.protocol_version,
        ),
    )
    return {
        "agent_id": result["agent"]["id"],
        "access_token": result["agent_secret"],
    }


@router.post("/agent/heartbeat", tags=["external-agents"])
def agent_heartbeat(
    body: AgentHeartbeatRequest,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return heartbeat_agent(db, models, agent, body)


@router.post("/agents/heartbeat", tags=["external-agents"])
def host_agent_heartbeat(
    body: HostAgentHeartbeatRequest,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    if body.agent_id and body.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="请求中的执行器编号与访问令牌不一致。")
    return heartbeat_agent(
        db,
        models,
        agent,
        AgentHeartbeatRequest(status=body.status),
    )


@router.post("/agent/jobs/claim", tags=["external-agents"])
def agent_claim_job(
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return {"job": claim_next_job(db, models, agent)}


@router.post("/agent-jobs/claim", tags=["external-agents"])
def host_agent_claim_job(
    body: HostAgentClaimRequest,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    if body.agent_id and body.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="请求中的执行器编号与访问令牌不一致。")
    job = claim_next_job(db, models, agent)
    if not job:
        return Response(status_code=204)
    return host_agent_job_response(job)


@router.post("/agent-jobs/{job_id}/progress", tags=["external-agents"])
def host_agent_progress(
    job_id: str,
    body: HostAgentProgressRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    progress_by_stage = {
        "claimed": 5,
        "scanning": 20,
        "materializing": 35,
        "profiling": 60,
        "recomputing": 70,
        "uploading": 85,
        "completed": 100,
        "failed": 100,
    }
    return append_job_event(
        db,
        models,
        agent,
        job_id,
        AgentEventCreate(
            external_event_id=idempotency_key or f"{job_id}:progress:{body.sequence}",
            event_type="started" if body.stage == "claimed" else "progress",
            stage=body.stage,
            progress=progress_by_stage[body.stage],
            message=body.message,
            details={
                **body.detail,
                "agent_sequence": body.sequence,
                "occurred_at": body.occurred_at.isoformat(),
            },
        ),
    )


@router.post("/agent-jobs/{job_id}/complete", tags=["external-agents"])
def host_agent_complete_job(
    job_id: str,
    body: AgentJobResult,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    job = finish_job_idempotently(
        db,
        models,
        agent,
        job_id,
        body,
        idempotency_key or f"{job_id}:complete",
    )
    return {"ok": True, "job": job}


@router.post("/agent-jobs/{job_id}/fail", tags=["external-agents"])
def host_agent_fail_job(
    job_id: str,
    body: AgentJobFailure,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    job = fail_job_idempotently(
        db,
        models,
        agent,
        job_id,
        body,
        idempotency_key or f"{job_id}:fail",
    )
    return {"ok": True, "job": job}


@router.post("/agent/jobs/{job_id}/events", tags=["external-agents"])
def agent_report_event(
    job_id: str,
    body: AgentEventCreate,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return append_job_event(db, models, agent, job_id, body)


@router.post("/agent/jobs/{job_id}/discovered-files", tags=["external-agents"])
def agent_report_files(
    job_id: str,
    body: DiscoveredFileBatch,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return report_discovered_files(db, models, agent, job_id, body)


@router.post("/agent/jobs/{job_id}/complete", tags=["external-agents"])
def agent_complete_job(
    job_id: str,
    body: AgentJobResult,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return finish_job(db, models, agent, job_id, body)


@router.post("/agent/jobs/{job_id}/fail", tags=["external-agents"])
def agent_fail_job(
    job_id: str,
    body: AgentJobFailure,
    db: Session = Depends(get_db),
    agent: AgentContext = Depends(get_agent_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return fail_job(db, models, agent, job_id, body)


@router.get("/workbench/overview", tags=["workbench"])
@router.get("/control/overview", tags=["workbench"])
def workbench(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return workbench_overview(db, ctx, models)


@router.get("/source-connectors", tags=["source-connectors"])
@router.get("/connectors", tags=["source-connectors"])
def connectors(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return list_connectors(db, ctx, models)


@router.post("/source-connectors", tags=["source-connectors"])
@router.post("/connectors", tags=["source-connectors"])
def add_connector(
    body: ConnectorCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return create_connector(db, ctx, models, body)


@router.get("/source-connectors/{connector_id}", tags=["source-connectors"])
@router.get("/connectors/{connector_id}", tags=["source-connectors"])
def connector_detail(
    connector_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return _connector_response(get_connector(db, ctx, models, connector_id))


@router.patch("/source-connectors/{connector_id}", tags=["source-connectors"])
@router.patch("/connectors/{connector_id}", tags=["source-connectors"])
def edit_connector(
    connector_id: str,
    body: ConnectorUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return update_connector(db, ctx, models, connector_id, body)


@router.delete("/source-connectors/{connector_id}", tags=["source-connectors"])
@router.delete("/connectors/{connector_id}", tags=["source-connectors"])
def remove_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return archive_connector(db, ctx, models, connector_id)


@router.post("/source-connectors/{connector_id}/scan", tags=["source-connectors"])
@router.post("/connectors/{connector_id}/scan", tags=["source-connectors"])
def scan_connector(
    connector_id: str,
    body: ScanRequest,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return enqueue_scan(db, ctx, models, connector_id, body)


@router.get("/operations/events/stream", tags=["operations"])
@router.get("/operations/stream", tags=["operations"])
async def operation_event_stream(
    request: Request,
    job_id: str | None = None,
    after_sequence: int = Query(default=0, ge=0),
    follow: bool = True,
    heartbeat_seconds: float = Query(default=5.0, ge=1.0, le=30.0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    async def generate() -> AsyncIterator[str]:
        emitted: set[str] = set()
        while True:
            db.expire_all()
            events = list_operation_events(db, ctx, models, job_id, after_sequence)
            for event in events:
                event_key = str(event.get("id") or f"{event.get('job_id')}:{event.get('sequence')}")
                if event_key in emitted:
                    continue
                emitted.add(event_key)
                yield f"id: {event_key}\nevent: operation\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if not follow:
                yield 'event: sync\ndata: {"status":"complete"}\n\n'
                return
            if await request.is_disconnected():
                return
            yield f": heartbeat {json.dumps({'server_time': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})}\n\n"
            await asyncio.sleep(heartbeat_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/operations", tags=["operations"])
def operations(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return list_operations(db, ctx, models, status=status, limit=limit)


@router.get("/operations/{job_id}", tags=["operations"])
def operation_detail(
    job_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    job = get_operation(db, ctx, models, job_id)
    return {
        "operation": _public_operation(job),
        "events": list_operation_events(db, ctx, models, job_id),
    }


@router.get("/operations/{job_id}/events", tags=["operations"])
def operation_events(
    job_id: str,
    after_sequence: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return list_operation_events(db, ctx, models, job_id, after_sequence)


@router.get("/review-items", tags=["review-inbox"])
def review_items(
    status: str = "open",
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return list_review_items(db, ctx, models, status=status)


@router.post("/review-items/{item_id}/claim", tags=["review-inbox"])
def claim_item(
    item_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return claim_review_item(db, ctx, models, item_id)


@router.post("/review-items/{item_id}/decide", tags=["review-inbox"])
def decide_item(
    item_id: str,
    body: ReviewDecisionCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
    models: ReconciliationModels = Depends(get_reconciliation_models),
):
    return decide_review_item(db, ctx, models, item_id, body)


@router.get("/llm-config", tags=["llm-config"])
@router.get("/llm/configuration", tags=["llm-config"])
def llm_config(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    return get_llm_config(db, ctx)


@router.put("/llm-config", tags=["llm-config"])
@router.put("/llm/configuration", tags=["llm-config"])
def replace_llm_config(
    body: LlmConfigUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    return update_llm_config(db, ctx, body)


@router.post("/llm-config/validate", tags=["llm-config"])
@router.post("/llm/configuration/validate", tags=["llm-config"])
def check_llm_config(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    return validate_llm_config(db, ctx)
