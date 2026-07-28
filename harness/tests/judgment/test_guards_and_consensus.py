from __future__ import annotations

from dataclasses import replace

from commerce_harness.judgment.cite_guard import CiteGuard, EvidenceLedger
from commerce_harness.judgment.consensus import ConsensusEngine
from commerce_harness.judgment.critic import Critic
from commerce_harness.judgment.models import (
    EvidenceCitation,
    EvidenceRecord,
    SuggestionCandidate,
)


def record() -> EvidenceRecord:
    return EvidenceRecord(
        file_id="file-1",
        row_no=8,
        metric="received",
        period="2026-05",
        shop="SHOP-A",
        value="88.10",
        definition_id="received-v1",
    )


def candidate(
    suggestion_id: str,
    *,
    row_no: int = 8,
    action: str = "核对跨期",
) -> SuggestionCandidate:
    return SuggestionCandidate(
        suggestion_id=suggestion_id,
        residual_id="residual-1",
        kind="explanation",
        category="timing",
        action=action,
        rationale="引用行支持该假设",
        confidence="0.91",
        citations=(
            EvidenceCitation(
                file_id="file-1",
                row_no=row_no,
                metric="received",
                period="2026-05",
                shop="SHOP-A",
                value="88.10",
                definition_id="received-v1",
            ),
        ),
        source_model="model-a",
    )


def test_cite_guard_rejects_same_value_with_forged_row() -> None:
    ledger = EvidenceLedger([record()])
    result = CiteGuard().verify(candidate("bad", row_no=999), ledger)
    assert not result.valid
    assert result.checked_count == 1
    assert "row_no" in result.failures[0].reason


def test_consensus_stays_l0_even_when_two_models_agree() -> None:
    ledger = EvidenceLedger([record()])
    first = candidate("vote-1")
    second = replace(first, suggestion_id="vote-2", source_model="model-b")
    recommendation = ConsensusEngine().recommend([first, second], ledger=ledger)
    assert recommendation.outcome == "agreement_requires_human"
    assert recommendation.recommended_action == "核对跨期"
    assert recommendation.requires_human_review
    assert not recommendation.may_write_ledger


def test_critic_veto_removes_vote_but_never_decides() -> None:
    ledger = EvidenceLedger([record()])
    vote = candidate("vote-1")
    guard = CiteGuard().verify(vote, ledger)
    assessment = Critic().assess(vote, guard, model_veto=True, model_reason="证据不足")
    result = ConsensusEngine().recommend(
        [vote],
        ledger=ledger,
        critic_assessments=[assessment],
    )
    assert result.outcome == "disagreement_requires_human"
    assert result.recommended_action is None
    assert not result.may_write_ledger
