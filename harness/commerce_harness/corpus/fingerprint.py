"""Situation fingerprinting from structural features only.

No raw values appear in the fingerprint — deidentification is built in at
the source rather than bolted on at export.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum

SERVICE_FEE_TERMS: dict[str, str] = {
    "软件服务费": "service_fee_term",
    "技术服务费": "service_fee_term",
    "佣金": "commission_term",
    "手续费": "handling_fee_term",
    "平台服务费": "platform_service_fee_term",
    "广告费": "advertising_fee_term",
    "推广费": "promotion_fee_term",
    "运费": "freight_term",
    "物流费": "logistics_fee_term",
    "保证金": "deposit_term",
    "退款": "refund_term",
    "赔付": "compensation_term",
}

TIMING_PATTERNS = frozenset({
    "same_period",
    "cross_period",
    "late_arrival",
    "unknown",
})


def _amount_log_bucket(value: Any) -> str:
    """Map an amount to a log-scale bucket like '1e2'.  No exact amount.

    The exponent is derived from the Decimal digit count rather than
    ``math.log10``, both to keep floats out of the pipeline and to avoid
    binary-rounding drift at bucket edges (a value of exactly 100 must never
    land in ``1e1``).
    """
    try:
        magnitude = abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return "unknown"
    if not magnitude.is_finite():
        return "unknown"
    if magnitude == 0:
        return "zero"
    digits, exponent = magnitude.as_tuple()[1:]
    if not isinstance(exponent, int):
        return "unknown"
    # adjusted() equivalent: position of the most significant digit.
    return f"1e{len(digits) - 1 + exponent}"


def _dominant_magnitude(amounts: Sequence[Any]) -> Decimal:
    """Largest absolute amount, ignoring values that will not parse."""
    largest = Decimal("0")
    for value in amounts:
        try:
            magnitude = abs(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if magnitude > largest:
            largest = magnitude
    return largest


def _sign_pattern(amounts: Sequence[Any]) -> str:
    """Classify the sign pattern of a set of amounts."""
    has_pos = False
    has_neg = False
    has_zero = False
    for a in amounts:
        try:
            d = Decimal(str(a))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if d > 0:
            has_pos = True
        elif d < 0:
            has_neg = True
        else:
            has_zero = True

    parts = []
    if has_pos:
        parts.append("positive")
    if has_neg:
        parts.append("negative")
    if has_zero:
        parts.append("zero")
    return "+".join(parts) if parts else "empty"


def _field_shapes(row: dict[str, Any]) -> list[str]:
    """Extract structural shapes from string fields."""
    shapes: list[str] = []
    for key, val in sorted(row.items()):
        if not isinstance(val, str) or key in ("amount",):
            continue
        if val.isdigit():
            shapes.append(f"{key}:D{{{len(val)}}}")
        elif val.isalpha():
            shapes.append(f"{key}:A{{{len(val)}}}")
    return shapes


def _classify_description(text: str | None) -> str | None:
    """Map business description to word class via SERVICE_FEE_TERMS."""
    if not text:
        return None
    for term, cls in SERVICE_FEE_TERMS.items():
        if term in text:
            return cls
    return "other"


def situation_fingerprint(
    *,
    source_kinds: Sequence[str],
    amounts: Sequence[Any],
    field_shapes: Sequence[str] | None = None,
    business_description: str | None = None,
    invariant_family: str | None = None,
    timing_pattern: str = "unknown",
    row: dict[str, Any] | None = None,
) -> str:
    """Compute a structural situation fingerprint (SHA-256).

    The fingerprint contains NO raw values — only structural features.
    """
    features: dict[str, Any] = {
        "source_kinds": sorted(set(source_kinds)),
        "sign_pattern": _sign_pattern(amounts),
        # Bucketed on the dominant magnitude, not on whichever amount happened
        # to be listed first: the same situation must fingerprint identically
        # regardless of row order.
        "amount_log_bucket": _amount_log_bucket(_dominant_magnitude(amounts)),
    }

    if field_shapes:
        features["field_shapes"] = sorted(field_shapes)
    elif row:
        features["field_shapes"] = _field_shapes(row)

    desc_class = _classify_description(business_description)
    if desc_class:
        features["business_description_class"] = desc_class

    if invariant_family:
        features["invariant_family"] = invariant_family

    if timing_pattern in TIMING_PATTERNS:
        features["timing_pattern"] = timing_pattern

    return deterministic_checksum(features)
