"""Period-close API routes: preclose, finalize, adjustments, detail."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from commerce_harness.kernel.period import InvalidPeriodTransition
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.period_service import (
    finalize_period,
    get_period_detail,
    post_adjustment,
    preclose_period,
)
from commerce_harness.workbench import WorkbenchPaths


class OperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    operator_name: str = Field(alias="operatorName", min_length=1, max_length=200)


class AdjustmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    operator_name: str = Field(alias="operatorName", min_length=1, max_length=200)
    amount: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=2, max_length=2000)
    unresolved_id: str | None = Field(default=None, alias="unresolvedId")


def build_period_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/periods", tags=["periods"])

    def _memory() -> DuckDBMemory:
        return DuckDBMemory(workbench.database)

    @router.post("/{period_id}/preclose")
    def api_preclose(period_id: str, body: OperatorRequest) -> dict[str, Any]:
        try:
            with _memory() as database:
                return preclose_period(database, period_id, body.operator_name)
        except LookupError:
            raise HTTPException(status_code=404, detail="账期不存在") from None
        except InvalidPeriodTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{period_id}/finalize")
    def api_finalize(period_id: str, body: OperatorRequest) -> dict[str, Any]:
        try:
            with _memory() as database:
                return finalize_period(database, period_id, body.operator_name)
        except LookupError:
            raise HTTPException(status_code=404, detail="账期不存在") from None
        except InvalidPeriodTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{period_id}/adjustments")
    def api_post_adjustment(
        period_id: str, body: AdjustmentRequest
    ) -> dict[str, Any]:
        try:
            decimal_amount = Decimal(body.amount)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="金额格式无效") from None
        try:
            with _memory() as database:
                return post_adjustment(
                    database,
                    period_id,
                    body.operator_name,
                    amount=decimal_amount,
                    reason=body.reason,
                    unresolved_id=body.unresolved_id,
                )
        except LookupError:
            raise HTTPException(status_code=404, detail="账期不存在") from None
        except InvalidPeriodTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/{period_id}")
    def api_get_period(period_id: str) -> dict[str, Any]:
        try:
            with _memory() as database:
                return get_period_detail(database, period_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="账期不存在") from None

    return router
