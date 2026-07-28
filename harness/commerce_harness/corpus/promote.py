"""Promotion candidate detection: fingerprint convergence -> rule draft.

When the same fingerprint + same disposition appears across >=N cases
and >=2 periods, it becomes a candidate for promotion to a rule, backed
by an experiment proposal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_MIN_CASES = 3
DEFAULT_MIN_PERIODS = 2


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    fingerprint_id: str
    disposition_kind: str
    case_count: int
    distinct_periods: int
    posting_target: str | None
    draft_rule: dict[str, Any]
    experiment_proposal: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AdjudicationCase:
    case_id: str
    fingerprint_id: str
    disposition_kind: str
    posting_target: str | None
    period_id: str | None
    rationale: str


def detect_promotion_candidates(
    cases: Sequence[AdjudicationCase],
    *,
    min_cases: int = DEFAULT_MIN_CASES,
    min_periods: int = DEFAULT_MIN_PERIODS,
) -> list[PromotionCandidate]:
    """Detect fingerprints eligible for promotion to rules.

    A fingerprint qualifies when it has the same disposition across
    ``min_cases`` or more cases spanning ``min_periods`` or more periods.
    """
    groups: dict[tuple[str, str], list[AdjudicationCase]] = defaultdict(list)
    for case in cases:
        key = (case.fingerprint_id, case.disposition_kind)
        groups[key].append(case)

    candidates: list[PromotionCandidate] = []
    for (fp_id, disp), group in sorted(groups.items()):
        if len(group) < min_cases:
            continue
        periods = {c.period_id for c in group if c.period_id}
        if len(periods) < min_periods:
            continue

        posting_target = None
        for c in group:
            if c.posting_target:
                posting_target = c.posting_target
                break

        draft_rule = _build_draft_rule(fp_id, disp, posting_target, group)
        experiment_proposal = _build_experiment_proposal(fp_id, disp, group)

        candidates.append(PromotionCandidate(
            fingerprint_id=fp_id,
            disposition_kind=disp,
            case_count=len(group),
            distinct_periods=len(periods),
            posting_target=posting_target,
            draft_rule=draft_rule,
            experiment_proposal=experiment_proposal,
        ))

    return candidates


def _build_draft_rule(
    fingerprint_id: str,
    disposition_kind: str,
    posting_target: str | None,
    cases: list[AdjudicationCase],
) -> dict[str, Any]:
    if disposition_kind == "rule_candidate" and posting_target:
        return {
            "action": "route",
            "participation": "legal_single_sided",
            "posting_target": posting_target,
            "origin_fingerprint": fingerprint_id,
            "supporting_cases": len(cases),
            "rationale": cases[0].rationale if cases else "",
        }
    return {
        "action": "classify",
        "category": disposition_kind,
        "origin_fingerprint": fingerprint_id,
        "supporting_cases": len(cases),
    }


def _build_experiment_proposal(
    fingerprint_id: str,
    disposition_kind: str,
    cases: list[AdjudicationCase],
) -> dict[str, Any]:
    periods = sorted({c.period_id for c in cases if c.period_id})
    return {
        "hypothesis_kind": "rule_add",
        "origin_fingerprint": fingerprint_id,
        "disposition_kind": disposition_kind,
        "scope": {
            "periods": periods,
        },
        "proposed_by": "policy:promotion_detector",
    }
