from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from redis import Redis

from .audit import record_audit
from .config import get_settings
from .crud import RESOURCE_MODELS, create_resource, delete_resource, get_resource, list_resources, serialize, update_resource
from .db import Base, engine, get_db
from .ingestion import confirm_ingestion, create_ingestion, lock_ingestion, publish_ingestion
from .models import AuditLog, CertifiedAggregate, DashboardAsset, Enterprise, IngestionRun, ModelAsset, SourceDefinition, Store, UploadSession
from .pbix import parse_pbix
from .query import execute_certified_query
from .schemas import BusinessConfirmation, CertifiedQuery, ConfigurationImport, EnterpriseCreate, ManualPBIXMetadata, NaturalLanguageQuestion, ResourceCreate, ResourcePatch, UploadInitiate
from .security import APPROVE_ROLES, RequestContext, WRITE_ROLES, get_context
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
    resources = {name: list_resources(db, ctx, name, include_archived=True) for name in RESOURCE_MODELS}
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
    counts = {resource: len(items) for resource, items in body.resources.items()}
    if body.dry_run:
        return {"dry_run": True, "valid": True, "counts": counts}
    id_map: dict[str, str] = {}
    imported: dict[str, int] = {}
    reference_fields = {"platform_account_id", "business_entity_id", "source_definition_id", "model_asset_id", "semantic_model_id", "provider_id", "primary_model_id", "fallback_model_id", "scope_id"}
    ordered = ["business-entities", "platforms", "stores", "users", "sources", "source-bindings", "source-schedules", "model-assets", "model-scope-bindings", "semantic-models", "metrics", "dashboards", "ai/providers", "ai/models", "ai/task-policies"]
    safe_fields = set(ResourceCreate.model_fields)
    for resource in ordered:
        for raw in body.resources.get(resource, []):
            payload = {key: value for key, value in raw.items() if key in safe_fields and key not in {"api_key"}}
            for field in reference_fields:
                if payload.get(field) in id_map:
                    payload[field] = id_map[payload[field]]
            if resource in {"source-bindings", "model-scope-bindings"} and payload.get("scope_type") == "enterprise":
                payload["scope_id"] = ctx.enterprise_id
            old_id = raw.get("id")
            created = create_resource(db, ctx, resource, payload)
            if old_id:
                id_map[str(old_id)] = created["id"]
            imported[resource] = imported.get(resource, 0) + 1
    record_audit(db, ctx, "import", "configuration", None, {"counts": imported})
    db.commit()
    return {"dry_run": False, "imported": imported}


@app.get("/api/v1/exports/certified", tags=["analytics"])
def export_certified(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    store_id: list[str] = Query(default=[]),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_context),
):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id)
    if store_id:
        stmt = stmt.where(CertifiedAggregate.store_id.in_(store_id))
    if date_from:
        stmt = stmt.where(CertifiedAggregate.period_start >= date_from)
    if date_to:
        stmt = stmt.where(CertifiedAggregate.period_start <= date_to)
    rows = [serialize(item) for item in db.scalars(stmt.order_by(CertifiedAggregate.period_start)).all()]
    record_audit(db, ctx, "export", "certified-data", None, {"format": format, "row_count": len(rows)})
    db.commit()
    if format == "json":
        return {"rows": rows, "row_count": len(rows)}
    columns = ["store_id", "period_start", "grain", "row_count", "order_count", "revenue", "refund", "fees", "profit"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=certified-data.csv"})


def _tenant_ingestion(db: Session, ctx: RequestContext, run_id: str) -> IngestionRun:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    run = db.scalar(select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.enterprise_id == ctx.enterprise_id))
    if not run:
        raise HTTPException(status_code=404, detail="ingestion not found")
    return run


