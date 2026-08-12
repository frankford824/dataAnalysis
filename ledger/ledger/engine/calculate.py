"""原语六：核算。按公式树求值。

公式树是模型数据，引擎是求值器。算子集合刻意保持最小——实测全部 2288 个现有 DAX
度量值只用了 5 个函数（CALCULATE 1485、SUMX 1269、SUM 323、DIVIDE 90、ROUND 6），
零计算列、零时间智能函数，所以下面这几个算子足够覆盖。

时间归属规则由模型声明（按下单日 / 按发生日），引擎执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..model.schema import Metric, Model, NodeExpr, Predicate, Template, ValueExpr
from ..money import decimal_amount, money_float, sum_amounts
from .classify import COL_MAJOR, COL_MINOR, COL_NATURAL_UNLINKED
from .link import LINK_KEY, LINKED
from .normalize import PARENT_FIRST, is_parent_only
from .predicate import PredicateError, compile_where
from .types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET

#: 每行对指标的贡献额。
AMOUNT = "__amount__"

#: 事实表的列。任何数字都能沿这张表点回原始文件行号。
FACT_COLUMNS = (
    "metric_id", "source_id", "template_id", "store", "period", "grain",
    "link_key", "linked", "amount", "subject", "major", "minor",
    "file_sha", "file_name", "sheet", "row_no",
    # 投影之后才知道，见 runtime._mark_counted：这一行有没有算进损益表、算进去多少。
    "counted", "contribution",
)


class CalculateError(Exception):
    pass


@dataclass
class NodeValue:
    """公式树一个节点的求值结果。"""

    id: str
    name: str
    level: int
    display: str
    value: float | None
    #: 为空表示数据不全，不出数。这与"算出来是 0"必须区分开。
    available: bool
    #: 缺哪些数据源导致不出数。
    missing_sources: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    is_total: bool = False
    #: 这个平台有没有这一项。为假时界面收起来——1688 没有软件服务费，
    #: 摆一行 0 在那里会被读成「这个月没花这笔钱」。
    applicable: bool = True


# --------------------------------------------------------------------------- #
# 指标求值：产出事实行
# --------------------------------------------------------------------------- #


def evaluate_metric(
    frame: pl.DataFrame,
    metric: Metric,
    template: Template,
    store_hint: str = "",
    period_hint: str = "",
) -> tuple[pl.DataFrame, list[str]]:
    """把一张归一后的数据帧求值成事实行。"""
    notes: list[str] = []
    if frame.is_empty():
        return _empty_facts(), notes

    frame = frame.filter(_predicates(metric.where, frame, notes)) if metric.where else frame
    if frame.is_empty():
        notes.append(f"指标 {metric.id} 的过滤条件筛掉了全部行")
        return _empty_facts(), notes

    amount = _value_expr(metric.value, frame, notes)

    # 父级字段聚合前必须按去重键取首行，否则重复计算。
    if is_parent_only(template, metric.value.of) and PARENT_FIRST in frame.columns:
        amount = pl.when(pl.col(PARENT_FIRST)).then(amount).otherwise(pl.lit(0.0))
        notes.append(f"指标 {metric.id} 只引用父级字段，已按去重键取首行")


    if metric.sign == "negate":
        amount = -amount
    elif metric.sign == "abs_negate":
        amount = -amount.abs()
    elif metric.sign == "abs_positive":
        amount = amount.abs()

    frame = frame.with_columns(amount.fill_null(0.0).alias(AMOUNT))

    # 挂上订单的行，归属跟着订单走；挂不上的才退回表格自己报的店名和日期。
    #
    # 反过来（先信表格）会把证据链切掉一大块：聚水潭把这家店写成「喜必顺旗舰店」，
    # 而店铺档案里叫「淘宝喜必顺」，于是这批行在每个切片里都对不上号，一条都不留档。
    # 表现是商品成本、代发成本、补发成本、客服打款、本金佣金五项报表上有数、点开
    # 一片空白——十七万的商品成本没有一行证据。钱落在谁的订单上是确定的，
    # 表格自己报的名字不是。
    slot = str(metric.time_basis)
    own_period = (
        pl.col(slot).dt.strftime("%Y-%m")
        if slot in frame.columns
        else pl.lit(None, dtype=pl.Utf8)
    )
    period = (
        pl.coalesce(pl.col("__spine_period__"), own_period)
        if "__spine_period__" in frame.columns
        else own_period
    )
    period = pl.coalesce(period, pl.lit(period_hint or None, dtype=pl.Utf8))

    own_store = (
        pl.col("store_name").cast(pl.Utf8)
        if "store_name" in frame.columns
        else pl.lit(None, dtype=pl.Utf8)
    )
    store = (
        pl.coalesce(pl.col("__spine_store__"), own_store)
        if "__spine_store__" in frame.columns
        else own_store
    )
    store = pl.coalesce(store, pl.lit(store_hint or None, dtype=pl.Utf8))

    grain = metric.link.grain if metric.link else "period"
    facts = frame.select(
        pl.lit(metric.id).alias("metric_id"),
        pl.lit(metric.source).alias("source_id"),
        pl.lit(template.id).alias("template_id"),
        store.fill_null("(未知店铺)").alias("store"),
        period.fill_null("(未知账期)").alias("period"),
        pl.when(pl.col(LINKED)).then(pl.lit(grain)).otherwise(pl.lit("unlinked")).alias("grain")
        if LINKED in frame.columns else pl.lit(grain).alias("grain"),
        (pl.col(LINK_KEY) if LINK_KEY in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias("link_key"),
        (pl.col(LINKED) if LINKED in frame.columns else pl.lit(True)).alias("linked"),
        pl.col(AMOUNT).alias("amount"),
        (pl.col("subject").cast(pl.Utf8) if "subject" in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias("subject"),
        (pl.col(COL_MAJOR) if COL_MAJOR in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias("major"),
        (pl.col(COL_MINOR) if COL_MINOR in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias("minor"),
        pl.col(ANCHOR_SHA).alias("file_sha"),
        pl.col(ANCHOR_FILE).alias("file_name"),
        pl.col(ANCHOR_SHEET).alias("sheet"),
        pl.col(ANCHOR_ROW).alias("row_no"),
    ).filter(pl.col("amount") != 0.0)
    return facts, notes


def _empty_facts() -> pl.DataFrame:
    schema = {
        "metric_id": pl.Utf8, "source_id": pl.Utf8, "template_id": pl.Utf8,
        "store": pl.Utf8, "period": pl.Utf8, "grain": pl.Utf8,
        "link_key": pl.Utf8, "linked": pl.Boolean, "amount": pl.Float64,
        "subject": pl.Utf8, "major": pl.Utf8, "minor": pl.Utf8,
        "file_sha": pl.Utf8, "file_name": pl.Utf8, "sheet": pl.Utf8, "row_no": pl.Int64,
        "counted": pl.Boolean, "contribution": pl.Float64,
    }
    return pl.DataFrame(schema=schema)


def row_amount(expr: ValueExpr, frame: pl.DataFrame) -> pl.Expr:
    """按取值表达式算每行净额。给报告层用，核算走 evaluate_metric。"""
    return _value_expr(expr, frame, [])


def _value_expr(expr: ValueExpr, frame: pl.DataFrame, notes: list[str]) -> pl.Expr:
    """取值表达式求值。产出每行的贡献额。"""
    if expr.op == "constant":
        return pl.lit(float(expr.value or 0.0))
    if expr.op == "count":
        return pl.lit(1.0)
    for role in expr.of:
        if role not in frame.columns:
            raise CalculateError(f"取值表达式引用了不存在的字段角色 {role}")
    if expr.op == "sum":
        # 多个角色时逐列相加。对账表把一笔业务拆成「收入金额」「支出金额」两栏，
        # 同一笔可能两栏都有数，净额只能是两栏之和。
        out = pl.col(expr.of[0]).cast(pl.Float64, strict=False).fill_null(0.0)
        for role in expr.of[1:]:
            out = out + pl.col(role).cast(pl.Float64, strict=False).fill_null(0.0)
        return out
    if expr.op == "sum_product":
        out = pl.col(expr.of[0]).cast(pl.Float64, strict=False)
        for role in expr.of[1:]:
            out = out * pl.col(role).cast(pl.Float64, strict=False)
        return out
    raise CalculateError(f"未知取值算子 {expr.op}")  # pragma: no cover


def _predicates(where: tuple[Predicate, ...], frame: pl.DataFrame, notes: list[str]) -> pl.Expr:
    try:
        return compile_where(where, frame)
    except PredicateError as exc:
        raise CalculateError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# 公式树求值
# --------------------------------------------------------------------------- #


def evaluate_statement(
    model: Model,
    metric_totals: dict[str, float],
    unavailable_metrics: set[str],
    inapplicable_metrics: set[str] | None = None,
) -> dict[str, NodeValue]:
    """按公式树求值。

    三种"没有数"要分清楚，混起来会让人读错账：

    unavailable_metrics 是数据没到，上层节点不出数，界面显示破折号并说明缺什么。
    inapplicable_metrics 是这个平台根本没有这一项——1688 没有软件服务费，抖音没有
    分项的平台费用。它们的值确实是 0，但显示成 0 会被读成"这个月没花这笔钱"，
    而且一张损益表上半数行都是这种 0，真正要看的几行就淹了。标出来让界面收起。
    算出来是 0 则照常参与运算。

    不适用只影响呈现，不影响任何金额：本来就没有数据的项，算进去也是加 0。
    """
    inapplicable = inapplicable_metrics or set()
    resolved: dict[str, NodeValue] = {}
    metric_names = {m.id: m.name for m in model.metrics}
    metric_sources = {m.id: m.source for m in model.metrics}
    visiting: set[str] = set()

    def resolve(ref: str) -> NodeValue:
        if ref in resolved:
            return resolved[ref]
        if ref in visiting:
            raise CalculateError(f"公式树存在环，卡在 {ref}")
        visiting.add(ref)
        try:
            node = _resolve_ref(ref)
        finally:
            visiting.discard(ref)
        resolved[ref] = node
        return node

    def _resolve_ref(ref: str) -> NodeValue:
        if ref in metric_names:
            missing = [metric_sources[ref]] if ref in unavailable_metrics else []
            return NodeValue(
                id=ref,
                name=metric_names[ref],
                level=3,
                display="amount",
                value=None if missing else metric_totals.get(ref, 0.0),
                available=not missing,
                missing_sources=missing,
                applicable=ref not in inapplicable,
            )
        spec = model.node(ref)
        refs = spec.children if spec.children else (spec.formula.of if spec.formula else ())
        parts = [resolve(r) for r in refs]
        missing = sorted({s for p in parts for s in p.missing_sources})
        op = "add" if spec.children else spec.formula.op  # type: ignore[union-attr]

        value: float | None
        if op == "constant":
            value, available = float(spec.formula.value or 0.0), True  # type: ignore[union-attr]
        elif any(not p.available for p in parts):
            # 总计行数据不全就不出数。中间分组行按已到部分出数并标注缺项，
            # 但如果一项都没到，出数就该是空而不是 0——显示 0 会被读成"这个月没花钱"。
            partial = [p.value for p in parts if p.available]
            value = None if spec.is_total or not partial else _apply(op, partial)
            available = False
        else:
            value = _apply(op, [p.value for p in parts])
            available = True

        return NodeValue(
            id=spec.id,
            name=spec.name,
            level=spec.level,
            display=spec.display,
            value=value,
            available=available,
            missing_sources=missing,
            children=list(refs),
            is_total=spec.is_total,
            # 只要有一个子项在这个平台成立，这一行就该出现。合计行因此总是出现。
            applicable=any(p.applicable for p in parts) if parts else True,
        )

    for spec in model.statement:
        resolve(spec.id)
    return resolved


def _apply(op: str, values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if op == "add":
        return float(sum_amounts(nums))
    if op == "negate":
        return money_float(-decimal_amount(nums[0])) if nums else None
    if op == "ratio":
        if len(nums) < 2 or nums[1] == 0:
            return None  # DIVIDE 的除零语义：返回空而不是报错
        return nums[0] / nums[1]
    raise CalculateError(f"未知节点算子 {op}")  # pragma: no cover


def totals_by_metric(facts: pl.DataFrame, only_linked: bool = False) -> dict[str, float]:
    """按指标汇总。only_linked 为真时只统计挂上订单的部分。"""
    if facts.is_empty():
        return {}
    frame = facts.filter(pl.col("linked")) if only_linked else facts
    if frame.is_empty():
        return {}
    totals = {}
    for metric_id, amount in frame.select("metric_id", "amount").iter_rows():
        totals[metric_id] = totals.get(metric_id, decimal_amount(0)) + decimal_amount(amount)
    return {metric_id: float(sum_amounts([amount])) for metric_id, amount in totals.items()}
