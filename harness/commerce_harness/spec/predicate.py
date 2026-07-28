"""Closed predicate DSL for invariant scoping and rule selection.

NO regex.  Max nesting depth 3, max 32 leaves.  Field vocabulary is
restricted to CanonicalRow fields plus ``attributes.*``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_MAX_DEPTH = 3
_MAX_LEAVES = 32

CANONICAL_FIELDS = frozenset({
    "dataset_kind",
    "source_type",
    "side",
    "business_key",
    "cash_bridge_key",
    "occurred_at",
    "amount",
    "period_key",
    "evidence_row",
    "source_name",
    "settlement_batch_id",
    "source_member",
    "source_sheet",
    "metric",
    "sku",
})

LEAF_OPS = frozenset({
    "eq", "ne", "in", "not_in",
    "prefix", "suffix", "contains",
    "range", "sign", "is_null", "not_null",
    "matches_shape",
})

COMBINATORS = frozenset({"all_of", "any_of", "none_of"})

_SHAPE_TOKEN = re.compile(
    r"D\{\d+(?:,\d+)?\}|A\{\d+(?:,\d+)?\}|[^DA{}]+"
)

_SIGN_VALUES = frozenset({"positive", "negative", "zero"})


class Predicate:
    """Opaque node; use ``evaluate_predicate`` to test against a row."""

    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: Any) -> None:
        self.kind = kind
        self.payload = payload

    def __repr__(self) -> str:
        return f"Predicate({self.kind!r}, {self.payload!r})"


def _valid_field(name: str) -> bool:
    if name in CANONICAL_FIELDS:
        return True
    return name.startswith("attributes.") and len(name) > len("attributes.")


def _parse_shape(pattern: str) -> list[tuple[str, str, int, int]]:
    tokens = _SHAPE_TOKEN.findall(pattern)
    result: list[tuple[str, str, int, int]] = []
    for token in tokens:
        if token.startswith("D{"):
            inner = token[2:-1]
            if "," in inner:
                lo, hi = inner.split(",", 1)
                result.append(("digit", token, int(lo), int(hi)))
            else:
                n = int(inner)
                result.append(("digit", token, n, n))
        elif token.startswith("A{"):
            inner = token[2:-1]
            if "," in inner:
                lo, hi = inner.split(",", 1)
                result.append(("alpha", token, int(lo), int(hi)))
            else:
                n = int(inner)
                result.append(("alpha", token, n, n))
        else:
            result.append(("literal", token, len(token), len(token)))
    return result


def _match_shape(value: str, pattern: str) -> bool:
    """Match a value against a shape pattern with full backtracking.

    Greedy consumption would reject valid values: for ``D{1,4}-D{2}`` against
    ``1234-56`` a greedy digit run swallows the whole prefix and then fails on
    the literal, even though a shorter run matches.
    """
    parts = _parse_shape(pattern)

    def _matches(part_index: int, pos: int) -> bool:
        if part_index == len(parts):
            return pos == len(value)
        kind, raw, lo, hi = parts[part_index]
        if kind == "literal":
            if not value.startswith(raw, pos):
                return False
            return _matches(part_index + 1, pos + lo)

        predicate = str.isdigit if kind == "digit" else str.isalpha
        available = 0
        while (
            pos + available < len(value)
            and available < hi
            and predicate(value[pos + available])
        ):
            available += 1
        # Try the longest run first, then shorter ones, so a following literal
        # still gets a chance to match.
        for length in range(available, lo - 1, -1):
            if _matches(part_index + 1, pos + length):
                return True
        return False

    return _matches(0, 0)


def _count_leaves(node: dict[str, Any]) -> int:
    for comb in COMBINATORS:
        if comb in node:
            return sum(_count_leaves(child) for child in node[comb])
    return 1


def _check_depth(node: dict[str, Any], depth: int) -> None:
    for comb in COMBINATORS:
        if comb in node:
            if depth >= _MAX_DEPTH:
                raise ValueError(f"predicate nesting depth exceeds {_MAX_DEPTH}")
            for child in node[comb]:
                _check_depth(child, depth + 1)
            return


def _parse_leaf(node: dict[str, Any]) -> Predicate:
    field = node.get("field")
    op = node.get("op")
    if not isinstance(op, str) or op not in LEAF_OPS:
        raise ValueError(f"invalid predicate op: {op!r}")
    if op in ("is_null", "not_null"):
        if not isinstance(field, str) or not _valid_field(field):
            raise ValueError(f"invalid predicate field: {field!r}")
        return Predicate("leaf", {"field": field, "op": op})
    value = node.get("value")
    if not isinstance(field, str) or not _valid_field(field):
        raise ValueError(f"invalid predicate field: {field!r}")
    if op == "sign" and value not in _SIGN_VALUES:
        raise ValueError(f"sign predicate value must be one of {_SIGN_VALUES}")
    if op in ("in", "not_in") and not isinstance(value, list):
        raise ValueError(f"'{op}' predicate value must be a list")
    if op == "range" and (
        not isinstance(value, dict) or not {"min", "max"} <= value.keys()
    ):
        raise ValueError("range predicate value must have 'min' and 'max'")
    if op == "matches_shape":
        if not isinstance(value, str):
            raise ValueError("matches_shape value must be a string pattern")
        _parse_shape(value)
    return Predicate("leaf", {"field": field, "op": op, "value": value})


def _parse_node(node: dict[str, Any]) -> Predicate:
    if not isinstance(node, dict):
        raise ValueError(f"predicate node must be a dict, got {type(node).__name__}")
    for comb in COMBINATORS:
        if comb in node:
            children = node[comb]
            if not isinstance(children, list) or len(children) == 0:
                raise ValueError(f"'{comb}' must contain a non-empty list")
            return Predicate(comb, [_parse_node(child) for child in children])
    return _parse_leaf(node)


def parse_predicate(raw: dict[str, Any]) -> Predicate:
    """Parse a dict into a validated ``Predicate`` tree.

    Raises ``ValueError`` on any structural or vocabulary violation.
    """
    if not isinstance(raw, dict):
        raise ValueError("predicate must be a dict")
    _check_depth(raw, 0)
    leaves = _count_leaves(raw)
    if leaves > _MAX_LEAVES:
        raise ValueError(f"predicate has {leaves} leaves, max is {_MAX_LEAVES}")
    return _parse_node(raw)


def _get_field(row: dict[str, Any], field: str) -> Any:
    if field.startswith("attributes."):
        attrs = row.get("attributes", {})
        if isinstance(attrs, dict):
            return attrs.get(field[len("attributes."):])
        return None
    return row.get(field)


def _eval_leaf(payload: dict[str, Any], row: dict[str, Any]) -> bool:
    field = payload["field"]
    op = payload["op"]
    actual = _get_field(row, field)

    if op == "is_null":
        return actual is None
    if op == "not_null":
        return actual is not None
    value = payload["value"]
    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "in":
        return actual in value
    if op == "not_in":
        return actual not in value
    if op == "prefix":
        return isinstance(actual, str) and actual.startswith(value)
    if op == "suffix":
        return isinstance(actual, str) and actual.endswith(value)
    if op == "contains":
        return isinstance(actual, str) and value in actual
    if op == "sign":
        try:
            d = Decimal(str(actual))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if value == "positive":
            return d > 0
        if value == "negative":
            return d < 0
        return d == 0
    if op == "range":
        if actual is None:
            return False
        try:
            d = Decimal(str(actual))
        except (InvalidOperation, TypeError, ValueError):
            return False
        lo = Decimal(str(value["min"]))
        hi = Decimal(str(value["max"]))
        return lo <= d <= hi
    if op == "matches_shape":
        return isinstance(actual, str) and _match_shape(actual, value)
    return False


def evaluate_predicate(pred: Predicate, row: dict[str, Any]) -> bool:
    """Evaluate a parsed ``Predicate`` against a row dict."""
    if pred.kind == "leaf":
        return _eval_leaf(pred.payload, row)
    if pred.kind == "all_of":
        return all(evaluate_predicate(child, row) for child in pred.payload)
    if pred.kind == "any_of":
        return any(evaluate_predicate(child, row) for child in pred.payload)
    if pred.kind == "none_of":
        return not any(evaluate_predicate(child, row) for child in pred.payload)
    raise ValueError(f"unknown predicate kind: {pred.kind!r}")


_OP_CN = {
    "eq": "等于",
    "ne": "不等于",
    "in": "属于",
    "not_in": "不属于",
    "prefix": "以…开头",
    "suffix": "以…结尾",
    "contains": "包含",
    "range": "在范围内",
    "sign": "符号为",
    "is_null": "为空",
    "not_null": "不为空",
    "matches_shape": "形状匹配",
}

_SIGN_CN = {"positive": "正", "negative": "负", "zero": "零"}


def predicate_to_chinese(pred: Predicate) -> str:
    """Translate a ``Predicate`` tree into human-readable Chinese."""
    if pred.kind == "leaf":
        p = pred.payload
        field = p["field"]
        op = p["op"]
        op_cn = _OP_CN.get(op, op)
        if op in ("is_null", "not_null"):
            return f"{field} {op_cn}"
        value = p["value"]
        if op == "sign":
            return f"{field} 符号为{_SIGN_CN.get(value, value)}"
        if op == "range":
            return f"{field} 在 [{value['min']}, {value['max']}] 范围内"
        if op in ("in", "not_in"):
            items = ", ".join(str(v) for v in value)
            return f"{field} {op_cn} [{items}]"
        return f"{field} {op_cn} {value!r}"
    if pred.kind == "all_of":
        parts = [predicate_to_chinese(c) for c in pred.payload]
        return "（" + " 且 ".join(parts) + "）"
    if pred.kind == "any_of":
        parts = [predicate_to_chinese(c) for c in pred.payload]
        return "（" + " 或 ".join(parts) + "）"
    if pred.kind == "none_of":
        parts = [predicate_to_chinese(c) for c in pred.payload]
        return "以下全不成立：（" + "、".join(parts) + "）"
    return str(pred)
