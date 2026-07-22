from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from threading import Lock
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from redis import Redis

from .audit import record_audit
from .config import get_settings
from .crud import RESOURCE_MODELS, create_resource, delete_resource, get_resource, list_resources, serialize, update_resource
from .db import Base, engine, get_db
from .ingestion import confirm_ingestion, create_ingestion, create_review_problem, lock_ingestion, publish_ingestion, recognize_source_candidates
from .models import AuditLog, AuthSession, CertifiedAggregate, DashboardAsset, Enterprise, IngestionRun, MetricDefinition, ModelAsset, PlatformAccount, Problem, ReviewQueueItem, SemanticModelVersion, SourceBinding, SourceDefinition, Store, UploadSession, UserAccount, utcnow
from .pbix import parse_pbix
from .query import execute_certified_query
from .schemas import BusinessConfirmation, CertifiedQuery, ConfigurationImport, EnterpriseCreate, LoginRequest, ManualPBIXMetadata, NaturalLanguageQuestion, PasswordChange, ProblemResolution, ResourceCreate, ResourcePatch, SetupComplete, UploadInitiate, UserInvite
from .security import APPROVE_ROLES, SESSION_COOKIE, RequestContext, WRITE_ROLES, create_session, get_context, hash_password, revoke_session, scoped_store_ids, verify_password
from .storage import get_storage


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auto_create_schema:
        Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="Commerce Analytics Platform API",
    version="0.1.0",
    description="Tenant-isolated deterministic ingestion and certified analytics API.",
    lifespan=lifespan,
)
SETUP_LOCK = Lock()
PUBLIC_PATHS = {"/health", "/ready", "/api/v1/setup", "/api/v1/setup/status", "/api/v1/setup/complete", "/api/v1/auth/login"}


def _openapi_schema():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}
    schemes["SessionCookie"] = {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE}
    for path, operations in schema.get("paths", {}).items():
        if path not in PUBLIC_PATHS:
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"BearerAuth": []}, {"SessionCookie": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_schema


def _setup_lock():
    with SETUP_LOCK:
        yield


@app.middleware("http")
async def reject_client_identity_headers(request: Request, call_next):
    forbidden = {"x-enterprise-id", "x-user-id", "x-role", "x-store-ids"}.intersection({key.lower() for key in request.headers})
    if forbidden:
        return JSONResponse(status_code=400, content={"detail": "client-supplied identity headers are not accepted"})
    return await call_next(request)


def _public_user(user: UserAccount, db: Session) -> dict:
    enterprise = db.get(Enterprise, user.enterprise_id)
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "enterprise_id": user.enterprise_id,
        "enterprise_name": enterprise.name if enterprise else None,
        "store_ids": user.store_ids,
        "must_change_password": user.must_change_password,
    }


def _set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="strict",
        expires=expires_at,
        path="/",
    )


@app.get("/api/v1/setup/status", tags=["authentication"])
@app.get("/api/v1/setup", tags=["authentication"])
def setup_status(db: Session = Depends(get_db)):
    return {"initialized": bool(db.scalar(select(UserAccount.id).limit(1)))}


@app.post("/api/v1/setup/complete", tags=["authentication"])
@app.post("/api/v1/setup", tags=["authentication"])
def setup_complete(body: SetupComplete, response: Response, db: Session = Depends(get_db), _lock: None = Depends(_setup_lock)):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(824761935)"))
    if db.scalar(select(UserAccount.id).limit(1)):
        raise HTTPException(status_code=409, detail="initial administrator already exists")
    now = utcnow()
    enterprise = db.scalar(select(Enterprise).where(Enterprise.name == body.enterprise_name))
    if enterprise is None:
        enterprise = Enterprise(name=body.enterprise_name, activation_at=body.activation_at, effective_from=body.activation_at, created_by="system-bootstrap")
        db.add(enterprise)
        db.flush()
    user = UserAccount(
        enterprise_id=enterprise.id,
        name=body.name,
        email=body.email.strip().lower(),
        role="platform_admin",
        store_ids=[],
        password_hash=hash_password(body.password),
        status="active",
        version=1,
        effective_from=now,
        created_by="system-bootstrap",
        approved_by="system-bootstrap",
    )
    db.add(user)
    db.flush()
    platform = PlatformAccount(enterprise_id=enterprise.id, name=body.platform_account_name, platform=body.platform, status="active", version=1, effective_from=body.activation_at, created_by=user.id, approved_by=user.id)
    db.add(platform)
    db.flush()
    store = Store(enterprise_id=enterprise.id, name=body.store_name, platform_account_id=platform.id, activation_at=body.activation_at, status="active", version=1, effective_from=body.activation_at, created_by=user.id, approved_by=user.id)
    db.add(store)
    source = SourceDefinition(
        enterprise_id=enterprise.id, name="标准订单文件", status="active", version=1,
        effective_from=body.activation_at, activation_at=body.activation_at,
        file_types=["csv", "xlsx", "zip"], recognition={"required_headers": ["order_id", "occurred_at"]},
        field_aliases={"order_id": ["订单号", "order"], "occurred_at": ["交易时间", "transaction_date"], "revenue": ["销售额", "sales"], "refund": ["退款额"], "platform_fee": ["平台费"], "advertising_fee": ["广告费"], "shipping_fee": ["运费"], "product_cost": ["商品成本"]},
        coverage_time_field="occurred_at", data_granularity="day", arrival_frequency="daily",
        dedupe_keys=["order_id"], validations=[{"type": "required_field", "field": "order_id"}], created_by=user.id, approved_by=user.id,
    )
    db.add(source)
    db.flush()
    db.add(SourceBinding(enterprise_id=enterprise.id, name="主店铺订单文件", source_definition_id=source.id, scope_type="store", scope_id=store.id, status="active", version=1, effective_from=body.activation_at, created_by=user.id, approved_by=user.id))
    model = SemanticModelVersion(enterprise_id=enterprise.id, name="电商标准经营模型", status="published", version=1, effective_from=body.activation_at, industry_template="ecommerce_standard", definition={"facts": ["sales", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "profit"], "dimensions": ["store", "date", "platform"]}, quality_gates=[{"key": "deterministic_reconciliation", "required": True}], created_by=user.id, approved_by=user.id)
    db.add(model)
    db.flush()
    expressions = {"sales": "sum(revenue)", "refund": "sum(refund)", "platform_fee": "sum(platform_fee)", "advertising_fee": "sum(advertising_fee)", "shipping_fee": "sum(shipping_fee)", "product_cost": "sum(product_cost)", "profit": "sum(revenue-refund-platform_fee-advertising_fee-shipping_fee-product_cost)"}
    for key, expression in expressions.items():
        db.add(MetricDefinition(enterprise_id=enterprise.id, semantic_model_id=model.id, name=key.replace("_", " ").title(), key=key, expression=expression, status="published", version=1, effective_from=body.activation_at, created_by=user.id, approved_by=user.id))
    dashboard = DashboardAsset(enterprise_id=enterprise.id, name="经营总览", status="published", version=1, effective_from=body.activation_at, created_by=user.id, approved_by=user.id, bi_adapter="builtin", definition={"template": "ecommerce_overview", "metrics": list(expressions)})
    db.add(dashboard)
    db.flush()
    record_audit(db, RequestContext(enterprise.id, user.id, user.role), "bootstrap", "users", user.id)
    token, session = create_session(db, user, commit=False)
    db.commit()
    _set_session_cookie(response, token, session.expires_at)
    return {"user": _public_user(user, db), "access_token": token, "expires_at": session.expires_at, "created": {"enterprise_id": enterprise.id, "platform_account_id": platform.id, "store_id": store.id, "source_definition_id": source.id, "semantic_model_id": model.id, "dashboard_id": dashboard.id}}


