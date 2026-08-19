"""把源表金额投影到脊柱行上。

为什么核算必须走这一步，而不是在源表侧聚合完就算完：源数据的粒度常常粗于脊柱。
对账表是主订单级、广告报表是商品级，而脊柱是子订单行级。粗粒度金额要落到脊柱行上，
分摊的除数来自脊柱（该商品有几个订单行、该主订单有几个子订单），源表侧根本算不出来。

实测这一步做对了，逐行能和人工 Excel 表完全对上；做错了差一倍——把主订单级金额
直接挂到每个子订单行上，主订单有几个子订单就重复计算几次。

投影产出两套事实：
    源事实   一行一条源记录，带文件行号，是证据链
    脊柱事实 一行一条脊柱记录，是口径，损益表从这里出数
两套都要留，前者回答"这个数从哪来"，后者回答"这个数是多少"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..model.schema import Metric
from ..money import decimal_amount, money_float, sum_amounts
from .link import SPINE_PERIOD, SPINE_STORE, Spine, target_role

#: 脊柱事实的列。
SPINE_FACT_COLUMNS = (
    "metric_id", "source_id", "store", "period", "link_key",
    "amount", "factor", "spine_row",
)


@dataclass
class Projection:
    """一个指标投影到脊柱的结果。"""

    facts: pl.DataFrame
    #: 源表里有金额、但脊柱上找不到对应键的部分。绝不静默丢弃。
    orphan_amount: float = 0.0
    orphan_keys: int = 0
    #: 脊柱上有、但这个指标没覆盖到的行数。覆盖率就从这里来。
    uncovered_rows: int = 0
    notes: list[str] = field(default_factory=list)


def claims(metric: Metric) -> pl.Expr:
    """这个指标认领哪些源事实行。

    事实表里存的是「每个指标看过的每一行」：对账表有五个指标读它，一行钱就在表里
    躺着五份。真正算进哪个指标由归类结果（`major`）定，所以凡是要回答「这一行属于
    哪个指标」的地方，都得过这一层——投影、进账标记、界面下钻各有一处。

    三处必须一致。不一致的表现是同一行钱在报表、下钻、检索里归到三个不同科目下，
    而三个说法看着都像对的。所以这个条件只写一遍。

    没声明大类的指标（推广扣费、运费这类源头就不分科目的表）不加这一层：
    它们的每一行都算数，硬要求 major 相等会把整张表筛空。
    """
    hit = pl.col("metric_id") == metric.id
    return hit & (pl.col("major") == metric.major) if metric.major else hit


def aggregate_by_key(source_facts: pl.DataFrame, metric: Metric) -> pl.DataFrame:
    """源事实按关联键汇总。这是投影的输入。"""
    frame = source_facts.filter(claims(metric))
    if frame.is_empty():
        return pl.DataFrame(schema={"link_key": pl.Utf8, "amount": pl.Float64})
    totals = {}
    for key, amount in (
        frame.filter(pl.col("link_key").is_not_null())
        .select("link_key", "amount")
        .iter_rows()
    ):
        totals[key] = totals.get(key, decimal_amount(0)) + decimal_amount(amount)
    return pl.DataFrame({
        "link_key": list(totals),
        # Allocation can legitimately carry sub-cent weights. Preserve them at
        # this intermediate boundary and round only the final ledger output.
        "amount": [float(sum_amounts([amount], cents=False)) for amount in totals.values()],
    }, schema={"link_key": pl.Utf8, "amount": pl.Float64})


def project(
    source_facts: pl.DataFrame,
    metric: Metric,
    spine: Spine,
) -> Projection:
    """把一个指标的源金额投影到脊柱行。"""
    role = target_role(metric.link.to) if metric.link else ""
    if not role or spine.frame.is_empty():
        return Projection(facts=_empty(), notes=[f"指标 {metric.name} 没有可投影的脊柱"])

    by_key = aggregate_by_key(source_facts, metric)
    spine_frame = spine.frame
    if role not in spine_frame.columns:
        return Projection(
            facts=_empty(),
            notes=[f"脊柱上没有 {role} 这一列，指标 {metric.name} 无法投影"],
        )

    keyed = spine_frame.with_columns(
        pl.col(role).cast(pl.Utf8).alias("link_key")
    ).with_row_index("spine_row")

    factor = _factor(keyed, metric)
    joined = keyed.join(by_key, on="link_key", how="left")

    facts = joined.select(
        pl.lit(metric.id).alias("metric_id"),
        pl.lit(metric.source).alias("source_id"),
        pl.col(SPINE_STORE).alias("store") if SPINE_STORE in joined.columns
        else pl.lit(None, dtype=pl.Utf8).alias("store"),
        pl.col(SPINE_PERIOD).alias("period") if SPINE_PERIOD in joined.columns
        else pl.lit(None, dtype=pl.Utf8).alias("period"),
        pl.col("link_key"),
        (pl.col("amount").fill_null(0.0) * factor).round(6).alias("amount"),
        factor.alias("factor"),
        pl.col("spine_row"),
    )

    covered = int(joined.select(pl.col("amount").is_not_null().sum()).item())
    matched_keys = set(
        joined.filter(pl.col("amount").is_not_null()).get_column("link_key").unique().to_list()
    )
    all_keys = set(by_key.get_column("link_key").to_list())
    missing = all_keys - matched_keys
    # 声明了「没挂上订单也进账」的键不是孤儿：人工对账表按费项 SUMIFS，
    # 并不要求对应订单出现在本期明细里。淘宝联盟佣金代扣就是这种。
    #
    # 只落进脊柱上已经有的店期。结算日在下个月、而下个月还没有订单明细时，
    # 不能凭空开出一个只有这几笔扣费、别的全缺的账期——那个残缺月会拦着结账，
    # 比这几毛钱没进账更糟。等那个账期的明细到了，这些键会再进来。
    orderless_keys, stranded = _orderless_on_spine(
        source_facts, metric, missing, spine,
    )
    orphan_keys = (missing - orderless_keys) | stranded
    orphan_amount = float(sum_amounts(
        by_key.filter(pl.col("link_key").is_in(list(orphan_keys)))
        .get_column("amount")
        .to_list()
    )) if orphan_keys else 0.0

    out = facts.filter(pl.col("amount") != 0.0)
    extra = _orderless_spine_facts(source_facts, metric, orderless_keys, by_key)
    if not extra.is_empty():
        out = pl.concat([out, extra], how="diagonal_relaxed")

    proj = Projection(
        facts=out,
        notes=ratio_health(keyed, metric) + _ratio_fallback_notes(keyed, metric),
        orphan_amount=money_float(orphan_amount),
        orphan_keys=len(orphan_keys),
        uncovered_rows=keyed.height - covered,
    )
    if orphan_keys:
        proj.notes.append(
            f"{metric.name}：源表里有 {len(orphan_keys):,} 个键、{orphan_amount:,.2f} 元"
            f"在脊柱上找不到对应订单，这部分没进利润"
        )
    return proj


def _orderless_keys(source_facts: pl.DataFrame, metric: Metric) -> set[str]:
    """归类规则标了 count_without_order、因而没挂上订单也要进账的那些键。"""
    if source_facts.is_empty() or "count_without_order" not in source_facts.columns:
        return set()
    frame = source_facts.filter(claims(metric) & pl.col("count_without_order").fill_null(False))
    if frame.is_empty() or "link_key" not in frame.columns:
        return set()
    return {k for k in frame.get_column("link_key").to_list() if k}


def _orderless_on_spine(
    source_facts: pl.DataFrame,
    metric: Metric,
    missing: set[str],
    spine: Spine,
) -> tuple[set[str], set[str]]:
    """把要进账的键分成「这个店期脊柱上有」和「会凭空开出新账期」两堆。"""
    flagged = missing & _orderless_keys(source_facts, metric)
    if not flagged:
        return set(), set()
    known = _periods_on_spine(spine)
    if not known or "store" not in source_facts.columns or "period" not in source_facts.columns:
        return set(), flagged
    frame = source_facts.filter(claims(metric) & pl.col("link_key").is_in(list(flagged)))
    keep: set[str] = set()
    for key, store, period in frame.select("link_key", "store", "period").iter_rows():
        if key and (str(store or ""), str(period or "")) in known:
            keep.add(key)
    return keep, flagged - keep


def _periods_on_spine(spine: Spine) -> set[tuple[str, str]]:
    frame = spine.frame
    if frame.is_empty() or SPINE_STORE not in frame.columns or SPINE_PERIOD not in frame.columns:
        return set()
    return {
        (str(store or ""), str(period or ""))
        for store, period in frame.select(SPINE_STORE, SPINE_PERIOD).unique().iter_rows()
    }


def _orderless_spine_facts(
    source_facts: pl.DataFrame,
    metric: Metric,
    keys: set[str],
    by_key: pl.DataFrame,
) -> pl.DataFrame:
    """把没挂上订单、但仍要进账的金额做成脊柱事实。不分摊，factor=1。

    spine_row 留空：这些钱不属于任何一个子订单，提成那边会收到「（无订单）」那一行，
    不能随便挂到脊柱第 0 行，否则会污染某个真实子订单的毛利。
    """
    if not keys:
        return _empty()
    amounts = by_key.filter(pl.col("link_key").is_in(list(keys)))
    if amounts.is_empty():
        return _empty()
    ctx_cols = [c for c in ("store", "period") if c in source_facts.columns]
    if ctx_cols:
        ctx = (
            source_facts.filter(claims(metric) & pl.col("link_key").is_in(list(keys)))
            .group_by("link_key")
            .agg(*[pl.col(c).drop_nulls().first() for c in ctx_cols])
        )
        amounts = amounts.join(ctx, on="link_key", how="left")
    return amounts.select(
        pl.lit(metric.id).alias("metric_id"),
        pl.lit(metric.source).alias("source_id"),
        (
            pl.col("store") if "store" in amounts.columns
            else pl.lit(None, dtype=pl.Utf8)
        ).alias("store"),
        (
            pl.col("period") if "period" in amounts.columns
            else pl.lit(None, dtype=pl.Utf8)
        ).alias("period"),
        pl.col("link_key"),
        pl.col("amount"),
        pl.lit(1.0).alias("factor"),
        pl.lit(None, dtype=pl.UInt32).alias("spine_row"),
    )


def _even() -> pl.Expr:
    """组内均分：除数是脊柱里共享同一个键的行数。"""
    return 1.0 / pl.len().over("link_key").cast(pl.Float64)


def _factor(keyed: pl.DataFrame, metric: Metric) -> pl.Expr:
    """每条脊柱行拿到的比例。"""
    alloc = metric.allocate
    if alloc is None:
        return pl.lit(1.0)
    if alloc.mode == "ratio":
        if alloc.by in keyed.columns:
            return pl.col(alloc.by).cast(pl.Float64, strict=False).fill_null(0.0)
        # 天猫千牛导出经常没有「收入分配率」这一列。绝不能按 1 填——
        # 一个主订单有几个子订单，钱就会被记几遍，利润凭空翻倍。
        # 有买家实付就按金额占比推；没有就退回笔数均摊。合计都守恒。
        if "buyer_paid" in keyed.columns:
            paid = pl.col("buyer_paid").cast(pl.Float64, strict=False).fill_null(0.0)
            total = paid.sum().over("link_key")
            return (
                pl.when(total == 0)
                .then(_even())
                .otherwise(paid / total)
            )
        return _even()
    return _even()


def _ratio_fallback_notes(keyed: pl.DataFrame, metric: Metric) -> list[str]:
    alloc = metric.allocate
    if alloc is None or alloc.mode != "ratio" or alloc.by in keyed.columns:
        return []
    if "buyer_paid" in keyed.columns:
        return [f"{metric.name}：订单明细没有收入分配率，已按买家实付金额占比分摊"]
    return [f"{metric.name}：订单明细没有收入分配率，已按子订单笔数均摊"]


def ratio_health(keyed: pl.DataFrame, metric: Metric) -> list[str]:
    """分摊率的数值健康度。

    实测全量 205 万行分配率取值区间 -10.06 到 16.19：等于 1 占 45.8%、等于 0 占 14.9%、
    0 到 1 之间占 39.0%、负值 0.210%、大于 1 占 0.115%。越界的不拦，但必须报出来——
    分配率大于 1 意味着这个子订单分到的钱比主订单总额还多。
    """
    alloc = metric.allocate
    if alloc is None or alloc.mode != "ratio" or alloc.by not in keyed.columns:
        return []
    col = pl.col(alloc.by).cast(pl.Float64, strict=False)
    stats = keyed.select(
        (col < 0).sum().alias("neg"),
        (col > 1).sum().alias("over"),
        col.is_null().sum().alias("null"),
    ).row(0, named=True)
    notes = []
    if stats["neg"] or stats["over"]:
        notes.append(
            f"{metric.name} 的分摊率有 {stats['neg']:,} 行为负、{stats['over']:,} 行大于 1，"
            f"这些行的分摊结果不可信"
        )
    if stats["null"]:
        notes.append(f"{metric.name} 的分摊率有 {stats['null']:,} 行为空，已按 0 计")
    return notes


def _empty() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "metric_id": pl.Utf8, "source_id": pl.Utf8, "store": pl.Utf8, "period": pl.Utf8,
            "link_key": pl.Utf8, "amount": pl.Float64, "factor": pl.Float64,
            "spine_row": pl.UInt32,
        }
    )
