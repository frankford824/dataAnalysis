"""Three-tier trust classification replacing the all-or-nothing gate.

Tiers:
- ``certified``: all blocking invariants pass, no material unresolved
- ``partial``: non-blocking violations or incomplete coverage
- ``blocked``: blocking invariant violations or material unresolved
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum


class TrustTier(StrEnum):
    CERTIFIED = "certified"
    PARTIAL = "partial"
    BLOCKED = "blocked"


_DEFAULT_UNEXPLAINED_THRESHOLD = Decimal("0.05")


def decide_trust_tier(
    *,
    blocking_violations: int,
    material_unresolved: int,
    unexplained_ratio: Decimal,
    incomplete_components: int,
    unexplained_threshold: Decimal = _DEFAULT_UNEXPLAINED_THRESHOLD,
) -> TrustTier:
    """Decide trust tier from evaluation state.

    Parameters
    ----------
    blocking_violations
        Count of invariants with ``blocks_certification=True`` that are violated.
    material_unresolved
        Count of material-amount unresolved balances.
    unexplained_ratio
        Fraction of total amount that is unexplained (0–1 scale).
    incomplete_components
        Number of profit/cost components not yet covered.
    unexplained_threshold
        Maximum ``unexplained_ratio`` for ``certified`` (default 5%).
    """
    if isinstance(unexplained_ratio, float):
        raise TypeError("unexplained_ratio must be Decimal, not float")

    if blocking_violations > 0:
        return TrustTier.BLOCKED
    if material_unresolved > 0:
        return TrustTier.BLOCKED

    if unexplained_ratio > unexplained_threshold:
        return TrustTier.PARTIAL
    if incomplete_components > 0:
        return TrustTier.PARTIAL

    return TrustTier.CERTIFIED
