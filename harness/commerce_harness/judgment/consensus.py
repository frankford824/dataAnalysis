from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import cast

from .cite_guard import CiteGuard, EvidenceLedger
from .models import (
    CriticAssessment,
    ReviewRecommendation,
    SuggestionCandidate,
    SuggestionKind,
)


class ConsensusEngine:
    """将投票汇总为建议，绝不返回自动执行授权。"""

    def __init__(self, *, cite_guard: CiteGuard | None = None) -> None:
        self._guard = cite_guard or CiteGuard()

    def recommend(
        self,
        candidates: Iterable[SuggestionCandidate],
        *,
        ledger: EvidenceLedger,
        critic_assessments: Iterable[CriticAssessment] = (),
        arbiter_candidate: SuggestionCandidate | None = None,
    ) -> ReviewRecommendation:
        votes = tuple(candidates)
        if not votes:
            raise ValueError("at least one candidate is required")
        residual_ids = {vote.residual_id for vote in votes}
        if len(residual_ids) != 1:
            raise ValueError("all candidates must target the same residual")
        critic_by_id = {item.suggestion_id: item for item in critic_assessments}
        survivors: list[SuggestionCandidate] = []
        reasons: list[str] = []
        for candidate in votes:
            guard = self._guard.verify(candidate, ledger)
            if not guard.valid:
                reasons.append(f"{candidate.suggestion_id}: citation guard failed")
                continue
            critic = critic_by_id.get(candidate.suggestion_id)
            if critic is not None and not critic.accepted_for_review:
                reasons.append(f"{candidate.suggestion_id}: critic veto")
                continue
            survivors.append(candidate)

        keys = [
            (cast(SuggestionKind, item.kind).value, item.category, item.action)
            for item in survivors
        ]
        counts = Counter(keys)
        agreed = (
            counts.most_common(1)[0][0]
            if counts and counts.most_common(1)[0][1] >= 2
            else None
        )
        if agreed is not None:
            action = agreed[2]
            outcome = "agreement_requires_human"
            reasons.append("independent suggestions agree; L0 still requires a human decision")
        elif arbiter_candidate is not None and self._guard.verify(arbiter_candidate, ledger).valid:
            action = arbiter_candidate.action
            outcome = "arbiter_suggestion_requires_human"
            survivors.append(arbiter_candidate)
            reasons.append("votes disagree; arbiter supplied advice only")
        else:
            action = None
            outcome = "disagreement_requires_human"
            reasons.append("no valid consensus")
        return ReviewRecommendation(
            residual_id=next(iter(residual_ids)),
            outcome=outcome,
            candidates=tuple(survivors),
            recommended_action=action,
            reasons=tuple(reasons),
        )
