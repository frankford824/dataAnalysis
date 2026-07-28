"""Adjudication corpus: situation fingerprints, persistence, and promotion detection."""

from .external import record_external_verdict
from .fingerprint import (
    SERVICE_FEE_TERMS,
    situation_fingerprint,
)
from .persist import (
    insert_case,
    record_review_as_case,
    upsert_fingerprint,
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
    "insert_case",
    "record_external_verdict",
    "record_review_as_case",
    "situation_fingerprint",
    "upsert_fingerprint",
]
