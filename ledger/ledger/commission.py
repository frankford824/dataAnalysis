"""提成：把每个子订单的毛利，按下单那一刻生效的配置，分到人头上。

这一层不算钱，只分钱
------------------
毛利是引擎算出来的，这里一分不改地拿过来。引擎跑完留下 `spine_facts`——
一行是（指标, 子订单），脊柱那一行上带着商品 id 和下单时间。提成要的东西
已经全在那儿了，这个模块做的只是三件事：

    1. 按模型标记的提成基数节点，把那几个指标在每个子订单上加起来
    2. 按下单时间找到当时生效的那一版配置
    3. 乘上每个人的比例

所以提成和损益表上的毛利必然一致——它们是同一批数字，不是两次计算。

为什么是「按下单时间找版本」而不是「按账期」
--------------------------------------
业务规则原话是「变更时间之前的下单时间按老规则，之后按新规则」。用下单时间，
5 月 10 日改的比例只影响 5 月 10 日之后下的单，同一个 5 月里两种比例并存——
这才是他们要的。用账期的话整个 5 月只能是一种比例，改比例这件事就没法在月中做。

为什么不记「已发多少」
------------------
提成是派生视图，不是台账。每次算都从当前数据重新算出当下正确的数，
跨期退款自然体现在下次结果里，不需要冲回逻辑。这是业务定的：
「提成都是实时计算的，公司实际发放我们不管，我们只要确保数据精确。」

性能
----
全程 polars，没有逐行 Python。找生效版本用 join_asof，那是这件事的原生算子。
实测淘宝一家店 21,988 个子订单、722 个商品，整个模块 0.05 秒量级——
相对于进数据那 30 秒，提成这个功能对性能是免费的。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import polars as pl

from .engine.link import SPINE_PERIOD, SPINE_PRODUCT, SPINE_STORE
from .engine.runtime import RunResult
from .model.schema import CommissionRule, Model
from .money import money_float

#: 脊柱上的下单时间列。提成按它取生效版本。
ORDER_TIME = "order_time"

#: 没配到人的那部分，在结果里用这个名字出现。
UNASSIGNED = "（未配置）"


class CommissionError(Exception):
    """提成算不了。消息里要说清缺什么，不要只说失败。"""


@dataclass(frozen=True)
class PersonLine:
    """一个人在这个店期拿到的提成。"""

    person: str
    #: 提成金额。毛利为负时这里也是负的——照实算，不替业务做取舍。
    amount: float
    #: 参与计算的毛利。
    base: float
    #: 分到几个商品。
    products: int


@dataclass(frozen=True)
class ProductLine:
    """一个商品的毛利和它的分配去向。"""

    product_id: str
    product_name: str
    base: float
    #: 这个商品的总提成率。没配置时是 0。
    total_rate: float
    amount: float
    #: 参与分配的人和各自金额，按金额倒序。
    people: tuple[tuple[str, float], ...]
    sub_orders: int
    #: 走的是店铺兜底而不是商品自己的配置。
    fallback: bool = False
    #: 一条配置都没匹配上。
    unassigned: bool = False
    #: 生效的那一版是哪天的。没匹配上时为空。
    effective_from: str = ""


@dataclass(frozen=True)
class Commission:
    """一个店期的提成结果。"""

    store: str
    period: str
    #: 提成基数是损益表哪一行。
    base_node: str
    base_name: str
    #: 这个店期的毛利合计，等于损益表上那一行。
    base_total: float
    #: 分出去的提成合计。
    total: float
    people: tuple[PersonLine, ...] = ()
    products: tuple[ProductLine, ...] = ()
    #: 没有任何配置命中的毛利。这个数不为零就说明有钱没人管。
    unassigned_base: float = 0.0
    #: 走店铺兜底的毛利。
    fallback_base: float = 0.0
    #: 毛利为负的子订单数与金额。负毛利会算出负提成，得让人看见再决定怎么办。
    negative_orders: int = 0
    negative_base: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.people)


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #


def compute(result: RunResult, model: Model, store: str, period: str) -> Commission:
    """算一个店期的提成。"""
    node = model.commission_base_node()
    if node is None:
        raise CommissionError(
            "模型里没有哪个损益表节点标了 commission_base，不知道提成该按什么算。"
        )
    metrics = model.commission_base_metrics()
    empty = Commission(
        store=store, period=period, base_node=node.id, base_name=node.name,
        base_total=0.0, total=0.0,
    )
    if not metrics:
        return _with_note(empty, f"{node.name} 底下没有任何指标，算不出提成基数。")

    orders = _order_base(result, metrics, _store_labels(model, store), period)
    if orders.is_empty():
        return _with_note(empty, f"{store} 在 {period} 没有订单。")

    base_total = money_float(float(orders["base"].sum()))
    negatives = orders.filter(pl.col("base") < 0)

    # 没有配置也照样往下走，不提前返回。一家店没配提成时，最该给人的东西恰恰是
    # 「这个月都有哪些商品、各自多少毛利」——那就是待配清单。提前返回省下的
    # 那点计算，代价是人得自己去别处凑出这份清单，或者对着 722 个商品 ID 手打。
    rules = model.commission_for(store)
    notes = (
        (f"{store} 还没有提成配置，{base_total:,.2f} 元{node.name}没有分配对象。",)
        if not rules else ()
    )

    matched = _match_versions(orders, rules)
    split = _split_to_people(matched, rules)
    people = _people_lines(split)

    return Commission(
        store=store,
        period=period,
        base_node=node.id,
        base_name=node.name,
        base_total=base_total,
        # 合计取各人金额之和，而不是再对未取整的总额取整一次。差别只在分位，
        # 但这一栏是拿去发钱的：页面上写着合计 5,727.80，底下四个人加起来必须
        # 也是 5,727.80。差一分钱不影响谁的收入，却会让人怀疑整张表。
        total=money_float(sum(p.amount for p in people)),
        people=people,
        products=_product_lines(matched, split, rules),
        unassigned_base=money_float(
            float(matched.filter(pl.col("effective_from").is_null())["base"].sum() or 0.0)
        ),
        fallback_base=money_float(
            float(matched.filter(pl.col("fallback"))["base"].sum() or 0.0)
        ),
        negative_orders=negatives.height,
        negative_base=money_float(float(negatives["base"].sum() or 0.0)),
        notes=notes,
    )


def _with_note(c: Commission, note: str) -> Commission:
    return Commission(**{**c.__dict__, "notes": (*c.notes, note)})


def _store_labels(model: Model, store_id: str) -> list[str]:
    """脊柱上「店铺」这一列存的是店名，提成配置里写的是店铺 id。

    两边不是一个东西，直接拿 id 去比会一行都匹配不上——而且匹配不上不会报错，
    只会算出一个漂亮的 0。所以这里把 id 翻成它认得的所有名字。
    id 本身也留着：万一哪天脊柱上存的是 id，不至于又要改一次。
    """
    try:
        s = model.store(store_id)
    except Exception:
        return [store_id]
    return [x for x in (s.id, s.name, *s.aliases) if x]


def _order_base(
    result: RunResult, metrics: tuple[str, ...], stores: list[str], period: str
) -> pl.DataFrame:
    """每个子订单的提成基数，带商品 id 和下单时间。

    脊柱是全量的（这次上传涉及的所有店期），这里按店期切出来。切完再算，
    免得把别的账期的毛利算进这个月的提成。
    """
    spine = result.spine
    if spine.is_empty():
        return spine

    keep = ["spine_row", SPINE_PRODUCT, ORDER_TIME]
    if "sub_order_id" in spine.columns:
        keep.append("sub_order_id")
    rows = spine.with_row_index("spine_row").with_columns(
        pl.col("spine_row").cast(pl.UInt32)
    )
    for col in (SPINE_STORE, SPINE_PERIOD, SPINE_PRODUCT):
        if col not in rows.columns:
            rows = rows.with_columns(pl.lit("", dtype=pl.Utf8).alias(col))
    if ORDER_TIME not in rows.columns:
        rows = rows.with_columns(pl.lit(None, dtype=pl.Datetime).alias(ORDER_TIME))

    rows = rows.filter(
        pl.col(SPINE_STORE).cast(pl.Utf8).is_in(stores) & (pl.col(SPINE_PERIOD) == period)
    ).select(keep)
    if rows.is_empty():
        return rows

    facts = result.spine_facts
    if facts.is_empty():
        base = rows.with_columns(pl.lit(0.0).alias("base"))
    else:
        per_row = (
            facts.filter(pl.col("metric_id").is_in(list(metrics)))
            .group_by("spine_row")
            .agg(pl.col("amount").sum().alias("base"))
        )
        base = rows.join(per_row, on="spine_row", how="left").with_columns(
            pl.col("base").fill_null(0.0)
        )

    # SPINE_PRODUCT 本来就叫 product_id，就地转型，不要 alias 到同名再 drop。
    return base.with_columns(
        pl.col(SPINE_PRODUCT).cast(pl.Utf8).fill_null(""),
        _as_datetime(base.schema[ORDER_TIME]).alias("order_at"),
    )


def _as_datetime(dtype: pl.DataType) -> pl.Expr:
    """脊柱上的下单时间可能已经是时间类型，也可能还是字符串。

    实测淘宝那份订单明细过来是字符串（`2026-05-31 23:59:54`），而对字符串做
    `cast(Datetime)` 在 polars 里不解析格式，只会整列变 null——然后每一单都
    「下单时间未知」，提成全额落进未配置，界面上是个合法的 0。
    这种错必须在这里挡住，不能靠上游保证。
    """
    col = pl.col(ORDER_TIME)
    if dtype in (pl.Datetime, pl.Date):
        return col.cast(pl.Datetime)
    return col.cast(pl.Utf8).str.to_datetime(strict=False)


def _versions(rules: tuple[CommissionRule, ...]) -> pl.DataFrame:
    """配置里出现过的版本：一行一个（商品, 生效日期）。"""
    seen = {(r.product_id, r.effective_from, r.total_rate) for r in rules}
    return pl.DataFrame(
        {
            "product_id": [p for p, _d, _t in seen],
            "effective_from": [d for _p, d, _t in seen],
            "total_rate": [t for _p, _d, t in seen],
        },
        schema={"product_id": pl.Utf8, "effective_from": pl.Utf8, "total_rate": pl.Float64},
    ).with_columns(pl.col("effective_from").str.to_datetime("%Y-%m-%d").alias("from_at"))


def _match_versions(orders: pl.DataFrame, rules: tuple[CommissionRule, ...]) -> pl.DataFrame:
    """给每个子订单找到下单那一刻生效的那一版。

    先按商品找，找不到再落到店铺兜底。两步都用 join_asof：它的语义正好是
    「取不晚于这个时间点的最后一条」，也就是生效制本身。逐行去比日期同样能算，
    但那是二十万次 Python 循环，而这里是一次 Rust 里的归并。
    """
    versions = _versions(rules)
    product_versions = versions.filter(pl.col("product_id") != "")
    store_versions = versions.filter(pl.col("product_id") == "").drop("product_id")

    # 下单时间缺失的订单没法判生效版本。它们不能默默按最新版算——那等于把
    # 「不知道什么时候下的单」当成「刚下的单」，改比例之前的老单会被按新比例算。
    # set_sorted 不是优化，是消警告：join_asof 带 by 分组时 polars 没法自己确认
    # 有序，每次调用都会 UserWarning。这行代码跑在每次算账上，警告会把服务端日志淹掉，
    # 而日志淹了就等于没有日志。全局排过序，分组内自然也有序，这个断言是成立的。
    known = (
        orders.filter(pl.col("order_at").is_not_null())
        .sort("order_at")
        .with_columns(pl.col("order_at").set_sorted())
    )
    unknown = orders.filter(pl.col("order_at").is_null())

    if product_versions.is_empty():
        hit = known.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("effective_from"),
            pl.lit(None, dtype=pl.Float64).alias("total_rate"),
        )
    else:
        # 带 by 分组时 polars 一律发「没法确认有序」的警告，set_sorted 也压不住。
        # 两边都排过序，前提是成立的。按掉它，否则每算一次账日志里就多一条，
        # 日志被这种恒定噪声填满之后，真正要看的那几行就没人看见了。
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Sortedness of columns")
            hit = known.join_asof(
                product_versions.sort("from_at").with_columns(pl.col("from_at").set_sorted()),
                left_on="order_at",
                right_on="from_at",
                by="product_id",
                strategy="backward",
            ).drop("from_at")

    hit = hit.with_columns(pl.lit(False).alias("fallback"))

    # 商品没配到的落店铺兜底。分成两半再拼，是因为 join_asof 一次只能有一套 by 键。
    if not store_versions.is_empty():
        missed = hit.filter(pl.col("effective_from").is_null()).drop(
            "effective_from", "total_rate", "fallback"
        )
        hit = hit.filter(pl.col("effective_from").is_not_null())
        if not missed.is_empty():
            fell = (
                missed.sort("order_at")
                .with_columns(pl.col("order_at").set_sorted())
                .join_asof(
                    store_versions.sort("from_at").with_columns(pl.col("from_at").set_sorted()),
                    left_on="order_at",
                    right_on="from_at",
                    strategy="backward",
                )
                .drop("from_at")
                .with_columns(pl.col("effective_from").is_not_null().alias("fallback"))
            )
            hit = pl.concat([hit, fell], how="diagonal_relaxed")

    if not unknown.is_empty():
        hit = pl.concat(
            [
                hit,
                unknown.with_columns(
                    pl.lit(None, dtype=pl.Utf8).alias("effective_from"),
                    pl.lit(None, dtype=pl.Float64).alias("total_rate"),
                    pl.lit(False).alias("fallback"),
                ),
            ],
            how="diagonal_relaxed",
        )
    return hit


def _people_frame(rules: tuple[CommissionRule, ...]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "rule_product": [r.product_id for r in rules],
            "effective_from": [r.effective_from for r in rules],
            "person": [r.person for r in rules],
            "share": [r.share for r in rules],
        },
        schema={
            "rule_product": pl.Utf8, "effective_from": pl.Utf8,
            "person": pl.Utf8, "share": pl.Float64,
        },
    )


def _split_to_people(matched: pl.DataFrame, rules: tuple[CommissionRule, ...]) -> pl.DataFrame:
    """把每个子订单的毛利按人的比例摊开。一个子订单几个人就变几行。"""
    people = _people_frame(rules)
    hit = matched.filter(pl.col("effective_from").is_not_null()).with_columns(
        pl.when(pl.col("fallback"))
        .then(pl.lit(""))
        .otherwise(pl.col("product_id"))
        .alias("rule_product")
    )
    if hit.is_empty():
        return hit.with_columns(
            pl.lit("", dtype=pl.Utf8).alias("person"),
            pl.lit(0.0).alias("share"),
            pl.lit(0.0).alias("amount"),
        )
    return hit.join(people, on=["rule_product", "effective_from"], how="inner").with_columns(
        (pl.col("base") * pl.col("share")).alias("amount")
    )


def _people_lines(split: pl.DataFrame) -> tuple[PersonLine, ...]:
    if split.is_empty() or "person" not in split.columns:
        return ()
    agg = (
        split.filter(pl.col("person") != "")
        .group_by("person")
        .agg(
            pl.col("amount").sum().alias("amount"),
            pl.col("base").sum().alias("base"),
            pl.col("product_id").n_unique().alias("products"),
        )
        .sort("amount", descending=True)
    )
    return tuple(
        PersonLine(
            person=r["person"],
            amount=money_float(r["amount"]),
            base=money_float(r["base"]),
            products=int(r["products"]),
        )
        for r in agg.iter_rows(named=True)
    )


def _product_lines(
    matched: pl.DataFrame, split: pl.DataFrame, rules: tuple[CommissionRule, ...]
) -> tuple[ProductLine, ...]:
    names = {r.product_id: r.product_name for r in rules if r.product_name}

    per_product = (
        matched.group_by("product_id")
        .agg(
            pl.col("base").sum().alias("base"),
            pl.len().alias("sub_orders"),
            pl.col("total_rate").max().alias("total_rate"),
            pl.col("fallback").any().alias("fallback"),
            pl.col("effective_from").drop_nulls().max().alias("effective_from"),
        )
        .sort("base", descending=True)
    )

    by_person: dict[str, list[tuple[str, float]]] = {}
    amounts: dict[str, float] = {}
    if not split.is_empty() and "person" in split.columns:
        grouped = (
            split.group_by("product_id", "person")
            .agg(pl.col("amount").sum().alias("amount"))
            .sort("amount", descending=True)
        )
        for row in grouped.iter_rows(named=True):
            pid = row["product_id"]
            by_person.setdefault(pid, []).append((row["person"], money_float(row["amount"])))
            amounts[pid] = amounts.get(pid, 0.0) + row["amount"]

    return tuple(
        ProductLine(
            product_id=r["product_id"],
            product_name=names.get(r["product_id"], ""),
            base=money_float(r["base"]),
            total_rate=float(r["total_rate"] or 0.0),
            amount=money_float(amounts.get(r["product_id"], 0.0)),
            people=tuple(by_person.get(r["product_id"], ())),
            sub_orders=int(r["sub_orders"]),
            fallback=bool(r["fallback"]),
            unassigned=r["effective_from"] is None,
            effective_from=r["effective_from"] or "",
        )
        for r in per_product.iter_rows(named=True)
    )
