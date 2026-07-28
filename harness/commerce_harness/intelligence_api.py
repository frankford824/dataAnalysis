"""API routes for model understanding, review questions, and consequences."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from commerce_harness.consequence import translate_consequence
from commerce_harness.judgment.cite_guard import EvidenceLedger
from commerce_harness.judgment.gateway import OpenAICompatibleGateway
from commerce_harness.judgment.models import EvidenceCitation, EvidenceRecord
from commerce_harness.llm_runtime import RuntimeLlmStore
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.questions import generate_review_question
from commerce_harness.understanding import (
    freeze_understanding,
    propose_file_understanding,
    record_proposal,
)
from commerce_harness.workbench import WorkbenchPaths


class UnderstandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    originalName: str
    headers: list[str] = Field(min_length=1, max_length=512)
    sampleRows: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    fallbackSourceKind: str | None = None


class FreezeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operatorName: str = Field(min_length=1, max_length=200)
    # Only the id of a proposal core itself produced; never its content.
    proposalId: str = Field(min_length=1, max_length=128)


class QuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasonCode: str | None = None
    amount: str | None = None
    count: int = Field(default=1, ge=1, le=1_000_000)
    explanation: dict[str, Any] | None = None
    # Preferred form: name the open items and let the kernel supply the money.
    unresolvedIds: list[str] | None = None


def _question_subject(
    database: DuckDBMemory, unresolved_ids: list[str]
) -> tuple[str, Decimal, int, EvidenceLedger, tuple[EvidenceCitation, ...]]:
    """Derive the question's numbers and evidence from stored rows only."""
    placeholders = ", ".join("?" for _ in unresolved_ids)
    rows = database.execute(
        f"""
        SELECT unresolved.unresolved_id, unresolved.reason_code, unresolved.amount,
               balance.period_id, balance.contract_id,
               binding.snapshot_id, binding.row_no, binding.source_value,
               binding.rule_version_id
        FROM unresolved_balance AS unresolved
        JOIN reconciliation_balance AS balance
             ON balance.balance_id = unresolved.balance_id
        LEFT JOIN evidence_binding AS binding
             ON binding.evidence_id = unresolved.evidence_id
        WHERE unresolved.unresolved_id IN ({placeholders})
        ORDER BY unresolved.unresolved_id, binding.ordinal
        """,
        list(unresolved_ids),
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="找不到这些待定项")

    reason_codes = {str(row[1]) for row in rows}
    if len(reason_codes) > 1:
        raise HTTPException(
            status_code=400, detail="一次只能问同一类待定项"
        )
    seen: dict[str, Decimal] = {}
    citations: list[EvidenceCitation] = []
    for row in rows:
        seen.setdefault(str(row[0]), abs(Decimal(str(row[2]))))
        if row[5] is None or row[6] is None:
            continue
        citations.append(
            EvidenceCitation(
                file_id=str(row[5]),
                row_no=int(row[6]),
                metric=str(row[1]),
                period=str(row[3]),
                shop=str(row[4]),
                value=Decimal(str(row[7] if row[7] is not None else row[2])),
                definition_id=str(row[8] or ""),
            )
        )
    unique_citations = {citation.identity: citation for citation in citations}
    ledger = EvidenceLedger(
        EvidenceRecord(
            file_id=citation.file_id,
            row_no=citation.row_no,
            metric=citation.metric,
            period=citation.period,
            shop=citation.shop,
            value=citation.value,
            definition_id=citation.definition_id,
        )
        for citation in unique_citations.values()
    )
    return (
        reason_codes.pop(),
        sum(seen.values(), Decimal("0")),
        len(seen),
        ledger,
        tuple(unique_citations.values()),
    )


class ConsequenceBody(BaseModel):
    metrics: dict[str, Any]


def build_intelligence_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])
    llm_store = RuntimeLlmStore(workbench.root)
    gateway = OpenAICompatibleGateway(runtime_store=llm_store)

    def _model() -> str:
        status = llm_store.public_status()
        return status.selected_model or "default"

    @router.post("/understand")
    def understand(body: UnderstandBody) -> dict[str, Any]:
        proposal = propose_file_understanding(
            gateway,
            model=_model(),
            headers=body.headers,
            sample_rows=body.sampleRows,
            original_name=body.originalName,
            fallback_source_kind=body.fallbackSourceKind,
        )
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            stored = record_proposal(database, proposal)
        return stored.to_dict()

    @router.post("/understand/freeze")
    def freeze(body: FreezeBody) -> dict[str, Any]:
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            try:
                frozen = freeze_understanding(
                    database,
                    body.proposalId,
                    operator_name=body.operatorName,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return frozen.to_dict()

    @router.post("/questions")
    def questions(body: QuestionBody) -> dict[str, Any]:
        ledger: EvidenceLedger | None = None
        citations: tuple[EvidenceCitation, ...] = ()
        if body.unresolvedIds:
            with DuckDBMemory(workbench.database) as database:
                database.initialize()
                reason_code, amount, count, ledger, citations = _question_subject(
                    database, body.unresolvedIds
                )
        else:
            if not body.reasonCode or body.amount is None:
                raise HTTPException(
                    status_code=422,
                    detail="需要 unresolvedIds，或同时给出 reasonCode 与 amount",
                )
            reason_code = body.reasonCode
            try:
                amount = Decimal(body.amount)
            except InvalidOperation as exc:
                raise HTTPException(
                    status_code=422, detail="amount 格式无效"
                ) from exc
            if not amount.is_finite():
                raise HTTPException(status_code=422, detail="amount 格式无效")
            count = body.count

        question = generate_review_question(
            gateway,
            model=_model(),
            reason_code=reason_code,
            amount=amount,
            count=count,
            explanation=body.explanation,
            ledger=ledger,
            citations=citations,
        )
        return question.to_dict()

    @router.post("/consequence")
    def consequence(body: ConsequenceBody) -> dict[str, Any]:
        return translate_consequence(body.metrics).to_dict()

    return router
