"""Conservation and reproducibility gates for deterministic outputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .money import amount, subtract_money, sum_money


class InvariantViolation(AssertionError):
    pass


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (set, frozenset)):
        canonical_items = [_canonical(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported checksum value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deterministic_checksum(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def assert_same_checksum(left: Any, right: Any) -> str:
    left_checksum = deterministic_checksum(left)
    right_checksum = deterministic_checksum(right)
    if left_checksum != right_checksum:
        raise InvariantViolation(
            f"deterministic checksum mismatch: {left_checksum} != {right_checksum}"
        )
    return left_checksum


def assert_row_count(expected: int, actual: int) -> None:
    if expected != actual:
        raise InvariantViolation(f"row count mismatch: expected {expected}, got {actual}")


def assert_amount_conserved(
    inputs: Iterable[str | int | Decimal],
    outputs: Iterable[str | int | Decimal],
    *,
    tolerance: str | Decimal = "0.0000",
) -> None:
    input_total = sum_money(inputs)
    output_total = sum_money(outputs)
    if abs(subtract_money(input_total, output_total)) > abs(amount(tolerance)):
        raise InvariantViolation(
            f"amount is not conserved: inputs={input_total}, outputs={output_total}"
        )


def assert_detail_matches_summary(
    details: Iterable[str | int | Decimal],
    summary: str | int | Decimal,
    *,
    tolerance: str | Decimal = "0.0000",
) -> None:
    detail_total = sum_money(details)
    summary_amount = amount(summary)
    if abs(subtract_money(detail_total, summary_amount)) > abs(amount(tolerance)):
        raise InvariantViolation(
            f"detail total {detail_total} does not equal summary {summary_amount}"
        )
