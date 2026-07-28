"""Exact DECIMAL(38, 4) money handling.

`float` is rejected at the boundary.  In particular, constructing a Decimal
from a binary float is never an accepted conversion path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
    localcontext,
)
from typing import cast

MONEY_PRECISION = 38
MONEY_SCALE = 4
MONEY_QUANTUM = Decimal("0.0001")
DECIMAL38_4_MAX = Decimal("9" * (MONEY_PRECISION - MONEY_SCALE) + "." + "9" * MONEY_SCALE)
DECIMAL38_4_MIN = Decimal(
    "-" + "9" * (MONEY_PRECISION - MONEY_SCALE) + "." + "9" * MONEY_SCALE
)

_DECIMAL_TEXT = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


class MoneyError(ValueError):
    """Base error for invalid or out-of-range monetary input."""


class MoneyPrecisionError(MoneyError):
    """Raised when a value does not fit DECIMAL(38, 4)."""


@dataclass(frozen=True, slots=True)
class MoneyValue:
    """A normalized amount plus an auditable conversion description."""

    amount: Decimal
    source_text: str
    source_scale: int
    strategy: str

    def storage_text(self) -> str:
        return format(self.amount, f".{MONEY_SCALE}f")


def _coerce_decimal_text(value: str | int | Decimal) -> tuple[str, str]:
    if isinstance(value, bool):
        raise TypeError("bool is not a monetary input")
    if isinstance(value, float):
        raise TypeError("binary float money is forbidden; provide original text")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyError("money must be finite")
        return str(value), "decimal"
    if isinstance(value, int):
        return str(value), "integer"
    if not isinstance(value, str):
        raise TypeError("money must be supplied as str, int, or Decimal")

    text = value.strip()
    if not text:
        raise MoneyError("money text is empty")

    # Parenthesized negatives and grouping commas occur in exported statements.
    parenthesized = text.startswith("(") and text.endswith(")")
    if parenthesized:
        text = "-" + text[1:-1].strip()
    text = text.replace(",", "")
    if not _DECIMAL_TEXT.fullmatch(text):
        raise MoneyError(f"invalid decimal money text: {value!r}")
    return text, "text"


def _source_scale(value: Decimal) -> int:
    return max(0, -cast(int, value.as_tuple().exponent))


def parse_money(
    value: str | int | Decimal,
    *,
    require_exact_scale: bool = False,
    rounding: str = ROUND_HALF_EVEN,
) -> MoneyValue:
    """Parse and normalize a value to DECIMAL(38, 4).

    More than four fractional digits are rounded using the explicit strategy
    unless ``require_exact_scale`` is true.  The returned object records that
    decision.  Values outside DECIMAL(38, 4) are rejected after quantization.
    """

    text, source_kind = _coerce_decimal_text(value)
    try:
        raw = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"invalid decimal money text: {text!r}") from exc
    if not raw.is_finite():
        raise MoneyError("money must be finite")

    scale = _source_scale(raw)
    if require_exact_scale and scale > MONEY_SCALE:
        raise MoneyPrecisionError(
            f"source scale {scale} exceeds DECIMAL(38, {MONEY_SCALE})"
        )

    try:
        with localcontext() as context:
            context.prec = MONEY_PRECISION + max(scale, MONEY_SCALE) + 8
            normalized = raw.quantize(MONEY_QUANTUM, rounding=rounding)
    except InvalidOperation as exc:
        raise MoneyPrecisionError("money cannot be represented as DECIMAL(38, 4)") from exc

    if normalized < DECIMAL38_4_MIN or normalized > DECIMAL38_4_MAX:
        raise MoneyPrecisionError("money exceeds DECIMAL(38, 4) range")

    strategy = (
        f"{source_kind}:exact_scale"
        if scale <= MONEY_SCALE
        else f"{source_kind}:quantize_{rounding}"
    )
    return MoneyValue(normalized, text, scale, strategy)


def amount(value: str | int | Decimal) -> Decimal:
    """Return only the normalized Decimal amount."""

    return parse_money(value).amount


def sum_money(values: Iterable[str | int | Decimal]) -> Decimal:
    """Deterministically sum monetary values at higher precision, then quantize."""

    with localcontext() as context:
        context.prec = MONEY_PRECISION + 8
        total = sum((amount(value) for value in values), Decimal("0.0000"))
    return parse_money(total).amount


def negate_money(value: str | int | Decimal) -> Decimal:
    """Negate without applying the process-wide Decimal precision context."""

    return parse_money(amount(value).copy_negate()).amount


def subtract_money(
    left: str | int | Decimal,
    right: str | int | Decimal,
) -> Decimal:
    """Subtract DECIMAL(38, 4) values without an intermediate 28-digit result."""

    return sum_money((left, amount(right).copy_negate()))
