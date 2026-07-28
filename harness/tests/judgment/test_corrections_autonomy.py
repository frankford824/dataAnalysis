from __future__ import annotations

from commerce_harness.judgment.autonomy import (
    AutonomyEvaluator,
    AutonomyPolicy,
    ReviewedOutcome,
)
from commerce_harness.judgment.context_assembler import ContextAssembler
from commerce_harness.judgment.corrections import CorrectionBook, CorrectionEntry


def correction() -> CorrectionEntry:
    return CorrectionEntry(
        correction_id="correction-1",
        suggestion_id="suggestion-1",
        residual_id="residual-1",
        category="timing",
        metric="received",
        period="2026-05",
        shop="SHOP-A",
        model_action="当期确认",
        human_action="跨期挂账",
        human_reason="实际到账日属于次月",
        decided_by="reviewer",
    )


def test_correction_book_round_trip_and_retrieval(tmp_path) -> None:
    path = tmp_path / "corrections.jsonl"
    book = CorrectionBook(path=path)
    book.append(correction())
    loaded = CorrectionBook.load(path)
    assert loaded.search(category="timing", metric="received") == (correction(),)
    assert loaded.overturned_counts() == {"timing": 1}


def test_autonomy_can_recommend_but_runtime_remains_l0() -> None:
    policy = AutonomyPolicy(
        level="L2",
        minimum_periods=2,
        minimum_reviews=2,
        required_precision="0.995",
        exposure_cap="1000",
    )
    outcomes = [
        ReviewedOutcome("timing", "2026-04", True, False, "100"),
        ReviewedOutcome("timing", "2026-05", True, False, "100"),
    ]
    result = AutonomyEvaluator(policy).evaluate("timing", outcomes)
    assert result.recommended_level == "L2"
    assert result.effective_level == "L0"
    assert result.requires_governance_approval
    assert not result.may_write_ledger


def test_context_assembler_keeps_priority_order_when_trimming() -> None:
    assembler = ContextAssembler(max_chars=1_000)
    context = assembler.assemble(
        residual={"residual_id": "r1", "description": "核心残差"},
        corrections=[{"id": "c1", "text": "a" * 200}],
        rules=[{"id": "rule1", "text": "b" * 900}],
        related_rows=[{"id": "row1", "text": "c" * 900}],
    )
    assert context["residual"]["residual_id"] == "r1"
    assert context["corrections"]
    assert context["rules"] == []
    assert context["related_rows"] == []

