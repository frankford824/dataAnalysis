"""Claims API router (Phase 4).

Product red line: we only discover and package evidence, never auto-submit.
All submit/response/recovery actions require an explicit operator name.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import WorkbenchPaths

from . import detect as detect_module
from . import packet as packet_module
from . import service as claim_service
from . import value as value_module


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operatorName: str = Field(min_length=1, max_length=200)
    externalRef: str | None = None


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operatorName: str = Field(min_length=1, max_length=200)
    verdict: Literal["accepted", "partially_accepted", "rejected"]
    acceptedAmount: str | None = None
    responseText: str | None = None


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operatorName: str = Field(min_length=1, max_length=200)
    recoveredAmount: str


class DetectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    periodId: str | None = None
    storeId: str | None = None


# Claim ids are system-generated; anything else must never reach the filesystem.
_CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _require_safe_claim_id(claim_id: str) -> str:
    if not _CLAIM_ID_PATTERN.match(claim_id):
        raise HTTPException(status_code=400, detail="非法索赔编号")
    return claim_id


def _resolve_packet_dir(exports_root: Path, claim_id: str) -> Path:
    """Resolve the packet directory and refuse anything outside exports/claims."""
    claims_root = (exports_root / "claims").resolve()
    candidate = (claims_root / claim_id).resolve()
    if candidate.parent != claims_root:
        raise HTTPException(status_code=400, detail="非法索赔编号")
    return candidate


def build_claims_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/claims", tags=["claims"])

    @contextmanager
    def memory() -> Iterator[DuckDBMemory]:
        with DuckDBMemory(workbench.database) as database:
            yield database

    @router.get("")
    def list_claims(
        periodId: str | None = Query(default=None),
        storeId: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with memory() as database:
            return claim_service.list_claims(
                database,
                period_id=periodId,
                store_id=storeId,
                status=status,
                limit=limit,
                offset=offset,
            )

    @router.get("/page")
    def list_claims_page(
        periodId: str | None = Query(default=None),
        storeId: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with memory() as database:
            return claim_service.list_claims(
                database,
                period_id=periodId,
                store_id=storeId,
                status=status,
                limit=limit,
                offset=offset,
            )

    @router.get("/stats")
    def claim_stats() -> list[dict[str, Any]]:
        with memory() as database:
            return value_module.refresh_invariant_claim_stats(database)

    @router.get("/{claim_id}")
    def get_claim(claim_id: str) -> dict[str, Any]:
        with memory() as database:
            try:
                return claim_service.get_claim(database, claim_id)
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="claim not found"
                ) from None

    @router.get("/{claim_id}/packet")
    def get_packet(claim_id: str) -> StreamingResponse:
        _require_safe_claim_id(claim_id)
        exports_root = workbench.root / "exports"
        # Existence is checked before any filesystem work so an unknown id can
        # never cause a directory to be read or created.
        with memory() as database:
            try:
                claim_service.get_claim(database, claim_id)
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="claim not found"
                ) from None
            packet_dir = _resolve_packet_dir(exports_root, claim_id)
            if not packet_dir.is_dir():
                result = packet_module.export_packet(
                    database, claim_id, exports_root
                )
                packet_dir = _resolve_packet_dir(
                    exports_root, Path(result["packet_dir"]).name
                )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(packet_dir.rglob("*")):
                if file_path.is_file():
                    arcname = file_path.relative_to(packet_dir).as_posix()
                    zf.write(file_path, arcname)
        buf.seek(0)

        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="claim-{claim_id}.zip"'
            },
        )

    @router.post("/{claim_id}/submit")
    def submit_claim(claim_id: str, body: SubmitRequest) -> dict[str, Any]:
        _require_safe_claim_id(claim_id)
        with memory() as database:
            try:
                claim = claim_service.get_claim(database, claim_id)
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="claim not found"
                ) from None

            if claim["status"] == "draft":
                exports_root = workbench.root / "exports"
                result = packet_module.export_packet(database, claim_id, exports_root)
                claim_service.transition_to_packaged(
                    database, claim_id,
                    packet_sha256=result["packet_sha256"],
                    actor=body.operatorName,
                )

            try:
                return claim_service.transition_to_submitted(
                    database, claim_id,
                    operator_name=body.operatorName,
                    external_ref=body.externalRef,
                )
            except claim_service.InvalidClaimTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{claim_id}/response")
    def respond_to_claim(claim_id: str, body: ResponseRequest) -> dict[str, Any]:
        accepted_amount: Decimal | None = None
        if body.acceptedAmount is not None:
            try:
                accepted_amount = Decimal(body.acceptedAmount)
            except InvalidOperation:
                raise HTTPException(
                    status_code=422, detail="acceptedAmount 格式无效"
                ) from None

        with memory() as database:
            try:
                result = claim_service.transition_to_response(
                    database, claim_id,
                    operator_name=body.operatorName,
                    verdict=body.verdict,
                    accepted_amount=accepted_amount,
                    response_text=body.responseText,
                )
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="claim not found"
                ) from None
            except claim_service.InvalidClaimTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            value_module.refresh_invariant_claim_stats(database)
            return result

    @router.post("/{claim_id}/recovery")
    def recover_claim(claim_id: str, body: RecoveryRequest) -> dict[str, Any]:
        try:
            recovered_amount = Decimal(body.recoveredAmount)
        except InvalidOperation:
            raise HTTPException(
                status_code=422, detail="recoveredAmount 格式无效"
            ) from None

        with memory() as database:
            try:
                result = claim_service.transition_to_recovered(
                    database, claim_id,
                    operator_name=body.operatorName,
                    recovered_amount=recovered_amount,
                )
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="claim not found"
                ) from None
            except claim_service.InvalidClaimTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            value_module.refresh_invariant_claim_stats(database)
            return result

    @router.post("/detect")
    def detect_claims(body: DetectRequest) -> list[dict[str, Any]]:
        with memory() as database:
            return detect_module.detect_claims(
                database,
                period_id=body.periodId,
                store_id=body.storeId,
            )

    return router
