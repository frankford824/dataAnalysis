from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy import inspect, or_, select
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
from .security import DATA_CONFIG_ROLES, RequestContext, USER_ADMIN_ROLES, WRITE_ROLES, encrypt_secret, hash_password


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
    if "password_hash" in result:
        result.pop("password_hash")
    if isinstance(obj, IngestionRun):
        result["checks"] = (obj.quality_result or {}).get("checks", [])
        result["cost"] = (obj.summary or {}).get("product_cost", "0.0000")
        result["duplicate_rows_removed"] = (obj.summary or {}).get("duplicate_rows_removed", 0)
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
        if "role" in data and data.get("role") not in {"admin", "implementer", "analyst", "viewer"}:
            raise HTTPException(status_code=422, detail="invalid enterprise role")
        for store_id in data.get("store_ids", []):
            _validate_tenant_reference(db, Store, store_id, enterprise_id)
        if "store_ids" in data:
            data["store_ids"] = [db.get(Store, store_id).logical_id for store_id in data["store_ids"]]
    if model is SourceDefinition:
        if data.get("import_mode") == "incremental" and not data.get("dedupe_keys"):
            raise HTTPException(status_code=422, detail="增量导入必须配置稳定业务键")
        if data.get("source_kind") == "fees" and "order_id" in (data.get("dedupe_keys") or []):
            # Fee exports may carry reference IDs, but they are not order facts.
            pass
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


