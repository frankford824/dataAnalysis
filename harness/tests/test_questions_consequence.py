from __future__ import annotations

from decimal import Decimal

from commerce_harness.consequence import translate_consequence
from commerce_harness.questions import generate_review_question


class _DisabledGateway:
    def complete_json(self, **kwargs):  # noqa: ANN003
        from commerce_harness.judgment.models import GatewayResult

        return GatewayResult(status="disabled", model="none", content=None)


def test_fallback_question_is_plain_language() -> None:
    question = generate_review_question(
        _DisabledGateway(),  # type: ignore[arg-type]
        model="none",
        reason_code="missing_side",
        amount=Decimal("542632.44"),
        count=27771,
        explanation=None,
        ledger=None,
    )
    assert question.fallback is True
    assert "27771" in question.what or "27,771" in question.what or "笔" in question.what
    assert "不变量" not in question.what
    assert "残差" not in question.why
    assert any(option.recommended for option in question.options)


def test_consequence_flags_unsafe_reversals() -> None:
    copy = translate_consequence(
        {
            "unresolved_amount_abs": {
                "before": "100",
                "after": "40",
                "delta": "-60",
            },
            "amount_weighted_auto_rate": {
                "before": "0.5",
                "after": "0.8",
                "delta": "0.3",
            },
            "newly_unresolved_count": {"before": "0", "after": "0", "delta": "0"},
            "major_reversal_count": {"before": "0", "after": "2", "delta": "2"},
        }
    )
    assert copy.books_safe is False
    assert "改坏" in copy.summary or any("改坏" in item for item in copy.details)


def test_consequence_safe_path() -> None:
    copy = translate_consequence(
        {
            "unresolved_amount_abs": {"delta": "-100"},
            "amount_weighted_auto_rate": {"delta": "0.1"},
            "newly_unresolved_count": {"after": "0"},
            "major_reversal_count": {"after": "0"},
        }
    )
    assert copy.books_safe is True
    assert any("不会被改" in item for item in copy.details)
