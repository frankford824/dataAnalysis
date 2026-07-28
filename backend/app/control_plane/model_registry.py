from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class ReconciliationModels:
    ExternalAgent: type[Any]
    AgentEnrollmentToken: type[Any]
    SourceConnector: type[Any]
    AgentJob: type[Any]
    AgentJobEvent: type[Any]
    DiscoveredFile: type[Any]
    ReviewItem: type[Any]
    ReviewDecision: type[Any]


def get_reconciliation_models() -> ReconciliationModels:
    """Resolve the reconciliation bounded-context models at request time."""

    try:
        module = import_module("app.reconciliation.models")
        return ReconciliationModels(
            ExternalAgent=module.ExternalAgent,
            AgentEnrollmentToken=module.AgentEnrollmentToken,
            SourceConnector=module.SourceConnector,
            AgentJob=module.AgentJob,
            AgentJobEvent=module.AgentJobEvent,
            DiscoveredFile=module.DiscoveredFile,
            ReviewItem=module.ReviewItem,
            ReviewDecision=module.ReviewDecision,
        )
    except (ImportError, AttributeError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "reconciliation_schema_unavailable",
                "message": "对账控制面尚未完成数据库迁移，请先应用当前版本迁移。",
            },
        ) from exc
