"""HTTP surface for certification reports."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from commerce_harness.certify.report import (
    build_certification_report,
    persist_certification,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import WorkbenchPaths


class IssueBody(BaseModel):
    periodId: str
    operatorName: str


# Report ids are system-generated (``cert_<hex>``); nothing else may reach the
# filesystem.
_REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def build_certify_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/certifications", tags=["certifications"])

    @router.post("")
    def issue(body: IssueBody) -> dict[str, Any]:
        try:
            with DuckDBMemory(workbench.database) as database:
                database.initialize()
                report = build_certification_report(
                    database,
                    period_id=body.periodId,
                    operator_name=body.operatorName,
                )
                path = persist_certification(database, workbench, report)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**report, "path": str(path)}

    @router.get("/{report_id}")
    def get_report(report_id: str) -> dict[str, Any]:
        if not _REPORT_ID_PATTERN.match(report_id):
            raise HTTPException(status_code=400, detail="非法报告编号")
        export_dir = (workbench.root / "exports" / "certifications").resolve()
        path = (export_dir / f"{report_id}.json").resolve()
        if path.parent != export_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="认证报告不存在")
        stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return stored

    return router
