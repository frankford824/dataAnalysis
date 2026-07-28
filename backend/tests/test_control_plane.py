from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, create_engine, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.control_plane.model_registry import ReconciliationModels, get_reconciliation_models
from app.control_plane.router import router
from app.db import Base, get_db
from app.models import AIProvider, Enterprise, Store, UserAccount
from app.security import RequestContext, get_context


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalAgent(Base):
    __tablename__ = "cp_test_external_agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    machine_name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(Text)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AgentEnrollmentToken(Base):
    __tablename__ = "cp_test_agent_enrollment_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_agent_id: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class SourceConnector(Base):
    __tablename__ = "cp_test_source_connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36))
    store_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentJob(Base):
    __tablename__ = "cp_test_agent_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    connector_id: Mapped[str | None] = mapped_column(String(36))
    agent_id: Mapped[str | None] = mapped_column(String(36))
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AgentJobEvent(Base):
    __tablename__ = "cp_test_agent_job_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class DiscoveredFile(Base):
    __tablename__ = "cp_test_discovered_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(256), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ReviewItem(Base):
    __tablename__ = "cp_test_review_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(String(36))
    store_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), default="normal", nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ReviewDecision(Base):
    __tablename__ = "cp_test_review_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    enterprise_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    review_item_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    decision_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(36), nullable=False)
    decided_role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


MODEL_BUNDLE = ReconciliationModels(
    ExternalAgent=ExternalAgent,
    AgentEnrollmentToken=AgentEnrollmentToken,
    SourceConnector=SourceConnector,
    AgentJob=AgentJob,
    AgentJobEvent=AgentJobEvent,
    DiscoveredFile=DiscoveredFile,
    ReviewItem=ReviewItem,
    ReviewDecision=ReviewDecision,
)


@pytest.fixture
def control_plane():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    with testing_session() as db:
        for enterprise_id, name in [("enterprise-a", "甲企业"), ("enterprise-b", "乙企业")]:
            db.add(
                Enterprise(
                    id=enterprise_id,
                    name=name,
                    activation_at=_now(),
                    effective_from=_now(),
                    created_by="seed",
                )
            )
        for store_id, name in [("store-a", "甲店铺"), ("store-b", "乙店铺")]:
            db.add(
                Store(
                    id=store_id,
                    logical_id=store_id,
                    enterprise_id="enterprise-a",
                    name=name,
                    status="active",
                    version=1,
                    activation_at=_now(),
                    effective_from=_now(),
                    created_by="seed",
                )
            )
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def override_db():
        with testing_session() as db:
            yield db

    def override_context(request: Request):
        stores = request.headers.get("X-Test-Stores")
        return RequestContext(
            enterprise_id=request.headers.get("X-Test-Enterprise", "enterprise-a"),
            user_id=request.headers.get("X-Test-User", "admin-a"),
            role=request.headers.get("X-Test-Role", "admin"),
            store_ids=frozenset(stores.split(",")) if stores else None,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_context] = override_context
    app.dependency_overrides[get_reconciliation_models] = lambda: MODEL_BUNDLE
    with TestClient(app) as client:
        yield client, testing_session
    Base.metadata.drop_all(engine)


def _headers(
    *,
    enterprise: str = "enterprise-a",
    role: str = "admin",
    user: str = "admin-a",
    stores: str | None = None,
) -> dict[str, str]:
    result = {
        "X-Test-Enterprise": enterprise,
        "X-Test-Role": role,
        "X-Test-User": user,
    }
    if stores:
        result["X-Test-Stores"] = stores
    return result


