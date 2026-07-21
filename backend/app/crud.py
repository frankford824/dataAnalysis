from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from .audit import record_audit
from .models import (
    AIModelProfile,
    AIProvider,
    AITaskPolicy,
    BusinessEntity,
    DashboardAsset,
    IngestionRun,
    MetricDefinition,
    ModelAsset,
    ModelScopeBinding,
    PlatformAccount,
    SemanticModelVersion,
    SourceBinding,
    SourceDefinition,
    SourceSchedule,
    Store,
    UserAccount,
)
from .security import RequestContext, WRITE_ROLES, encrypt_secret


RESOURCE_MODELS = {
    "business-entities": BusinessEntity,
    "platforms": PlatformAccount,
    "stores": Store,
    "users": UserAccount,
    "sources": SourceDefinition,
    "source-bindings": SourceBinding,
    "source-schedules": SourceSchedule,
    "model-assets": ModelAsset,
    "model-scope-bindings": ModelScopeBinding,
    "semantic-models": SemanticModelVersion,
    "metrics": MetricDefinition,
    "dashboards": DashboardAsset,
    "ai/providers": AIProvider,
    "ai/models": AIModelProfile,
    "ai/task-policies": AITaskPolicy,
}

T = TypeVar("T")


def serialize(obj: Any) -> dict[str, Any]:
    result = {column.key: getattr(obj, column.key) for column in inspect(obj).mapper.column_attrs}
    if "encrypted_api_key" in result:
        result["has_api_key"] = bool(result.pop("encrypted_api_key"))
    return result


def _columns(model) -> set[str]:
    return {column.key for column in inspect(model).mapper.column_attrs}


def _validate_tenant_reference(db: Session, model, value: str, enterprise_id: str) -> None:
    target = db.get(model, value)
    if not target or target.enterprise_id != enterprise_id:
        raise HTTPException(status_code=422, detail=f"cross-tenant or missing reference: {model.__tablename__}")


def validate_references(db: Session, model, data: dict, enterprise_id: str) -> None:
    refs = {
        "platform_account_id": PlatformAccount,
        "business_entity_id": BusinessEntity,
        "source_definition_id": SourceDefinition,
        "model_asset_id": ModelAsset,
        "semantic_model_id": SemanticModelVersion,
        "provider_id": AIProvider,
        "primary_model_id": AIModelProfile,
        "fallback_model_id": AIModelProfile,
    }
    for field, target_model in refs.items():
        if data.get(field):
            _validate_tenant_reference(db, target_model, data[field], enterprise_id)
    if model is UserAccount:
        if data.get("role") not in {"admin", "implementer", "analyst", "viewer"}:
            raise HTTPException(status_code=422, detail="invalid enterprise role")
        for store_id in data.get("store_ids", []):
            _validate_tenant_reference(db, Store, store_id, enterprise_id)
    if model in {SourceBinding, ModelScopeBinding} and data.get("scope_type") != "enterprise":
        target_map = {
            "store": Store,
            "platform_account": PlatformAccount,
            "business_entity": BusinessEntity,
            "source": SourceDefinition,
        }
        target = target_map.get(data.get("scope_type"))
        if not target:
            raise HTTPException(status_code=422, detail="invalid scope_type")
        _validate_tenant_reference(db, target, data.get("scope_id", ""), enterprise_id)
    elif model in {SourceBinding, ModelScopeBinding} and data.get("scope_id") != enterprise_id:
        raise HTTPException(status_code=422, detail="enterprise scope must reference current enterprise")


def create_resource(db: Session, ctx: RequestContext, resource: str, payload: dict) -> dict:
    ctx.require(WRITE_ROLES)
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    data = {key: value for key, value in payload.items() if value is not None and key in _columns(model)}
    validate_references(db, model, data, ctx.enterprise_id)
    if model is AIProvider and payload.get("api_key"):
        data["encrypted_api_key"] = encrypt_secret(payload["api_key"])
    required = {
        PlatformAccount: ["platform"],
        Store: ["activation_at"],
        SourceDefinition: ["coverage_time_field", "data_granularity", "arrival_frequency", "activation_at"],
        SourceBinding: ["source_definition_id", "scope_type", "scope_id"],
        SourceSchedule: ["source_definition_id", "cron"],
        ModelScopeBinding: ["model_asset_id", "scope_type", "scope_id"],
        MetricDefinition: ["key", "expression"],
        AIModelProfile: ["provider_id", "model_name"],
        AITaskPolicy: ["task"],
        UserAccount: ["email", "role"],
    }.get(model, [])
    missing = [field for field in required if data.get(field) is None]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing required fields: {', '.join(missing)}")
    obj = model(enterprise_id=ctx.enterprise_id, created_by=ctx.user_id, **data)
    db.add(obj)
    db.flush()
    record_audit(db, ctx, "create", resource, obj.id, {"version": obj.version})
    db.commit()
    db.refresh(obj)
    return serialize(obj)


