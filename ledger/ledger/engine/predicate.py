"""过滤条件求值。

模型层的 `Predicate` 出现在两个地方，语义必须一致：

  指标的 `where`   决定哪些源数据行纳入这个科目
  指标的 `expect`  决定覆盖率的分母是脊柱上的哪些订单

所以编译逻辑放在这里共用，而不是各写一份。
"""

from __future__ import annotations

import polars as pl

from ..model.schema import Predicate


class PredicateError(Exception):
    """条件引用了不存在的字段，或用了未知算子。"""


def compile_where(where: tuple[Predicate, ...], frame: pl.DataFrame) -> pl.Expr:
    """把一串条件编成一个布尔表达式。全部满足才为真。

    空值默认视为不满足：缺数据不等于符合条件。排除型的条件要反过来，见
    `Predicate.include_null`——「状态不是已取消」不该因为状态是空的就把这笔成本丢掉。
    """
    out = pl.lit(True)
    for p in where:
        out = out & _one(p, frame).fill_null(p.include_null)
    return out


def missing_fields(where: tuple[Predicate, ...], frame: pl.DataFrame) -> list[str]:
    """条件里引用了但表上没有的字段角色。"""
    return [p.field for p in where if p.field not in frame.columns]


def _one(p: Predicate, frame: pl.DataFrame) -> pl.Expr:
    if p.field not in frame.columns:
        raise PredicateError(f"过滤条件引用了不存在的字段角色 {p.field}")
    col = pl.col(p.field).cast(pl.Utf8)
    if p.op == "eq":
        return col == str(p.value)
    if p.op == "ne":
        return col != str(p.value)
    if p.op == "in":
        return col.is_in([str(v) for v in p.value])  # type: ignore[union-attr]
    if p.op == "not_in":
        return ~col.is_in([str(v) for v in p.value])  # type: ignore[union-attr]
    if p.op == "contains":
        return col.str.contains(str(p.value), literal=True)
    if p.op == "not_contains":
        return ~col.str.contains(str(p.value), literal=True)
    if p.op == "gt":
        return pl.col(p.field).cast(pl.Float64, strict=False) > float(p.value)  # type: ignore[arg-type]
    if p.op == "lt":
        return pl.col(p.field).cast(pl.Float64, strict=False) < float(p.value)  # type: ignore[arg-type]
    if p.op == "notnull":
        # 解析器把空单元格统一成空串，所以空串也算没值——否则「有运单号」这类
        # 条件会把一批空串当成有值，覆盖率的分母就虚高了。
        return col.is_not_null() & (col.str.strip_chars() != "")
    raise PredicateError(f"未知过滤算子 {p.op}")  # pragma: no cover