def _register_agent(client: TestClient, headers: dict[str, str] | None = None):
    issued = client.post(
        "/api/v1/agent-enrollment-tokens",
        headers=headers or _headers(),
        json={"label": "finance-win", "expires_in_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["enrollment_token"]
    registered = client.post(
        "/api/v1/agent/register",
        json={
            "enrollment_token": token,
            "name": "财务主机执行器",
            "machine_name": "finance-win",
            "capabilities": ["filesystem_scan", "deterministic_reconciliation"],
            "agent_version": "0.1.0",
        },
    )
    assert registered.status_code == 200, registered.text
    payload = registered.json()
    return token, payload["agent"]["id"], payload["agent_secret"]


def _agent_headers(agent_id: str, secret: str) -> dict[str, str]:
    return {"X-Agent-ID": agent_id, "X-Agent-Secret": secret}


def test_enrollment_secret_is_one_time_hashed_and_admin_only(control_plane):
    client, session_factory = control_plane
    denied = client.post(
        "/api/v1/agent-enrollment-tokens",
        headers=_headers(role="implementer"),
        json={"label": "not-allowed"},
    )
    assert denied.status_code == 403

    token, agent_id, secret = _register_agent(client)
    second_use = client.post(
        "/api/v1/agent/register",
        json={
            "enrollment_token": token,
            "name": "重复注册",
            "machine_name": "finance-win",
        },
    )
    assert second_use.status_code == 401

    with session_factory() as db:
        stored_token = db.scalar(select(AgentEnrollmentToken))
        agent = db.get(ExternalAgent, agent_id)
        assert stored_token.token_hash != token
        assert agent.secret_hash != secret
        assert secret not in agent.secret_hash

    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers=_agent_headers(agent_id, secret),
        json={"status": "online", "diagnostics": {"queue_depth": 0}},
    )
    assert heartbeat.status_code == 200
    assert client.post(
        "/api/v1/agent/heartbeat",
        headers=_agent_headers(agent_id, "wrong-secret"),
        json={},
    ).status_code == 401


def test_agent_claim_events_discovery_and_completion_are_tenant_scoped(control_plane):
    client, session_factory = control_plane
    _, agent_id, secret = _register_agent(client)
    agent_headers = _agent_headers(agent_id, secret)

    connector = client.post(
        "/api/v1/source-connectors",
        headers=_headers(),
        json={
            "name": "finance-win 原始目录",
            "connector_type": "finance_win",
            "agent_id": agent_id,
            "store_ids": ["store-a"],
            "config": {"roots": [r"D:\只读数据"]},
        },
    )
    assert connector.status_code == 200, connector.text
    queued = client.post(
        f"/api/v1/source-connectors/{connector.json()['id']}/scan",
        headers=_headers(),
        json={"reason": "验收", "full_scan": False},
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job"]["id"]

    with session_factory() as db:
        db.add(
            AgentJob(
                enterprise_id="enterprise-b",
                job_type="source_scan",
                status="queued",
                stage="queued",
                progress=0,
                priority=100,
                payload={},
                created_by="admin-b",
            )
        )
        db.commit()

    claim = client.post("/api/v1/agent/jobs/claim", headers=agent_headers)
    assert claim.status_code == 200
    assert claim.json()["job"]["id"] == job_id
    assert claim.json()["job"]["enterprise_id"] == "enterprise-a"

    event_body = {
        "external_event_id": "event-001",
        "event_type": "progress",
        "stage": "inventory",
        "progress": 25,
        "message": "正在读取文件清单",
        "details": {"discovered": 1},
    }
    first_event = client.post(
        f"/api/v1/agent/jobs/{job_id}/events",
        headers=agent_headers,
        json=event_body,
    )
    duplicate_event = client.post(
        f"/api/v1/agent/jobs/{job_id}/events",
        headers=agent_headers,
        json=event_body,
    )
    assert first_event.status_code == duplicate_event.status_code == 200
    assert first_event.json()["id"] == duplicate_event.json()["id"]

    files = {
        "files": [
            {
                "fingerprint": "a" * 64,
                "source_path": r"D:\只读数据\订单.xlsx",
                "file_name": "订单.xlsx",
                "size_bytes": 1234,
                "modified_at": _now().isoformat(),
                "attributes": {"offline": False},
            }
        ]
    }
    reported = client.post(
        f"/api/v1/agent/jobs/{job_id}/discovered-files",
        headers=agent_headers,
        json=files,
    )
    repeated = client.post(
        f"/api/v1/agent/jobs/{job_id}/discovered-files",
        headers=agent_headers,
        json=files,
    )
    assert reported.json() == {"created": 1, "duplicates": 0}
    assert repeated.json() == {"created": 0, "duplicates": 1}

    completed = client.post(
        f"/api/v1/agent/jobs/{job_id}/complete",
        headers=agent_headers,
        json={"result": {"files": 1}, "message": "完成"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    with session_factory() as db:
        assert len(db.scalars(select(AgentJobEvent).where(AgentJobEvent.job_id == job_id)).all()) == 1
        assert len(db.scalars(select(DiscoveredFile).where(DiscoveredFile.job_id == job_id)).all()) == 1


def test_browser_scope_polling_sse_and_workbench_use_real_rows(control_plane):
    client, _ = control_plane
    _, agent_id, secret = _register_agent(client)
    connector_ids: dict[str, str] = {}
    for store in ["store-a", "store-b"]:
        created = client.post(
            "/api/v1/source-connectors",
            headers=_headers(),
            json={
                "name": f"{store} 数据",
                "connector_type": "filesystem",
                "agent_id": agent_id,
                "store_ids": [store],
                "config": {"roots": [rf"D:\{store}"]},
            },
        )
        connector_ids[store] = created.json()["id"]
        scan = client.post(
            f"/api/v1/source-connectors/{created.json()['id']}/scan",
            headers=_headers(),
            json={},
        )
        assert scan.status_code == 200

    restricted = _headers(role="analyst", user="analyst-a", stores="store-a")
    connectors = client.get("/api/v1/source-connectors", headers=restricted)
    assert connectors.status_code == 403
    unscoped = client.post(
        "/api/v1/source-connectors",
        headers=_headers(role="implementer", user="impl-a", stores="store-a"),
        json={
            "name": "越权企业来源",
            "connector_type": "filesystem",
            "agent_id": agent_id,
            "store_ids": [],
            "config": {"roots": [r"D:\all"]},
        },
    )
    assert unscoped.status_code == 403
    operations = client.get("/api/v1/operations", headers=restricted)
    assert len(operations.json()) == 1
    assert operations.json()[0]["payload"]["store_ids"] == ["store-a"]
    assert "connector_config" not in operations.json()[0]["payload"]
    overview = client.get("/api/v1/control/overview", headers=restricted)
    assert overview.status_code == 200
    assert overview.json()["pending_review_count"] is None
    assert overview.json()["can_review"] is False

    claimed = client.post("/api/v1/agent/jobs/claim", headers=_agent_headers(agent_id, secret)).json()["job"]
    client.post(
        f"/api/v1/agent/jobs/{claimed['id']}/events",
        headers=_agent_headers(agent_id, secret),
        json={
            "external_event_id": "stream-1",
            "event_type": "progress",
            "stage": "scan",
            "progress": 10,
            "message": "开始扫描",
        },
    )
    polling = client.get(
        f"/api/v1/operations/{claimed['id']}/events",
        headers=_headers(),
    )
    assert polling.status_code == 200
    assert polling.json()[0]["message"] == "开始扫描"
    stream = client.get(
        f"/api/v1/operations/stream?job_id={claimed['id']}&follow=false",
        headers=_headers(),
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: operation" in stream.text
    assert "开始扫描" in stream.text


def test_review_maker_checker_and_implementer_boundary(control_plane):
    client, session_factory = control_plane
    with session_factory() as db:
        item = ReviewItem(
            enterprise_id="enterprise-a",
            kind="large_difference",
            title="大额差异需要复核",
            risk_level="high",
            requires_approval=True,
            created_by="maker-admin",
            store_ids=["store-a"],
        )
        other_tenant = ReviewItem(
            enterprise_id="enterprise-b",
            kind="mapping",
            title="乙企业事项",
            created_by="admin-b",
        )
        db.add_all([item, other_tenant])
        db.commit()
        item_id = item.id

    implementer = _headers(role="implementer", user="impl-a")
    listing = client.get("/api/v1/review-items", headers=implementer)
    assert [row["id"] for row in listing.json()] == [item_id]
    assert client.post(
        f"/api/v1/review-items/{item_id}/claim",
        headers=implementer,
    ).status_code == 200
    blocked = client.post(
        f"/api/v1/review-items/{item_id}/decide",
        headers=implementer,
        json={"action": "confirm", "note": "确认没有问题"},
    )
    assert blocked.status_code == 403
    submitted = client.post(
        f"/api/v1/review-items/{item_id}/decide",
        headers=implementer,
        json={"action": "request_approval", "note": "请管理员复核"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["item"]["status"] == "pending_approval"

    self_approval = client.post(
        f"/api/v1/review-items/{item_id}/decide",
        headers=_headers(user="maker-admin"),
        json={"action": "confirm", "note": "自行批准"},
    )
    assert self_approval.status_code == 409
    approved = client.post(
        f"/api/v1/review-items/{item_id}/decide",
        headers=_headers(user="checker-admin"),
        json={"action": "confirm", "note": "复核通过"},
    )
    assert approved.status_code == 200
    assert approved.json()["item"]["status"] == "confirmed"


def test_llm_config_never_returns_key_and_implementer_cannot_manage(control_plane, monkeypatch):
    client, session_factory = control_plane
    implementer = _headers(role="implementer", user="impl-a")
    assert client.get("/api/v1/llm-config", headers=implementer).status_code == 403

    payload = {
        "provider": {
            "name": "本地 LiteLLM",
            "mode": "local",
            "api_base": "http://litellm:4000",
            "api_key": "super-secret-local-key",
        },
        "models": [
            {
                "name": "解释模型",
                "model_name": "local-explainer",
                "timeout_seconds": 20,
                "max_retries": 1,
            }
        ],
        "task_policies": [
            {
                "task": "difference_explanation",
                "primary_model": "解释模型",
                "enabled": True,
                "redaction_policy": {"sample_rows": 3},
            }
        ],
    }
    saved = client.put("/api/v1/llm/configuration", headers=_headers(), json=payload)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["provider"]["has_api_key"] is True
    assert "api_key" not in result["provider"]
    assert "super-secret-local-key" not in saved.text
    with session_factory() as db:
        provider = db.scalar(select(AIProvider).where(AIProvider.enterprise_id == "enterprise-a"))
        assert provider.encrypted_api_key != "super-secret-local-key"

    monkeypatch.setattr(
        "app.control_plane.services.httpx.get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )
    validated = client.post("/api/v1/llm/configuration/validate", headers=_headers())
    assert validated.status_code == 200
    assert validated.json()["status"] == "available"
    assert validated.json()["deterministic_processing_available"] is True


def test_expired_enrollment_and_cross_tenant_agent_assignment_are_rejected(control_plane):
    client, session_factory = control_plane
    with session_factory() as db:
        from app.control_plane.agent_auth import secret_hash

        db.add(
            AgentEnrollmentToken(
                enterprise_id="enterprise-a",
                label="expired",
                token_hash=secret_hash("x" * 40),
                expires_at=_now() - timedelta(minutes=1),
                created_by="admin-a",
            )
        )
        db.commit()
    expired = client.post(
        "/api/v1/agent/register",
        json={
            "enrollment_token": "x" * 40,
            "name": "过期代理",
            "machine_name": "old-host",
        },
    )
    assert expired.status_code == 401

    _, agent_id, _ = _register_agent(client, _headers(enterprise="enterprise-b", user="admin-b"))
    cross_tenant = client.post(
        "/api/v1/source-connectors",
        headers=_headers(),
        json={
            "name": "错误绑定",
            "connector_type": "filesystem",
            "agent_id": agent_id,
            "config": {},
        },
    )
    assert cross_tenant.status_code == 422
    unknown_store = client.post(
        "/api/v1/source-connectors",
        headers=_headers(),
        json={
            "name": "错误店铺范围",
            "connector_type": "filesystem",
            "store_ids": ["store-from-other-enterprise"],
            "config": {"roots": [r"D:\blocked"]},
        },
    )
    assert unknown_store.status_code in {403, 422}


def test_routes_match_the_real_reconciliation_model_contract():
    from app.reconciliation.models import (
        AgentJob as RealAgentJob,
        DiscoveredFile as RealDiscoveredFile,
        ExternalAgent as RealExternalAgent,
        ReviewItem as RealReviewItem,
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        db.add(
            Enterprise(
                id="real-enterprise",
                name="真实模型企业",
                activation_at=_now(),
                effective_from=_now(),
                created_by="seed",
            )
        )
        db.add(
            UserAccount(
                id="real-admin",
                enterprise_id="real-enterprise",
                name="真实管理员",
                email="real-admin@example.test",
                role="admin",
                store_ids=[],
                password_hash="not-used-in-isolated-control-plane-test",
                must_change_password=False,
                status="active",
                version=1,
                effective_from=_now(),
                created_by="seed",
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def override_db():
        with session_factory() as db:
            yield db

    def override_context(request: Request):
        return RequestContext(
            enterprise_id="real-enterprise",
            user_id=request.headers.get("X-Test-User", "real-admin"),
            role=request.headers.get("X-Test-Role", "admin"),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_context] = override_context
    with TestClient(app) as client:
        issued = client.post(
            "/api/v1/agent-enrollment-tokens",
            json={"label": "finance-win"},
        )
        assert issued.status_code == 200, issued.text
        registered = client.post(
            "/api/v1/agents/register",
            headers={"Authorization": f"Bearer {issued.json()['enrollment_token']}"},
            json={
                "name": "财务主机",
                "connector": "ssh_windows",
                "platform": "Windows-11",
                "capabilities": ["filesystem_scan"],
                "protocol_version": "1",
            },
        )
        assert registered.status_code == 200, registered.text
        assert "agent_key_hash" not in registered.text
        agent_id = registered.json()["agent_id"]
        agent_secret = registered.json()["access_token"]
        agent_headers = {"Authorization": f"Bearer {agent_secret}"}

        heartbeat = client.post(
            "/api/v1/agents/heartbeat",
            headers=agent_headers,
            json={"agent_id": agent_id, "status": "busy", "at": _now().isoformat()},
        )
        assert heartbeat.status_code == 200, heartbeat.text

        connector = client.post(
            "/api/v1/connectors",
            json={
                "name": "finance-win-orders",
                "connector_type": "finance_win",
                "agent_id": agent_id,
                "store_ids": [],
                "config": {
                    "roots": [r"D:\KAOSHI\OneDrive\内贸\宝贝报表"],
                    "purpose": "orders",
                    "skip_offline": True,
                },
            },
        )
        assert connector.status_code == 200, connector.text
        assert connector.json()["connector_type"] == "directory"
        connector_id = connector.json()["id"]
        queued = client.post(
            f"/api/v1/connectors/{connector_id}/scan",
            json={"reason": "真实字段契约"},
        )
        assert queued.status_code == 200, queued.text

        claimed = client.post(
            "/api/v1/agent-jobs/claim",
            headers=agent_headers,
            json={"agent_id": agent_id, "capabilities": ["scan"]},
        )
        assert claimed.status_code == 200, claimed.text
        job_id = claimed.json()["id"]
        assert claimed.json()["kind"] == "scan"
        event = client.post(
            f"/api/v1/agent-jobs/{job_id}/progress",
            headers={**agent_headers, "Idempotency-Key": f"{job_id}:progress:1"},
            json={
                "sequence": 1,
                "stage": "inventory",
                "message": "已读取目录清单",
                "detail": {},
                "occurred_at": _now().isoformat(),
            },
        )
        assert event.status_code == 422  # 未定义阶段不能被控制面静默接受
        progress_payload = {
            "sequence": 1,
            "stage": "scanning",
            "message": "已读取目录清单",
            "detail": {},
            "occurred_at": _now().isoformat(),
        }
        event = client.post(
            f"/api/v1/agent-jobs/{job_id}/progress",
            headers={**agent_headers, "Idempotency-Key": f"{job_id}:progress:1"},
            json=progress_payload,
        )
        duplicate_event = client.post(
            f"/api/v1/agent-jobs/{job_id}/progress",
            headers={**agent_headers, "Idempotency-Key": f"{job_id}:progress:1"},
            json=progress_payload,
        )
        assert event.status_code == duplicate_event.status_code == 200
        assert event.json()["id"] == duplicate_event.json()["id"]
        terminal_payload = {
            "result": {
                "files": [
                    {
                        "source_id": "f" * 64,
                        "path": r"D:\KAOSHI\OneDrive\内贸\宝贝报表\订单.xlsx",
                        "purpose": "orders",
                        "extension": ".xlsx",
                        "size": 2048,
                        "mtime_utc": _now().isoformat(),
                        "attributes": [],
                    }
                ],
                "count": 1,
            }
        }
        completed = client.post(
            f"/api/v1/agent-jobs/{job_id}/complete",
            headers={**agent_headers, "Idempotency-Key": f"{job_id}:complete"},
            json=terminal_payload,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["job"]["state"] == "succeeded"
        repeated_complete = client.post(
            f"/api/v1/agent-jobs/{job_id}/complete",
            headers={**agent_headers, "Idempotency-Key": f"{job_id}:complete"},
            json=terminal_payload,
        )
        assert repeated_complete.status_code == 200
        no_job = client.post(
            "/api/v1/agent-jobs/claim",
            headers=agent_headers,
            json={"agent_id": agent_id, "capabilities": ["scan"]},
        )
        assert no_job.status_code == 204
        second_scan = client.post(
            f"/api/v1/connectors/{connector_id}/scan",
            json={"reason": "失败幂等回归"},
        )
        assert second_scan.status_code == 200
        second_claim = client.post(
            "/api/v1/agent-jobs/claim",
            headers=agent_headers,
            json={"agent_id": agent_id, "capabilities": ["scan"]},
        )
        second_job_id = second_claim.json()["id"]
        failure_payload = {"error_code": "ReadOnlySourceOffline", "message": "主机暂时离线"}
        failed = client.post(
            f"/api/v1/agent-jobs/{second_job_id}/fail",
            headers={**agent_headers, "Idempotency-Key": f"{second_job_id}:fail"},
            json=failure_payload,
        )
        repeated_failure = client.post(
            f"/api/v1/agent-jobs/{second_job_id}/fail",
            headers={**agent_headers, "Idempotency-Key": f"{second_job_id}:fail"},
            json=failure_payload,
        )
        assert failed.status_code == repeated_failure.status_code == 200
        assert failed.json()["job"]["state"] == "failed"

        with session_factory() as db:
            db.add(
                RealReviewItem(
                    enterprise_id="real-enterprise",
                    subject_type="discovered_file",
                    subject_id=db.scalar(select(RealDiscoveredFile.id)),
                    status="pending",
                    risk_level="high",
                    requested_by="maker-admin",
                )
            )
            db.commit()
            review_id = db.scalar(select(RealReviewItem.id))

        implementer_headers = {"X-Test-Role": "implementer", "X-Test-User": "real-impl"}
        assert client.post(
            f"/api/v1/review-items/{review_id}/claim",
            headers=implementer_headers,
        ).status_code == 200
        escalated = client.post(
            f"/api/v1/review-items/{review_id}/decide",
            headers=implementer_headers,
            json={"action": "request_approval", "note": "大额事项请管理员复核"},
        )
        assert escalated.status_code == 200, escalated.text
        assert escalated.json()["item"]["status"] == "pending"
        approved = client.post(
            f"/api/v1/review-items/{review_id}/decide",
            headers={"X-Test-Role": "admin", "X-Test-User": "checker-admin"},
            json={"action": "confirm", "note": "复核通过"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["item"]["status"] == "decided"

    with session_factory() as db:
        assert db.scalar(select(RealExternalAgent)).status == "online"
        assert db.scalar(select(RealAgentJob)).state == "succeeded"
        assert db.scalar(select(RealDiscoveredFile)).full_path.endswith("订单.xlsx")
    Base.metadata.drop_all(engine)