@app.post("/api/v1/auth/login", tags=["authentication"])
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    stmt = select(UserAccount).where(UserAccount.email == body.email.strip().lower(), UserAccount.archived_at.is_(None), UserAccount.status == "active")
    users = list(db.scalars(stmt.order_by(UserAccount.version.desc())).all())
    user = users[0] if users else None
    if not user or not verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token, session = create_session(db, user)
    _set_session_cookie(response, token, session.expires_at)
    return {"user": _public_user(user, db), "access_token": token, "token_type": "bearer", "expires_at": session.expires_at}


@app.get("/api/v1/auth/me", tags=["authentication"])
def auth_me(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    user = db.get(UserAccount, ctx.user_id)
    return _public_user(user, db)


@app.post("/api/v1/auth/logout", tags=["authentication"])
def logout(response: Response, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    revoke_session(db, ctx.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}


@app.post("/api/v1/auth/change-password", tags=["authentication"])
def change_password(body: PasswordChange, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    user = db.get(UserAccount, ctx.user_id)
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    db.execute(text("UPDATE auth_sessions SET revoked_at = :now WHERE user_id = :user_id AND id <> :session_id AND revoked_at IS NULL"), {"now": utcnow(), "user_id": user.id, "session_id": ctx.session_id or ""})
    db.commit()
    return {"changed": True}


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api"}


@app.get("/ready", tags=["operations"])
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        storage = get_storage()
        storage.put("_health/probe", b"ok", "text/plain")
        if storage.get("_health/probe") != b"ok":
            raise RuntimeError("object storage readback failed")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"dependency not ready: {type(exc).__name__}") from exc
    return {"status": "ready", "database": "ok", "object_storage": "ok"}


@app.post("/api/v1/enterprises", tags=["enterprises"])
def create_enterprise(body: EnterpriseCreate, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require({"platform_admin"})
    enterprise = Enterprise(name=body.name, activation_at=body.activation_at, effective_from=body.activation_at, created_by=ctx.user_id)
    db.add(enterprise)
    try:
        db.flush()
        record_audit(db, RequestContext(enterprise.id, ctx.user_id, ctx.role), "create", "enterprises", enterprise.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="enterprise name already exists") from exc
    db.refresh(enterprise)
    return serialize(enterprise)


@app.get("/api/v1/enterprises", tags=["enterprises"])
def list_enterprises(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    stmt = select(Enterprise).order_by(Enterprise.created_at)
    if ctx.role != "platform_admin":
        stmt = stmt.where(Enterprise.id == ctx.enterprise_id)
    return [serialize(value) for value in db.scalars(stmt).all()]


@app.get("/api/v1/audit-logs", tags=["audit"])
def audit_logs(limit: int = Query(default=100, ge=1, le=1000), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    ctx.require({"platform_admin", "admin", "implementer"})
    items = db.scalars(select(AuditLog).where(AuditLog.enterprise_id == ctx.enterprise_id).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [serialize(item) for item in items]


@app.post("/api/v1/users/invite", tags=["users"])
def invite_user(body: UserInvite, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require({"platform_admin", "admin"})
    scoped_store_ids(db, ctx, body.store_ids)
    temporary = body.password or f"Tmp-{secrets.token_urlsafe(12)}"
    created = create_resource(
        db,
        ctx,
        "users",
        {"name": body.name, "email": body.email, "role": body.role, "store_ids": body.store_ids, "password": temporary, "status": "active"},
    )
    user = db.get(UserAccount, created["id"])
    user.must_change_password = body.password is None
    db.commit()
    return {"user": serialize(user), "temporary_password": temporary if body.password is None else None}


@app.get("/api/v1/problems", tags=["review-queue"])
@app.get("/api/v1/issues", tags=["review-queue"])
def list_problems(status: str = Query(default="open"), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require({"platform_admin", "admin", "implementer"})
    stmt = select(Problem).where(Problem.enterprise_id == ctx.enterprise_id)
    if status != "all":
        stmt = stmt.where(Problem.status == status)
    return [serialize(item) for item in db.scalars(stmt.order_by(Problem.created_at.desc())).all()]


@app.post("/api/v1/problems/{problem_id}/resolve", tags=["review-queue"])
@app.patch("/api/v1/issues/{problem_id}", tags=["review-queue"])
def resolve_problem(problem_id: str, body: ProblemResolution, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require({"platform_admin", "admin", "implementer"})
    problem = db.scalar(select(Problem).where(Problem.id == problem_id, Problem.enterprise_id == ctx.enterprise_id))
    if not problem:
        raise HTTPException(status_code=404, detail="problem not found")
    if body.source_definition_id and not db.scalar(select(SourceDefinition.id).where(SourceDefinition.id == body.source_definition_id, SourceDefinition.enterprise_id == ctx.enterprise_id)):
        raise HTTPException(status_code=422, detail="source definition not found")
    problem.resolution = {"action": body.action, "note": body.resolution, "source_definition_id": body.source_definition_id, "field_mapping": body.field_mapping}
    if body.action in {"resolve", "retry_with_mapping"}:
        if body.action == "resolve" and not body.source_definition_id:
            raise HTTPException(status_code=422, detail="resolve requires source_definition_id")
    if body.action == "retry_with_mapping":
        if not body.source_definition_id or not body.field_mapping:
            raise HTTPException(status_code=422, detail="retry_with_mapping requires source_definition_id and field_mapping")
        source = db.scalar(select(SourceDefinition).where(SourceDefinition.id == body.source_definition_id, SourceDefinition.enterprise_id == ctx.enterprise_id))
        if not source:
            raise HTTPException(status_code=422, detail="source definition not found")
        updated_aliases = dict(source.field_aliases or {})
        for canonical, uploaded_field in body.field_mapping.items():
            if canonical not in {"order_id", "occurred_at", "store_id", "revenue", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost"}:
                raise HTTPException(status_code=422, detail=f"unsupported canonical field: {canonical}")
            updated_aliases[canonical] = [uploaded_field]
        updated_source = update_resource(db, ctx, "sources", source.id, {"field_aliases": updated_aliases})
        saved_resolution = dict(problem.resolution)
        saved_resolution["source_definition_id"] = updated_source["id"]
        problem.resolution = saved_resolution
        problem.status = "awaiting_configuration_approval" if updated_source["status"] == "draft" else "awaiting_reupload"
    elif body.action == "resolve":
        if not db.scalar(select(SourceDefinition.id).where(SourceDefinition.id == body.source_definition_id, SourceDefinition.enterprise_id == ctx.enterprise_id)):
            raise HTTPException(status_code=422, detail="source definition not found")
        problem.status = "awaiting_reupload"
    else:
        problem.status = "rejected"
        problem.resolved_by = ctx.user_id
        problem.resolved_at = utcnow()
    queue = db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.problem_id == problem.id))
    if queue:
        queue.status = problem.status
    record_audit(db, ctx, "resolve", "problem", problem.id, problem.resolution)
    db.commit()
    return serialize(problem)


@app.get("/api/v1/health/diagnostics", tags=["operations"])
def diagnostics(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require({"platform_admin", "admin", "implementer"})
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
    try:
        storage = get_storage()
        storage.put("_health/diagnostic", b"ok", "text/plain")
        checks["object_storage"] = "ok" if storage.get("_health/diagnostic") == b"ok" else "failed"
    except Exception:
        checks["object_storage"] = "failed"
    try:
        checks["queue"] = "ok" if Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1).ping() else "failed"
    except Exception:
        checks["queue"] = "failed"
    if ctx.enterprise_id:
        record_audit(db, ctx, "diagnose", "system", None)
        db.commit()
    return {"status": "healthy" if all(value == "ok" for value in checks.values()) else "degraded", "checks": checks, "ai_required": False}


@app.get("/api/v1/configuration/export", tags=["configuration"])
def export_configuration(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    ctx.require({"platform_admin", "admin", "implementer"})
    resources = {name: list_resources(db, ctx, name, include_archived=True) for name in RESOURCE_MODELS if name != "users"}
    # serialize() replaces encrypted_api_key with has_api_key; exports therefore never contain credentials.
    result = {"schema_version": 1, "enterprise_id": ctx.enterprise_id, "exported_at": datetime.now().astimezone().isoformat(), "resources": resources}
    record_audit(db, ctx, "export", "configuration", None, {"counts": {key: len(value) for key, value in resources.items()}})
    db.commit()
    return result


@app.post("/api/v1/configuration/import", tags=["configuration"])
def import_configuration(body: ConfigurationImport, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    ctx.require({"platform_admin", "admin", "implementer"})
    unknown = set(body.resources) - set(RESOURCE_MODELS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown resource groups: {', '.join(sorted(unknown))}")
    if "users" in body.resources:
        raise HTTPException(status_code=422, detail="user accounts are not configuration data; use the invitation API")
    counts = {resource: len(items) for resource, items in body.resources.items()}
    id_map: dict[str, str] = {}
    imported: dict[str, int] = {}
    reference_fields = {"platform_account_id", "business_entity_id", "source_definition_id", "model_asset_id", "semantic_model_id", "provider_id", "primary_model_id", "fallback_model_id", "scope_id"}
    ordered = ["business-entities", "platforms", "stores", "sources", "source-bindings", "source-schedules", "model-assets", "model-scope-bindings", "semantic-models", "metrics", "dashboards", "ai/providers", "ai/models", "ai/task-policies"]
    safe_fields = set(ResourceCreate.model_fields)
    savepoint = db.begin_nested()
    try:
        for resource in ordered:
            for raw in body.resources.get(resource, []):
                payload = {key: value for key, value in raw.items() if key in safe_fields and key not in {"api_key"}}
                for field in reference_fields:
                    if payload.get(field) in id_map:
                        payload[field] = id_map[payload[field]]
                if resource in {"source-bindings", "model-scope-bindings"} and payload.get("scope_type") == "enterprise":
                    payload["scope_id"] = ctx.enterprise_id
                # Pydantic validation catches invalid enum/value shapes before any durable write.
                validated = ResourceCreate.model_validate(payload).model_dump()
                old_id = raw.get("id")
                created = create_resource(db, ctx, resource, validated, commit=False)
                if old_id:
                    id_map[str(old_id)] = created["id"]
                imported[resource] = imported.get(resource, 0) + 1
        db.flush()
        if body.dry_run:
            savepoint.rollback()
            db.rollback()
            return {"dry_run": True, "valid": True, "counts": counts}
        savepoint.commit()
        record_audit(db, ctx, "import", "configuration", None, {"counts": imported})
        db.commit()
        return {"dry_run": False, "imported": imported}
    except ValidationError as exc:
        if savepoint.is_active:
            savepoint.rollback()
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    except Exception:
        if savepoint.is_active:
            savepoint.rollback()
        db.rollback()
        raise


@app.get("/api/v1/exports/certified", tags=["analytics"])
def export_certified(
    format: str = Query(default="csv", pattern="^(csv|xlsx|json)$"),
    store_id: list[str] = Query(default=[]),
    platform_id: str | None = Query(default=None),
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    permitted = scoped_store_ids(db, ctx, store_id or None)
    if platform_id:
        platform_stores = set(db.scalars(select(Store.id).where(Store.enterprise_id == ctx.enterprise_id, Store.platform_account_id == platform_id)).all())
        if not platform_stores:
            raise HTTPException(status_code=422, detail="platform has no stores in this enterprise")
        permitted &= platform_stores
    if period:
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        date_from, date_to = start, end
    if date_from and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id)
    if ctx.store_ids is not None or store_id or platform_id:
        stmt = stmt.where(CertifiedAggregate.store_id.in_(permitted))
    if date_from:
        stmt = stmt.where(CertifiedAggregate.period_start >= date_from)
    if date_to:
        stmt = stmt.where(CertifiedAggregate.period_start < date_to)
    rows = [serialize(item) for item in db.scalars(stmt.order_by(CertifiedAggregate.period_start)).all()]
    record_audit(db, ctx, "export", "certified-data", None, {"format": format, "row_count": len(rows)})
    db.commit()
    columns = ["store_id", "period_start", "grain", "row_count", "order_count", "revenue", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "fees", "profit"]
    if format == "json":
        return {"rows": rows}
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=certified-data.csv"})
    import xlsxwriter
    binary = io.BytesIO()
    workbook = xlsxwriter.Workbook(binary, {"in_memory": True})
    sheet = workbook.add_worksheet("Certified data")
    for col, name in enumerate(columns):
        sheet.write(0, col, name)
    for row_number, row in enumerate(rows, start=1):
        for col, name in enumerate(columns):
            value = row.get(name)
            if name in {"revenue", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "fees", "profit"} and value is not None:
                sheet.write_number(row_number, col, float(value))
            else:
                sheet.write(row_number, col, str(value) if value is not None else "")
    workbook.close()
    binary.seek(0)
    return StreamingResponse(binary, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=certified-data.xlsx"})


def _tenant_ingestion(db: Session, ctx: RequestContext, run_id: str) -> IngestionRun:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    run = db.scalar(select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.enterprise_id == ctx.enterprise_id))
    if not run:
        raise HTTPException(status_code=404, detail="ingestion not found")
    run_stores = set(run.summary.get("store_ids", [])) | ({run.store_id} if run.store_id else set())
    if ctx.store_ids is not None and not run_stores.issubset(scoped_store_ids(db, ctx)):
        raise HTTPException(status_code=404, detail="ingestion not found")
    return run


@app.get("/api/v1/ingestions", tags=["ingestions"])
def list_ingestions(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    items = list(db.scalars(select(IngestionRun).where(IngestionRun.enterprise_id == ctx.enterprise_id).order_by(IngestionRun.created_at.desc())).all())
    if ctx.store_ids is not None:
        permitted = scoped_store_ids(db, ctx)
        items = [item for item in items if (set(item.summary.get("store_ids", [])) | ({item.store_id} if item.store_id else set())).issubset(permitted)]
    return [serialize(item) for item in items]


@app.get("/api/v1/ingestions/{run_id}", tags=["ingestions"])
def get_ingestion(run_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    return serialize(_tenant_ingestion(db, ctx, run_id))


def _choose_source(db: Session, ctx: RequestContext, filename: str, data: bytes, store_id: str | None, requested_source_id: str | None) -> str:
    if requested_source_id:
        source = db.scalar(select(SourceDefinition).where(SourceDefinition.id == requested_source_id, SourceDefinition.enterprise_id == ctx.enterprise_id, SourceDefinition.archived_at.is_(None)))
        if not source:
            raise HTTPException(status_code=404, detail="source definition not found")
        return source.id
    try:
        matches = recognize_source_candidates(db, ctx, filename, data, store_id)
    except HTTPException as exc:
        problem = create_review_problem(db, ctx, "file_structure", "文件结构无法读取，已转交实施人员处理", {"filename": Path(filename).name, "reason": str(exc.detail)})
        raise HTTPException(status_code=409, detail={"code": "file_structure", "message": problem.user_message, "problem_id": problem.id, "options": []}) from exc
    if len(matches) == 1:
        return matches[0].id
    kind = "source_not_recognized" if not matches else "source_ambiguous"
    message = "无法自动识别这份文件，请选择文件类型" if not matches else "这份文件可能属于多种类型，请选择正确类型"
    choices = matches or list(db.scalars(select(SourceDefinition).where(SourceDefinition.enterprise_id == ctx.enterprise_id, SourceDefinition.archived_at.is_(None)).order_by(SourceDefinition.name).limit(3)).all())
    options = [{"id": source.id, "label": source.name} for source in choices[:3]]
    problem = create_review_problem(db, ctx, kind, message, {"filename": Path(filename).name, "sha256": hashlib.sha256(data).hexdigest(), "candidate_ids": [item.id for item in matches]})
    raise HTTPException(status_code=409, detail={"code": kind, "message": message, "problem_id": problem.id, "options": options})


@app.post("/api/v1/ingestions/upload", tags=["ingestions"])
async def upload_ingestion(source_definition_id: str | None = Form(default=None), store_id: str | None = Form(default=None), backfill: bool = Form(default=False), file: UploadFile = File(...), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail="file exceeds the configured upload limit")
    if store_id:
        scoped_store_ids(db, ctx, [store_id])
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(IngestionRun).where(IngestionRun.enterprise_id == ctx.enterprise_id, IngestionRun.source_sha256 == digest))
    if existing:
        return {**serialize(existing), "deduplicated": True, "duplicate_of_run_id": existing.id}
    selected_source = _choose_source(db, ctx, file.filename or "upload", data, store_id, source_definition_id)
    try:
        run = create_ingestion(db, get_storage(), ctx, selected_source, file.filename or "upload", data, store_id, backfill)
    except HTTPException as exc:
        if exc.status_code == 422:
            create_review_problem(db, ctx, "file_validation", "文件内容需要实施人员协助处理", {"filename": Path(file.filename or "upload").name, "reason": str(exc.detail)})
        raise
    if run.status == "quality_failed":
        create_review_problem(db, ctx, "quality_gate", "文件校验未通过，已转交实施人员处理", {"failed_checks": [item["key"] for item in run.quality_result.get("checks", []) if item.get("status") == "failed"]}, run.id)
    return {**serialize(run), "deduplicated": False}


def _tenant_upload(db: Session, ctx: RequestContext, upload_id: str) -> UploadSession:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    upload = db.scalar(select(UploadSession).where(UploadSession.id == upload_id, UploadSession.enterprise_id == ctx.enterprise_id))
    if not upload:
        raise HTTPException(status_code=404, detail="upload session not found")
    return upload


@app.post("/api/v1/ingestions/upload/initiate", tags=["ingestions"])
def initiate_upload(body: UploadInitiate, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    if body.size > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail="file exceeds the configured upload limit")
    if (body.size + body.part_size - 1) // body.part_size > 400:
        raise HTTPException(status_code=422, detail="upload would require too many parts")
    if body.source_definition_id and not db.scalar(select(SourceDefinition.id).where(SourceDefinition.id == body.source_definition_id, SourceDefinition.enterprise_id == ctx.enterprise_id)):
        raise HTTPException(status_code=404, detail="source definition not found")
    if body.store_id:
        scoped_store_ids(db, ctx, [body.store_id])
    existing = db.scalar(select(IngestionRun).where(IngestionRun.enterprise_id == ctx.enterprise_id, IngestionRun.source_sha256 == body.sha256.lower()))
    if existing:
        return {"deduplicated": True, "ingestion": serialize(existing)}
    upload = UploadSession(enterprise_id=ctx.enterprise_id, source_definition_id=body.source_definition_id, store_id=body.store_id, filename=Path(body.filename).name, expected_sha256=body.sha256.lower(), expected_size=body.size, part_size=body.part_size, is_backfill=body.backfill, created_by=ctx.user_id)
    db.add(upload)
    db.flush()
    record_audit(db, ctx, "initiate_upload", "upload-session", upload.id, {"size": body.size})
    db.commit()
    db.refresh(upload)
    return {"deduplicated": False, "upload_id": upload.id, "part_size": upload.part_size, "received_parts": upload.received_parts}


@app.put("/api/v1/ingestions/upload/{upload_id}/parts/{part_number}", tags=["ingestions"])
async def upload_part(upload_id: str, part_number: int, request: Request, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    if part_number < 1 or part_number > 400:
        raise HTTPException(status_code=422, detail="invalid part number")
    upload = _tenant_upload(db, ctx, upload_id)
    if upload.status != "uploading":
        raise HTTPException(status_code=409, detail="upload is not active")
    data = await request.body()
    if not data or len(data) > upload.part_size:
        raise HTTPException(status_code=422, detail="part is empty or exceeds negotiated part size")
    get_storage().put(f"{ctx.enterprise_id}/uploads/{upload.id}/parts/{part_number:08d}", data)
    upload.received_parts = sorted(set(upload.received_parts + [part_number]))
    db.commit()
    return {"upload_id": upload.id, "part_number": part_number, "size": len(data), "received_parts": upload.received_parts}


@app.post("/api/v1/ingestions/upload/{upload_id}/complete", tags=["ingestions"])
def complete_upload(upload_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    upload = _tenant_upload(db, ctx, upload_id)
    if upload.status == "completed":
        existing = db.scalar(select(IngestionRun).where(IngestionRun.enterprise_id == upload.enterprise_id, IngestionRun.source_sha256 == upload.expected_sha256))
        if existing:
            return serialize(existing)
    if upload.status != "uploading":
        raise HTTPException(status_code=409, detail="upload is not active")
    expected_parts = (upload.expected_size + upload.part_size - 1) // upload.part_size
    if upload.received_parts != list(range(1, expected_parts + 1)):
        raise HTTPException(status_code=409, detail="upload has missing parts")
    storage = get_storage()
    data = b"".join(storage.get(f"{ctx.enterprise_id}/uploads/{upload.id}/parts/{number:08d}") for number in upload.received_parts)
    if len(data) != upload.expected_size or hashlib.sha256(data).hexdigest() != upload.expected_sha256:
        raise HTTPException(status_code=422, detail="completed upload failed size or SHA-256 validation")
    selected_source = _choose_source(db, ctx, upload.filename, data, upload.store_id, upload.source_definition_id)
    run = create_ingestion(db, storage, ctx, selected_source, upload.filename, data, upload.store_id, upload.is_backfill)
    upload.status = "completed"
    record_audit(db, ctx, "complete_upload", "upload-session", upload.id, {"ingestion_id": run.id})
    db.commit()
    return serialize(run)


@app.post("/api/v1/ingestions/{run_id}/confirm", tags=["ingestions"])
def confirm_run(run_id: str, body: BusinessConfirmation, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    return serialize(confirm_ingestion(db, ctx, _tenant_ingestion(db, ctx, run_id), body.accepted, body.note))


@app.post("/api/v1/ingestions/{run_id}/publish", tags=["ingestions"])
def publish_run(run_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(APPROVE_ROLES)
    return serialize(publish_ingestion(db, get_storage(), ctx, _tenant_ingestion(db, ctx, run_id)))


@app.post("/api/v1/ingestions/{run_id}/lock", tags=["ingestions"])
def lock_run(run_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(APPROVE_ROLES)
    return serialize(lock_ingestion(db, ctx, _tenant_ingestion(db, ctx, run_id)))


@app.post("/api/v1/model-assets/pbix", tags=["model-assets"])
async def upload_pbix(name: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    data = await file.read()
    if Path(file.filename or "").suffix.lower() != ".pbix":
        raise HTTPException(status_code=422, detail="a .pbix file is required")
    digest = hashlib.sha256(data).hexdigest()
    key = f"{ctx.enterprise_id}/models/{digest}/{Path(file.filename or 'model.pbix').name}"
    get_storage().put(key, data, "application/octet-stream")
    validation_status, metadata, message = parse_pbix(data)
    asset = ModelAsset(enterprise_id=ctx.enterprise_id, name=name, status="draft", version=1, created_by=ctx.user_id, asset_type="pbix", object_key=key, sha256=digest, metadata_payload=metadata, validation_status=validation_status, parser_message=message)
    db.add(asset)
    db.flush()
    record_audit(db, ctx, "upload_pbix", "model-assets", asset.id, {"sha256": digest, "validation_status": validation_status})
    db.commit()
    db.refresh(asset)
    return serialize(asset)


@app.post("/api/v1/model-assets/{asset_id}/manual-metadata", tags=["model-assets"])
def register_pbix_metadata(asset_id: str, body: ManualPBIXMetadata, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    asset = get_resource(db, ctx, "model-assets", asset_id)
    if asset.asset_type != "pbix":
        raise HTTPException(status_code=409, detail="manual PBIX metadata is only valid for PBIX assets")
    asset.metadata_payload = body.model_dump()
    asset.validation_status = "manually_registered"
    record_audit(db, ctx, "register_manual_metadata", "model-assets", asset.id)
    db.commit()
    db.refresh(asset)
    return serialize(asset)


@app.post("/api/v1/certified-query", tags=["analytics"])
def certified_query(body: CertifiedQuery, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    if db.get(Enterprise, ctx.enterprise_id) is None:
        raise HTTPException(status_code=404, detail="enterprise not found")
    store_scope = scoped_store_ids(db, ctx) if ctx.store_ids is not None else None
    result = execute_certified_query(db, ctx.enterprise_id, body.sql, store_scope)
    record_audit(db, ctx, "query", "certified-query", None, {"sql": body.sql, "row_count": result["row_count"]})
    db.commit()
    return result


@app.post("/api/v1/business-questions", tags=["analytics"])
def business_question(body: NaturalLanguageQuestion, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    if db.get(Enterprise, ctx.enterprise_id) is None:
        raise HTTPException(status_code=404, detail="enterprise not found")
    allowed_stores = scoped_store_ids(db, ctx, body.store_ids or None)
    if body.platform_id:
        platform_stores = set(db.scalars(select(Store.id).where(Store.enterprise_id == ctx.enterprise_id, Store.platform_account_id == body.platform_id)).all())
        if not platform_stores:
            raise HTTPException(status_code=422, detail="platform has no stores in this enterprise")
        allowed_stores &= platform_stores
    question = body.question.lower()
    metric = ({"sales": "revenue", "refund": "refund", "fees": "fees", "profit": "profit", "ranking": "profit", "month_comparison": "revenue"}.get(body.question_type or "")
              or ("profit" if any(word in question for word in ["profit", "利润"]) else "refund" if any(word in question for word in ["refund", "退款"]) else "fees" if any(word in question for word in ["fee", "费用", "费率"]) else "revenue"))
    stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id)
    if ctx.store_ids is not None or body.store_ids or body.platform_id:
        stmt = stmt.where(CertifiedAggregate.store_id.in_(allowed_stores))
    if body.date_from:
        stmt = stmt.where(CertifiedAggregate.period_start >= body.date_from)
    if body.date_to:
        stmt = stmt.where(CertifiedAggregate.period_start < body.date_to)
    rows = db.scalars(stmt).all()
    revenue = sum((row.revenue for row in rows), start=Decimal(0))
    refund = sum((row.refund for row in rows), start=Decimal(0))
    profit = sum((row.profit for row in rows), start=Decimal(0))
    total = sum((getattr(row, metric) for row in rows), start=Decimal(0))
    metric_labels = {"revenue": "销售额", "refund": "退款额", "fees": "费用", "profit": "利润"}
    result: dict = {"answer": f"{metric_labels[metric]}合计为 {total}", "metric": metric, "value": str(total), "currency": "企业配置币种", "context": {"stores": body.store_ids, "date_from": body.date_from, "date_to": body.date_to}, "options": ["按店铺查看", "按月份查看", "导出结果"], "ai_used": False}
    if body.question_type == "refund_rate" or any(word in question for word in ["退款率", "refund rate"]):
        value = (refund / revenue * 100) if revenue else Decimal(0)
        result.update(metric="refund_rate", value=str(value.quantize(Decimal("0.01"))), unit="percent", answer=f"退款率为 {value.quantize(Decimal('0.01'))}%")
    elif body.question_type == "profit_margin" or any(word in question for word in ["利润率", "margin"]):
        value = (profit / revenue * 100) if revenue else Decimal(0)
        result.update(metric="profit_margin", value=str(value.quantize(Decimal("0.01"))), unit="percent", answer=f"利润率为 {value.quantize(Decimal('0.01'))}%")
    elif body.question_type == "ranking" or any(word in question for word in ["排名", "ranking", "top store"]):
        grouped: dict[str, Decimal] = {}
        for row in rows:
            grouped[row.store_id or "unassigned"] = grouped.get(row.store_id or "unassigned", Decimal(0)) + getattr(row, metric)
        store_names = {store.id: store.name for store in db.scalars(select(Store).where(Store.enterprise_id == ctx.enterprise_id)).all()}
        ranking = [{"store_id": key, "store_name": store_names.get(key, "未分配店铺"), "value": str(value)} for key, value in sorted(grouped.items(), key=lambda item: item[1], reverse=True)]
        ranking_text = "；".join(f"第{index}名 {item['store_name']} {item['value']}" for index, item in enumerate(ranking[:3], start=1))
        result.update(metric=f"{metric}_by_store", ranking=ranking, answer=f"按{metric_labels[metric]}排名：{ranking_text}" if ranking_text else "当前范围没有可排名的已发布数据")
    if body.question_type == "month_comparison" or any(word in question for word in ["上月", "last month", "环比"]):
        anchor = body.date_to - timedelta(microseconds=1) if body.date_to else utcnow()
        current_start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_end = current_start
        previous_start = (current_start - timedelta(days=1)).replace(day=1)
        previous_stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id, CertifiedAggregate.period_start >= previous_start, CertifiedAggregate.period_start < previous_end)
        if ctx.store_ids is not None or body.store_ids or body.platform_id:
            previous_stmt = previous_stmt.where(CertifiedAggregate.store_id.in_(allowed_stores))
        previous = sum((getattr(row, metric) for row in db.scalars(previous_stmt).all()), start=Decimal(0))
        change = total - previous
        change_rate = ((change / previous) * 100).quantize(Decimal("0.01")) if previous else None
        result["comparison"] = {"previous_month": str(previous), "change": str(change), "change_rate": str(change_rate) if change_rate is not None else None}
        result["answer"] = (f"本期{metric_labels[metric]}为 {total}，上月为 {previous}，变化 {change}（{change_rate}%）。"
                            if change_rate is not None else f"本期{metric_labels[metric]}为 {total}；上月没有已发布数据，暂不计算环比。")
    record_audit(db, ctx, "business_question", "certified-query", None, {"metric": metric})
    db.commit()
    return result


@app.get("/api/v1/analytics/overview", tags=["analytics"])
def analytics_overview(
    store_id: list[str] = Query(default=[]),
    platform_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    permitted = scoped_store_ids(db, ctx, store_id or None)
    if date_from and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    if platform_id:
        permitted &= set(db.scalars(select(Store.id).where(Store.enterprise_id == ctx.enterprise_id, Store.platform_account_id == platform_id)).all())
    stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id)
    if ctx.store_ids is not None or store_id or platform_id:
        stmt = stmt.where(CertifiedAggregate.store_id.in_(permitted))
    if date_from:
        stmt = stmt.where(CertifiedAggregate.period_start >= date_from)
    if date_to:
        stmt = stmt.where(CertifiedAggregate.period_start < date_to)
    rows = list(db.scalars(stmt).all())
    amount_keys = ["revenue", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "fees", "profit"]
    totals = {key: sum((getattr(row, key) for row in rows), start=Decimal(0)) for key in amount_keys}
    totals.update(row_count=sum(row.row_count for row in rows), order_count=sum(row.order_count for row in rows))
    totals["refund_rate"] = (totals["refund"] / totals["revenue"] * 100).quantize(Decimal("0.01")) if totals["revenue"] else Decimal(0)
    totals["profit_margin"] = (totals["profit"] / totals["revenue"] * 100).quantize(Decimal("0.01")) if totals["revenue"] else Decimal(0)
    trend_groups: dict[str, dict[str, Decimal]] = {}
    store_groups: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        period_key = row.period_start.date().isoformat()
        trend = trend_groups.setdefault(period_key, {key: Decimal(0) for key in amount_keys})
        by_store = store_groups.setdefault(row.store_id or "unassigned", {key: Decimal(0) for key in amount_keys})
        for key in amount_keys:
            trend[key] += getattr(row, key)
            by_store[key] += getattr(row, key)
    stores_by_id = {store.id: store.name for store in db.scalars(select(Store).where(Store.enterprise_id == ctx.enterprise_id)).all()}
    trend = [{"period": period_key, **{key: str(value) for key, value in values.items()}} for period_key, values in sorted(trend_groups.items())]
    by_store = [{"store_id": key, "store_name": stores_by_id.get(key, "未分配店铺"), **{metric: str(value) for metric, value in values.items()}} for key, values in store_groups.items()]
    comparison = None
    if date_from and date_to:
        duration = date_to - date_from
        previous_rows = list(db.scalars(select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id, CertifiedAggregate.period_start >= date_from - duration, CertifiedAggregate.period_start < date_from, CertifiedAggregate.store_id.in_(permitted) if (ctx.store_ids is not None or store_id or platform_id) else text("1=1"))).all())
        previous = {key: sum((getattr(row, key) for row in previous_rows), start=Decimal(0)) for key in amount_keys}
        comparison = {key: {"previous": str(previous[key]), "change": str(totals[key] - previous[key])} for key in amount_keys}
    return {"metrics": {key: str(value) if isinstance(value, Decimal) else value for key, value in totals.items()}, "trend": trend, "by_store": by_store, "comparison": comparison, "filters": {"store_ids": sorted(permitted) if (ctx.store_ids is not None or store_id or platform_id) else [], "platform_id": platform_id, "date_from": date_from, "date_to": date_to}}


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _superset_guest_token(ctx: RequestContext, dashboard_id: str, permitted_store_ids: set[str] | None = None) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    rls_clause = f"enterprise_id = '{ctx.enterprise_id}'"
    if permitted_store_ids is not None:
        quoted = ",".join(f"'{value}'" for value in sorted(permitted_store_ids))
        rls_clause += f" AND store_id IN ({quoted})" if quoted else " AND 1 = 0"
    payload = {
        "user": {"username": ctx.user_id, "first_name": "Business", "last_name": "Viewer"},
        "resources": [{"type": "dashboard", "id": dashboard_id}],
        "rls_rules": [{"clause": rls_clause}],
        "iat": now,
        "exp": now + 300,
        "aud": "commerce-analytics",
        "type": "guest",
    }
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_payload = _base64url(json.dumps(payload, separators=(",", ":")).encode())
    message = f"{encoded_header}.{encoded_payload}".encode()
    secret = os.getenv("SUPERSET_GUEST_TOKEN_SECRET", "development-guest-secret-change-me").encode()
    signature = _base64url(hmac.new(secret, message, hashlib.sha256).digest())
    return f"{message.decode()}.{signature}"


@app.post("/api/v1/dashboards/{dashboard_id}/embed-token", tags=["dashboards"])
def dashboard_embed_token(dashboard_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    dashboard = db.scalar(
        select(DashboardAsset).where(
            DashboardAsset.id == dashboard_id,
            DashboardAsset.enterprise_id == ctx.enterprise_id,
            DashboardAsset.bi_adapter == "superset",
            DashboardAsset.status.in_(["published", "active"]),
            DashboardAsset.archived_at.is_(None),
        )
    )
    if not dashboard or not dashboard.external_id:
        raise HTTPException(status_code=404, detail="published embedded dashboard not found")
    record_audit(db, ctx, "embed", "dashboards", dashboard.id)
    db.commit()
    permitted = scoped_store_ids(db, ctx) if ctx.store_ids is not None else None
    return {"token": _superset_guest_token(ctx, dashboard.external_id, permitted), "embedded_id": dashboard.external_id, "expires_in": 300}


def _register_resource_routes() -> None:
    for resource in RESOURCE_MODELS:
        path, tag = f"/api/v1/{resource}", resource.replace("/", "-")

        def create(body: ResourceCreate, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context), _resource=resource):
            return create_resource(db, ctx, _resource, body.model_dump())

        def listing(include_archived: bool = Query(default=False), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context), _resource=resource):
            return list_resources(db, ctx, _resource, include_archived)

        def retrieve(object_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context), _resource=resource):
            return serialize(get_resource(db, ctx, _resource, object_id))

        def patch(object_id: str, body: ResourcePatch, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context), _resource=resource):
            return update_resource(db, ctx, _resource, object_id, body.model_dump())

        def remove(object_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context), _resource=resource):
            return delete_resource(db, ctx, _resource, object_id)

        app.add_api_route(path, create, methods=["POST"], tags=[tag], name=f"create-{tag}")
        app.add_api_route(path, listing, methods=["GET"], tags=[tag], name=f"list-{tag}")
        app.add_api_route(f"{path}/{{object_id}}", retrieve, methods=["GET"], tags=[tag], name=f"get-{tag}")
        app.add_api_route(f"{path}/{{object_id}}", patch, methods=["PATCH"], tags=[tag], name=f"update-{tag}")
        app.add_api_route(f"{path}/{{object_id}}", remove, methods=["DELETE"], tags=[tag], name=f"delete-{tag}")


_register_resource_routes()
