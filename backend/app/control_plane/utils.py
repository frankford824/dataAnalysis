from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def model_columns(model: type[Any]) -> set[str]:
    return {column.key for column in inspect(model).mapper.column_attrs}


def make_model(model: type[Any], **values: Any) -> Any:
    columns = model_columns(model)
    return model(**{key: value for key, value in values.items() if key in columns})


def set_fields(obj: Any, **values: Any) -> None:
    columns = model_columns(type(obj))
    for key, value in values.items():
        if key in columns:
            setattr(obj, key, value)


def serialize(obj: Any) -> dict[str, Any]:
    result = {column.key: getattr(obj, column.key) for column in inspect(obj).mapper.column_attrs}
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    for sensitive in {"secret_hash", "agent_key_hash", "token_hash", "encrypted_api_key"}:
        result.pop(sensitive, None)
    return result


def object_store_ids(obj: Any) -> set[str]:
    direct = getattr(obj, "store_ids", None)
    if direct:
        return {str(value) for value in direct}
    payload = (
        getattr(obj, "payload", None)
        or getattr(obj, "config", None)
        or getattr(obj, "read_policy", None)
        or {}
    )
    return {str(value) for value in payload.get("store_ids", [])}


def context_allows_object(ctx: Any, obj: Any) -> bool:
    if ctx.store_ids is None:
        return True
    object_scopes = object_store_ids(obj)
    return not object_scopes or bool(object_scopes.intersection(ctx.store_ids))
