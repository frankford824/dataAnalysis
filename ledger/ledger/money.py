"""Deterministic helpers for amounts that reach formal ledger outputs."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

CENT = Decimal("0.01")


def decimal_amount(value: object) -> Decimal:
    """Convert through text so binary-float noise never becomes ledger input."""
    if isinstance(value, Decimal):
        out = value
    else:
        try:
            out = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"无效金额：{value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"金额必须是有限数：{value!r}")
    return out


def sum_amounts(values: Iterable[object], *, cents: bool = True) -> Decimal:
    total = sum((decimal_amount(value) for value in values), Decimal(0))
    return total.quantize(CENT, rounding=ROUND_HALF_UP) if cents else total


def money_float(value: object) -> float:
    """Return the JSON-compatible representation of an exact cent amount."""
    return float(decimal_amount(value).quantize(CENT, rounding=ROUND_HALF_UP))
