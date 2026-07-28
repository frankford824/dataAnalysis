from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from .model_registry import ReconciliationModels, get_reconciliation_models


@dataclass(frozen=True)
class AgentContext:
    id: str
    enterprise_id: str
    name: str


def secret_hash(value: str) -> str:
    pepper = get_settings().secret_key
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def get_agent_context(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-ID"),
    x_agent_secret: str | None = Header(default=None, alias="X-Agent-Secret"),
    db: Session = Depends(get_db),
    models: ReconciliationModels = Depends(get_reconciliation_models),
) -> AgentContext:
    bearer_secret = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_agent_authorization", "message": "执行器访问令牌格式无效。"},
            )
        bearer_secret = value.strip()
    supplied_secret = bearer_secret or x_agent_secret
    if not supplied_secret:
        raise HTTPException(
            status_code=401,
            detail={"code": "agent_auth_required", "message": "执行器需要提供代理身份。"},
        )
    if x_agent_id:
        agent = db.get(models.ExternalAgent, x_agent_id)
    else:
        hash_column = (
            models.ExternalAgent.secret_hash
            if hasattr(models.ExternalAgent, "secret_hash")
            else models.ExternalAgent.agent_key_hash
        )
        agent = db.scalar(select(models.ExternalAgent).where(hash_column == secret_hash(supplied_secret)))
    expected = (
        getattr(agent, "secret_hash", None)
        or getattr(agent, "agent_key_hash", "")
        if agent
        else ""
    )
    agent_status = getattr(agent, "status", None) if agent else None
    if (
        not agent
        or agent_status in {None, "disabled", "archived"}
        or not hmac.compare_digest(expected, secret_hash(supplied_secret))
    ):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_agent_credentials", "message": "执行器身份无效或已停用。"},
        )
    return AgentContext(
        agent.id,
        agent.enterprise_id,
        getattr(agent, "name", None) or getattr(agent, "display_name", agent.id),
    )
