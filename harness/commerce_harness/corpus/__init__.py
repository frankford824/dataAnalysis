"""Adjudication corpus: situation fingerprints and promotion detection."""

from .fingerprint import (
    SERVICE_FEE_TERMS,
    situation_fingerprint,
)
from .promote import (
    AdjudicationCase,
    PromotionCandidate,
    detect_promotion_candidates,
)

__all__ = [
    "AdjudicationCase",
    "PromotionCandidate",
    "SERVICE_FEE_TERMS",
    "detect_promotion_candidates",
    "situation_fingerprint",
]
