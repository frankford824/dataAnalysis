from __future__ import annotations

from dataclasses import replace

import pytest

from commerce_harness.judgment.autonomy import AutonomyEvaluator, AutonomyPolicy, ReviewedOutcome
from commerce_harness.judgment.cite_guard import EvidenceLedger
from commerce_harness.judgment.consensus import ConsensusEngine
from commerce_harness.judgment.corrections import CorrectionBook, CorrectionEntry
from commerce_harness.judgment.gateway import (
    GatewayConfig,
    OpenAICompatibleGateway,
    ReplayTransport,
)
from commerce_harness.judgment.models import EvidenceCitation, EvidenceRecord, SuggestionCandidate
from commerce_harness.judgment.residual import ResidualJudge


class MalformedTransport:
    def post_json(self, url, *, headers, payload, timeout_seconds):
        del url, headers, payload, timeout_seconds
        return {"choices": []}


def evidence() -> EvidenceRecord:
    return EvidenceRecord("file-1", 1, "fee", "2026-05", "SHOP-A", "9.99", "fee-v1")


def vote(suggestion_id: str, action: str = "方案甲") -> SuggestionCandidate:
    item = evidence()
    return SuggestionCandidate(
        suggestion_id=suggestion_id,
        residual_id="residual-1",
        kind="classification",
        category="fee",
        action=action,
        rationale="等待人工确认",
        confidence="0.7",
        citations=(
            EvidenceCitation(
                item.file_id,
                item.row_no,
                item.metric,
                item.period,
                item.shop,
                item.value,
                item.definition_id,
            ),
        ),
        source_model="model",
    )


def test_gateway_config_and_malformed_response_fail_closed() -> None:
    config = GatewayConfig.from_env(
        {
            "FA_GATEWAY_BASE_URL": "https://gateway.invalid/v1/",
            "FA_GATEWAY_API_KEY": "key",
            "FA_GATEWAY_MAX_ATTEMPTS": "1",
        }
    )
    assert config.enabled
    result = OpenAICompatibleGateway(config, transport=MalformedTransport()).complete_json(
        purpose="test",
        model="model",
        messages=[{"role": "user", "content": "{}"}],
    )
    assert result.status == "error"
    assert "choices" in (result.reason or "")


def test_replay_rejects_unrecorded_request_and_exhaustion() -> None:
    transport = ReplayTransport(
        [{"request": {"expected": True}, "response": {"choices": []}}]
    )
    with pytest.raises(RuntimeError, match="does not match"):
        transport.post_json(
            "offline",
            headers={},
            payload={"expected": False},
            timeout_seconds=1,
        )
    with pytest.raises(RuntimeError, match="exhausted"):
        transport.post_json("offline", headers={}, payload={}, timeout_seconds=1)


def test_arbiter_is_still_only_a_human_review_suggestion() -> None:
    ledger = EvidenceLedger([evidence()])
    first = vote("one", "方案甲")
    second = vote("two", "方案乙")
    arbiter = vote("arbiter", "方案丙")
    result = ConsensusEngine().recommend(
        [first, second],
        ledger=ledger,
        arbiter_candidate=arbiter,
    )
    assert result.outcome == "arbiter_suggestion_requires_human"
    assert result.recommended_action == "方案丙"
    assert not result.may_write_ledger


def test_consensus_rejects_empty_or_cross_residual_votes() -> None:
    engine = ConsensusEngine()
    ledger = EvidenceLedger([evidence()])
    with pytest.raises(ValueError, match="at least one"):
        engine.recommend([], ledger=ledger)
    with pytest.raises(ValueError, match="same residual"):
        engine.recommend(
            [vote("one"), replace(vote("two"), residual_id="residual-2")],
            ledger=ledger,
        )


def test_invalid_candidate_schema_and_float_are_rejected() -> None:
    payload = vote("one").to_dict()
    payload["extra"] = True
    with pytest.raises(ValueError, match="schema mismatch"):
        SuggestionCandidate.from_mapping(payload, source_model="model")
    with pytest.raises(TypeError, match="float"):
        replace(vote("one"), confidence=0.5)
    with pytest.raises(ValueError, match="typed citations"):
        replace(vote("one"), rationale="模型自行声称金额为 9.99")


def test_disabled_residual_judge_returns_no_fake_advice() -> None:
    judge = ResidualJudge(OpenAICompatibleGateway(GatewayConfig()), model="model")
    assert judge.suggest({"residual": {"residual_id": "r1"}}) == ()


def test_autonomy_failure_reasons_and_duplicate_correction(tmp_path) -> None:
    outcome = ReviewedOutcome("fee", "2026-05", False, True, "2500")
    assessment = AutonomyEvaluator(
        AutonomyPolicy(minimum_reviews=2, exposure_cap="100")
    ).evaluate("fee", [outcome])
    assert assessment.recommended_level == "L0"
    assert assessment.major_error_count == 1
    assert any("major amount" in reason for reason in assessment.reasons)
    entry = CorrectionEntry(
        "c1",
        "s1",
        "r1",
        "fee",
        "fee",
        "2026-05",
        "SHOP-A",
        "模型建议",
        "人工决定",
        "复核纠正",
        "reviewer",
    )
    book = CorrectionBook(path=tmp_path / "book.jsonl")
    book.append(entry)
    with pytest.raises(ValueError, match="duplicate"):
        book.append(entry)
