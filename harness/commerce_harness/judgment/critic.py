from __future__ import annotations

from .models import CriticAssessment, GuardResult, SuggestionCandidate


class Critic:
    """只有否决权；不会把建议升级为决定。"""

    def assess(
        self,
        candidate: SuggestionCandidate,
        guard_result: GuardResult,
        *,
        model_veto: bool = False,
        model_reason: str = "",
    ) -> CriticAssessment:
        if not guard_result.valid:
            return CriticAssessment(
                suggestion_id=candidate.suggestion_id,
                accepted_for_review=False,
                reason="citation guard rejected the candidate",
            )
        if model_veto:
            return CriticAssessment(
                suggestion_id=candidate.suggestion_id,
                accepted_for_review=False,
                reason=model_reason or "critic vetoed the evidence-to-conclusion link",
            )
        return CriticAssessment(
            suggestion_id=candidate.suggestion_id,
            accepted_for_review=True,
            reason=model_reason or "no critic objection; human review is still required",
        )