@app.get("/api/v1/ingestions", tags=["ingestions"])
def list_ingestions(db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    return [serialize(item) for item in db.scalars(select(IngestionRun).where(IngestionRun.enterprise_id == ctx.enterprise_id).order_by(IngestionRun.created_at.desc())).all()]


@app.get("/api/v1/ingestions/{run_id}", tags=["ingestions"])
def get_ingestion(run_id: str, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    return serialize(_tenant_ingestion(db, ctx, run_id))


@app.post("/api/v1/ingestions/upload", tags=["ingestions"])
async def upload_ingestion(source_definition_id: str = Form(...), store_id: str | None = Form(default=None), backfill: bool = Form(default=False), file: UploadFile = File(...), db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    ctx.require(WRITE_ROLES)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    return serialize(create_ingestion(db, get_storage(), ctx, source_definition_id, file.filename or "upload", data, store_id, backfill))


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
    source = db.scalar(select(SourceDefinition.id).where(SourceDefinition.id == body.source_definition_id, SourceDefinition.enterprise_id == ctx.enterprise_id))
    if not source:
        raise HTTPException(status_code=404, detail="source definition not found")
    if body.store_id and not db.scalar(select(Store.id).where(Store.id == body.store_id, Store.enterprise_id == ctx.enterprise_id)):
        raise HTTPException(status_code=422, detail="store not found")
    existing = db.scalar(select(IngestionRun).where(IngestionRun.enterprise_id == ctx.enterprise_id, IngestionRun.source_definition_id == body.source_definition_id, IngestionRun.source_sha256 == body.sha256.lower()))
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
    if part_number < 1 or part_number > 100_000:
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
        existing = db.scalar(select(IngestionRun).where(IngestionRun.enterprise_id == upload.enterprise_id, IngestionRun.source_definition_id == upload.source_definition_id, IngestionRun.source_sha256 == upload.expected_sha256))
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
    run = create_ingestion(db, storage, ctx, upload.source_definition_id, upload.filename, data, upload.store_id, upload.is_backfill)
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
    result = execute_certified_query(db, ctx.enterprise_id, body.sql)
    record_audit(db, ctx, "query", "certified-query", None, {"sql": body.sql, "row_count": result["row_count"]})
    db.commit()
    return result


@app.post("/api/v1/business-questions", tags=["analytics"])
def business_question(body: NaturalLanguageQuestion, db: Session = Depends(get_db), ctx: RequestContext = Depends(get_context)):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    if db.get(Enterprise, ctx.enterprise_id) is None:
        raise HTTPException(status_code=404, detail="enterprise not found")
    allowed_stores: list[str] = []
    if body.store_ids:
        from .models import Store
        allowed_stores = list(db.scalars(select(Store.id).where(Store.enterprise_id == ctx.enterprise_id, Store.id.in_(body.store_ids))).all())
        if len(allowed_stores) != len(set(body.store_ids)):
            raise HTTPException(status_code=422, detail="store context contains an unknown store")
    question = body.question.lower()
    metric = "profit" if any(word in question for word in ["profit", "利润"]) else "refund" if any(word in question for word in ["refund", "退款"]) else "revenue"
    stmt = select(CertifiedAggregate).where(CertifiedAggregate.enterprise_id == ctx.enterprise_id)
    if allowed_stores:
        stmt = stmt.where(CertifiedAggregate.store_id.in_(allowed_stores))
    if body.date_from:
        stmt = stmt.where(CertifiedAggregate.period_start >= body.date_from)
    if body.date_to:
        stmt = stmt.where(CertifiedAggregate.period_start <= body.date_to)
    rows = db.scalars(stmt).all()
    total = sum((getattr(row, metric) for row in rows), start=0)
    result = {"answer": f"{metric} total is {total}", "metric": metric, "value": str(total), "currency": "configured enterprise currency", "context": {"stores": body.store_ids, "date_from": body.date_from, "date_to": body.date_to}, "options": ["按店铺查看", "按月份查看", "导出结果"], "ai_used": False}
    record_audit(db, ctx, "business_question", "certified-query", None, {"metric": metric})
    db.commit()
    return result


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _superset_guest_token(ctx: RequestContext, dashboard_id: str) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "user": {"username": ctx.user_id, "first_name": "Business", "last_name": "Viewer"},
        "resources": [{"type": "dashboard", "id": dashboard_id}],
        "rls_rules": [{"clause": f"enterprise_id = '{ctx.enterprise_id}'"}],
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
    return {"token": _superset_guest_token(ctx, dashboard.external_id), "embedded_id": dashboard.external_id, "expires_in": 300}


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
