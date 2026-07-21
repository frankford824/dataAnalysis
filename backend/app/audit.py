from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditLog
from .security import RequestContext


def record_audit(
    db: Session,
    ctx: RequestContext,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            enterprise_id=ctx.enterprise_id,
            actor_id=ctx.user_id,
            actor_role=ctx.role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )
