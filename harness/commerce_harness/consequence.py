"""Translate shadow-run metrics into plain-language consequences.

Shown the moment a reviewer selects an option: what would change, and
whether any previously matched books would be harmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class ConsequenceCopy:
    summary: str
    books_safe: bool
    details: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "booksSafe": self.books_safe,
            "details": list(self.details),
        }


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def translate_consequence(
    metrics: Mapping[str, Mapping[str, Any]] | Mapping[str, Any],
) -> ConsequenceCopy:
    """Turn experiment metrics into a one-screen consequence paragraph.

    Accepts either the full metric map ``{name: {before, after, delta}}``
    or a flat summary with the same keys at the top level.
    """

    def read(name: str, field: str = "delta") -> Decimal:
        entry = metrics.get(name)
        if isinstance(entry, Mapping) and field in entry:
            return _as_decimal(entry[field])
        if field == "after" and isinstance(entry, Mapping) and "after" in entry:
            return _as_decimal(entry["after"])
        if name in metrics and not isinstance(metrics.get(name), Mapping):
            return _as_decimal(metrics.get(name))
        return Decimal("0")

    newly = read("newly_unresolved_count", "after")
    if newly == 0:
        newly = read("newly_unresolved_count", "delta")
    reversals = read("major_reversal_count", "after")
    if reversals == 0:
        reversals = read("major_reversal_count", "delta")
    unresolved_delta = read("unresolved_amount_abs", "delta")
    auto_rate_delta = read("amount_weighted_auto_rate", "delta")

    books_safe = reversals <= 0 and newly <= 0
    details: list[str] = []

    if unresolved_delta < 0:
        details.append(
            f"对不上的金额会减少 {format(abs(unresolved_delta), 'f')} 元。"
        )
    elif unresolved_delta > 0:
        details.append(
            f"对不上的金额会增加 {format(unresolved_delta, 'f')} 元。"
        )
    else:
        details.append("对不上的金额总额不变。")

    if auto_rate_delta > 0:
        details.append("自动对上的比例会提高。")
    elif auto_rate_delta < 0:
        details.append("自动对上的比例会下降。")

    if newly > 0:
        details.append(f"会新出现 {int(newly)} 笔对不上的账。")
    else:
        details.append("不会新增多笔对不上的账。")

    if reversals > 0:
        details.append(
            f"有 {int(reversals)} 笔原来对上的账会被改坏——不建议这样定。"
        )
    else:
        details.append("原来对上的账一笔都不会被改。")

    prefix = "这样定是安全的：" if books_safe else "这样定有风险："
    summary = prefix + details[-1]

    return ConsequenceCopy(
        summary=summary,
        books_safe=books_safe,
        details=tuple(details),
    )