def list_resources(db: Session, ctx: RequestContext, resource: str, include_archived: bool = False) -> list[dict]:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    stmt = select(model).where(model.enterprise_id == ctx.enterprise_id)
    if not include_archived:
        stmt = stmt.where(model.archived_at.is_(None))
    return [serialize(item) for item in db.scalars(stmt.order_by(model.created_at.desc())).all()]


def get_resource(db: Session, ctx: RequestContext, resource: str, object_id: str):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    obj = db.scalar(select(model).where(model.id == object_id, model.enterprise_id == ctx.enterprise_id))
    if not obj:
        raise HTTPException(status_code=404, detail="resource not found")
    return obj


def update_resource(db: Session, ctx: RequestContext, resource: str, object_id: str, payload: dict) -> dict:
    ctx.require(WRITE_ROLES)
    obj = get_resource(db, ctx, resource, object_id)
    model = type(obj)
    data = {key: value for key, value in payload.items() if value is not None and key in _columns(model)}
    if model is AIProvider and payload.get("api_key"):
        data["encrypted_api_key"] = encrypt_secret(payload["api_key"])
    validate_references(db, model, data, ctx.enterprise_id or "")
    immutable_statuses = {"active", "approved", "published", "locked"}
    if obj.status in immutable_statuses and any(key not in {"status", "approved_by", "effective_to"} for key in data):
        clone_data = {
            column.key: getattr(obj, column.key)
            for column in inspect(model).mapper.column_attrs
            if column.key not in {"id", "version", "created_at", "updated_at", "archived_at", "approved_by", "status", "effective_to"}
        }
        clone_data.update(data)
        clone_data.update(version=obj.version + 1, status="draft", created_by=ctx.user_id, approved_by=None, archived_at=None)
        now = datetime.now(timezone.utc)
        obj.effective_to = now
        clone_data.setdefault("effective_from", now)
        new_obj = model(**clone_data)
        db.add(new_obj)
        db.flush()
        record_audit(db, ctx, "version", resource, new_obj.id, {"previous_id": obj.id, "version": new_obj.version})
        db.commit()
        return serialize(new_obj)
    for key, value in data.items():
        setattr(obj, key, value)
    record_audit(db, ctx, "update", resource, obj.id, {"fields": sorted(data)})
    db.commit()
    db.refresh(obj)
    return serialize(obj)


def _is_referenced(db: Session, obj: Any) -> bool:
    checks = {
        SourceDefinition: [(SourceBinding, SourceBinding.source_definition_id), (SourceSchedule, SourceSchedule.source_definition_id), (IngestionRun, IngestionRun.source_definition_id)],
        ModelAsset: [(ModelScopeBinding, ModelScopeBinding.model_asset_id)],
        AIProvider: [(AIModelProfile, AIModelProfile.provider_id)],
        SemanticModelVersion: [(MetricDefinition, MetricDefinition.semantic_model_id)],
        AIModelProfile: [(AITaskPolicy, AITaskPolicy.primary_model_id), (AITaskPolicy, AITaskPolicy.fallback_model_id)],
    }
    if isinstance(obj, Store):
        if db.scalar(select(IngestionRun.id).where(IngestionRun.store_id == obj.id).limit(1)):
            return True
        if db.scalar(select(SourceBinding.id).where(SourceBinding.scope_type == "store", SourceBinding.scope_id == obj.id).limit(1)):
            return True
        if db.scalar(select(ModelScopeBinding.id).where(ModelScopeBinding.scope_type == "store", ModelScopeBinding.scope_id == obj.id).limit(1)):
            return True
    return any(db.scalar(select(model.id).where(column == obj.id).limit(1)) for model, column in checks.get(type(obj), []))


def delete_resource(db: Session, ctx: RequestContext, resource: str, object_id: str) -> dict:
    ctx.require(WRITE_ROLES)
    obj = get_resource(db, ctx, resource, object_id)
    if obj.status == "draft" and not _is_referenced(db, obj):
        db.delete(obj)
        action = "delete_draft"
    else:
        obj.status = "archived"
        obj.archived_at = datetime.now(timezone.utc)
        action = "archive"
    record_audit(db, ctx, action, resource, object_id)
    db.commit()
    return {"id": object_id, "action": action}