def create_resource(db: Session, ctx: RequestContext, resource: str, payload: dict, *, commit: bool = True) -> dict:
    if resource in {"metrics", "semantic-models"}:
        raise HTTPException(status_code=405, detail="标准模型与指标元数据只读，由内置注册表发布")
    ctx.require(USER_ADMIN_ROLES if resource == "users" else DATA_CONFIG_ROLES if resource in {"platforms", "stores", "sources", "source-bindings", "source-schedules"} else WRITE_ROLES)
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    data = {key: value for key, value in payload.items() if value is not None and key in _columns(model)}
    validate_references(db, model, data, ctx.enterprise_id)
    if model is AIProvider and payload.get("api_key"):
        data["encrypted_api_key"] = encrypt_secret(payload["api_key"])
    if model is UserAccount:
        if not payload.get("password"):
            raise HTTPException(status_code=422, detail="password is required for a user account")
        data["email"] = data.get("email", "").strip().lower()
        if db.scalar(select(UserAccount.id).where(UserAccount.email == data["email"])):
            raise HTTPException(status_code=409, detail="email is already registered")
        data["password_hash"] = hash_password(payload["password"])
        data.setdefault("must_change_password", False)
    if model in {Store, PlatformAccount, SourceDefinition}:
        data.setdefault("logical_id", __import__("uuid").uuid4().hex)
    required = {
        PlatformAccount: ["platform"],
        Store: ["activation_at", "platform_account_id"],
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
    if commit:
        db.commit()
        db.refresh(obj)
    return serialize(obj)


def list_resources(db: Session, ctx: RequestContext, resource: str, include_archived: bool = False) -> list[dict]:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    if model is UserAccount:
        ctx.require(USER_ADMIN_ROLES)
    stmt = select(model).where(model.enterprise_id == ctx.enterprise_id)
    if not include_archived:
        now = datetime.now(timezone.utc)
        stmt = stmt.where(
            model.archived_at.is_(None),
            or_(model.effective_from.is_(None), model.effective_from <= now),
            or_(model.effective_to.is_(None), model.effective_to > now),
        )
    items = list(db.scalars(stmt.order_by(model.version.desc(), model.created_at.desc())).all())
    if model is Store:
        if ctx.store_ids is not None:
            items = [item for item in items if item.id in ctx.store_ids or item.logical_id in ctx.store_ids]
        if not include_archived:
            current: dict[str, Any] = {}
            for item in items:
                current.setdefault(item.logical_id, item)
            items = list(current.values())
    if model in {PlatformAccount, SourceDefinition} and not include_archived:
        current = {}
        for item in items:
            current.setdefault(item.logical_id, item)
        items = list(current.values())
    return [serialize(item) for item in items]


def get_resource(db: Session, ctx: RequestContext, resource: str, object_id: str):
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    model = RESOURCE_MODELS[resource]
    if model is UserAccount:
        ctx.require(USER_ADMIN_ROLES)
    obj = db.scalar(select(model).where(model.id == object_id, model.enterprise_id == ctx.enterprise_id))
    if not obj:
        raise HTTPException(status_code=404, detail="resource not found")
    if model is Store and ctx.store_ids is not None and obj.id not in ctx.store_ids and obj.logical_id not in ctx.store_ids:
        raise HTTPException(status_code=404, detail="resource not found")
    return obj


def update_resource(db: Session, ctx: RequestContext, resource: str, object_id: str, payload: dict) -> dict:
    if resource in {"metrics", "semantic-models"}:
        raise HTTPException(status_code=405, detail="标准模型与指标元数据只读，由内置注册表发布")
    ctx.require(USER_ADMIN_ROLES if resource == "users" else DATA_CONFIG_ROLES if resource in {"platforms", "stores", "sources", "source-bindings", "source-schedules"} else WRITE_ROLES)
    obj = get_resource(db, ctx, resource, object_id)
    model = type(obj)
    data = {key: value for key, value in payload.items() if value is not None and key in _columns(model)}
    if model is AIProvider and payload.get("api_key"):
        data["encrypted_api_key"] = encrypt_secret(payload["api_key"])
    if model is UserAccount and payload.get("password"):
        data["password_hash"] = hash_password(payload["password"])
        data["password_changed_at"] = datetime.now(timezone.utc)
        data["must_change_password"] = False
    validate_references(db, model, data, ctx.enterprise_id or "")
    immutable_statuses = {"active", "approved", "published", "locked"}
    if model is not UserAccount and obj.status in immutable_statuses and any(key not in {"status", "approved_by", "effective_to"} for key in data):
        clone_data = {
            column.key: getattr(obj, column.key)
            for column in inspect(model).mapper.column_attrs
            if column.key not in {"id", "version", "created_at", "updated_at", "archived_at", "approved_by", "status", "effective_to"}
        }
        clone_data.update(data)
        immediately_effective = model in {PlatformAccount, Store, SourceDefinition}
        clone_data.update(
            version=obj.version + 1,
            status=obj.status if immediately_effective else "draft",
            created_by=ctx.user_id,
            approved_by=ctx.user_id if immediately_effective else None,
            archived_at=None,
        )
        now = datetime.now(timezone.utc)
        obj.effective_to = now
        clone_data["effective_from"] = now
        new_obj = model(**clone_data)
        db.add(new_obj)
        db.flush()
        if model is SourceDefinition:
            bindings = db.scalars(
                select(SourceBinding).where(
                    SourceBinding.enterprise_id == obj.enterprise_id,
                    SourceBinding.source_definition_id == obj.id,
                    SourceBinding.archived_at.is_(None),
                    or_(SourceBinding.effective_to.is_(None), SourceBinding.effective_to > now),
                )
            ).all()
            for binding in bindings:
                binding.effective_to = now
                db.add(SourceBinding(
                    enterprise_id=binding.enterprise_id,
                    name=binding.name,
                    source_definition_id=new_obj.id,
                    scope_type=binding.scope_type,
                    scope_id=binding.scope_id,
                    status=binding.status,
                    version=binding.version + 1,
                    effective_from=now,
                    created_by=ctx.user_id,
                    approved_by=ctx.user_id,
                ))
        elif model is PlatformAccount:
            current_stores = db.scalars(
                select(Store).where(
                    Store.enterprise_id == obj.enterprise_id,
                    Store.platform_account_id == obj.id,
                    Store.archived_at.is_(None),
                    or_(Store.effective_to.is_(None), Store.effective_to > now),
                )
            ).all()
            for store in current_stores:
                store.effective_to = now
                store_data = {
                    column.key: getattr(store, column.key)
                    for column in inspect(Store).mapper.column_attrs
                    if column.key not in {"id", "version", "created_at", "updated_at", "effective_from", "effective_to", "archived_at", "approved_by", "created_by"}
                }
                store_data.update(
                    platform_account_id=new_obj.id,
                    version=store.version + 1,
                    effective_from=now,
                    status=store.status,
                    created_by=ctx.user_id,
                    approved_by=ctx.user_id,
                )
                db.add(Store(**store_data))
            platform_bindings = db.scalars(
                select(SourceBinding).where(
                    SourceBinding.enterprise_id == obj.enterprise_id,
                    SourceBinding.scope_type == "platform_account",
                    SourceBinding.scope_id == obj.id,
                    SourceBinding.archived_at.is_(None),
                    or_(SourceBinding.effective_to.is_(None), SourceBinding.effective_to > now),
                )
            ).all()
            for binding in platform_bindings:
                binding.effective_to = now
                db.add(SourceBinding(
                    enterprise_id=binding.enterprise_id,
                    name=binding.name,
                    source_definition_id=binding.source_definition_id,
                    scope_type=binding.scope_type,
                    scope_id=new_obj.id,
                    status=binding.status,
                    version=binding.version + 1,
                    effective_from=now,
                    created_by=ctx.user_id,
                    approved_by=ctx.user_id,
                ))
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
    if resource in {"metrics", "semantic-models"}:
        raise HTTPException(status_code=405, detail="标准模型与指标元数据只读，由内置注册表发布")
    ctx.require(USER_ADMIN_ROLES if resource == "users" else DATA_CONFIG_ROLES if resource in {"platforms", "stores", "sources", "source-bindings", "source-schedules"} else WRITE_ROLES)
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
