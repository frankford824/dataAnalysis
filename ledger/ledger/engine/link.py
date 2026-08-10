"""原语四：挂钩。按声明的键做关联，并把结果归集到声明的层级。

层级由模型声明，引擎不猜：订单级、商品级、期间级、店铺级。

关联键支持从文本正则提取——支付宝账务明细没有订单号列，订单号埋在备注里，
格式还有花括号、圆括号、直接拼接三种。提取规则带版本，失败率超阈值告警。

每次关联产出命中率，进入自检层。实测基线：
  淘宝 `聚水潭.线上子订单编号 = 宝贝报表.子订单编号`  99.1%
  拼多多 `聚水潭.线上订单号 = 宝贝报表.订单号`        99.9%
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from ..model.schema import LinkRule, Metric, Predicate, Template
from .predicate import compile_where, missing_fields
from .rules import EXCLUDED, ChainStats, compile_key_rules, resolve_key
from .types import LinkReport

#: 关联键归一后的列名。
LINK_KEY = "__link_key__"
LINKED = "__linked__"

#: 脊柱提供的上下文列。挂上订单的行继承这些值，不需要自己带店铺和账期。
SPINE_STORE = "store"
SPINE_PERIOD = "period"
SPINE_PRODUCT = "product_id"


@dataclass
class Spine:
    """订单脊柱。其他数据挂钩的目标。

    脊柱按多个角色分别建索引：淘宝和抖店挂子订单编号，拼多多没有主子结构挂订单号。
    哪个角色能被挂，由模型里各指标的 `to` 声明决定，引擎不预设。

    脊柱缺失时整个店无法做订单级核算，这是完整度机制里最重的一项。
    """

    frame: pl.DataFrame
    #: 角色 → 关联键 → (店铺, 账期, 商品)
    indexes: dict[str, dict[str, tuple[str, str, str]]] = field(default_factory=dict, repr=False)

    def build(self, role: str) -> None:
        """为一个角色建索引。重复调用无副作用。"""
        if role in self.indexes:
            return
        table: dict[str, tuple[str, str, str]] = {}
        self.indexes[role] = table
        if self.frame.is_empty() or role not in self.frame.columns:
            return
        # role 本身可能就是店铺/期间/商品列，去重后再选，否则 polars 会报列名重复。
        extra = [
            c for c in (SPINE_STORE, SPINE_PERIOD, SPINE_PRODUCT)
            if c in self.frame.columns and c != role
        ]
        cols = [role] + extra
        for row in self.frame.select(cols).iter_rows():
            key = normalize_key(row[0])
            if not key or key in table:
                continue
            values = dict(zip(cols[1:], row[1:]))
            table[key] = (
                str(values.get(SPINE_STORE) or ""),
                str(values.get(SPINE_PERIOD) or ""),
                str(values.get(SPINE_PRODUCT) or ""),
            )

    def keys(self, role: str) -> set[str]:
        self.build(role)
        return set(self.indexes[role])

    def keys_where(self, role: str, where: tuple[Predicate, ...]) -> set[str]:
        """脊柱上满足条件的那些键。

        用来收窄覆盖率的分母：没发货的订单不该被要求有出库成本。
        条件引用了脊柱上没有的列时退回全部键——`expect` 是一句预期声明，
        写不准不该让整个店算不出账，自检层会把退化情况讲出来。
        """
        keys = self.keys(role)
        if not where or self.frame.is_empty():
            return keys
        if missing_fields(where, self.frame) or role not in self.frame.columns:
            return keys
        picked = self.frame.filter(compile_where(where, self.frame)).get_column(role)
        return {k for k in (normalize_key(v) for v in picked) if k} & keys

    def index(self, role: str) -> dict[str, tuple[str, str, str]]:
        self.build(role)
        return self.indexes[role]

    def context(self, role: str, key: str) -> tuple[str, str, str] | None:
        return self.index(role).get(key)

    @property
    def size(self) -> int:
        return self.frame.height

    @classmethod
    def empty(cls) -> Spine:
        return cls(frame=pl.DataFrame())


def target_role(to: str | None) -> str:
    """解析关联目标。形如 `order.sub_order_id`，取角色名。"""
    if not to:
        return ""
    return to.split(".", 1)[1] if "." in to else to


_KEY_NOISE = re.compile(r"[\s\u3000'\"]+")


def normalize_key(value: object) -> str:
    """关联键归一。去空白与引号，去掉 Excel 长数字被转成科学记数或末尾 .0 的痕迹。"""
    if value is None:
        return ""
    s = _KEY_NOISE.sub("", str(value))
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def link(
    frame: pl.DataFrame,
    metric: Metric,
    spine: Spine,
    template: Template | None = None,
    bridges: dict[str, dict[str, str]] | None = None,
) -> tuple[pl.DataFrame, LinkReport]:
    """按指标声明的规则关联。返回加了关联列的数据帧与命中报告。

    模板声明了取键规则链就走规则链，否则用指标 link.key 的简单取值。
    """
    rule = metric.link
    if rule is None:
        report = LinkReport(
            metric_id=metric.id,
            key_role="",
            grain="period",
            total_rows=frame.height,
            linked_rows=frame.height,
        )
        return _without_link(frame), report

    report = LinkReport(metric_id=metric.id, key_role=rule.key, grain=rule.grain, total_rows=frame.height)

    if frame.is_empty():
        return _without_link(frame), report

    if template is not None and template.key_rules:
        frame, chain = _keys_from_chain(frame, template, bridges or {})
        report.chain = chain
        report.excluded_rows = chain.excluded
        report.extract_failed_rows = chain.unmatched
    else:
        frame = frame.with_columns(_extract_keys(frame, rule).alias(LINK_KEY))
        if rule.extract:
            report.extract_failed_rows = int(
                frame.select(
                    (pl.col(rule.key).is_not_null() & pl.col(LINK_KEY).is_null()).sum()
                ).item()
            )

    role = target_role(rule.to)
    if rule.grain in ("period", "store", "unlinked") or not spine.size or not role:
        # 期间级与店铺级不需要挂订单；脊柱为空时全部标为未挂钩，由自检层报缺脊柱。
        linked = pl.lit(rule.grain in ("period", "store"))
        frame = frame.with_columns(linked.alias(LINKED))
        report.linked_rows = frame.height if rule.grain in ("period", "store") else 0
        if metric.naturally_unlinked:
            report.naturally_unlinked_rows = frame.height
        return frame, report

    known = spine.keys(role)
    frame = frame.with_columns(
        (pl.col(LINK_KEY).is_in(list(known)) & (pl.col(LINK_KEY) != EXCLUDED_KEY))
        .fill_null(False)
        .alias(LINKED)
    )

    if metric.naturally_unlinked:
        report.naturally_unlinked_rows = int(frame.select((~pl.col(LINKED)).sum()).item())
    report.linked_rows = int(frame.select(pl.col(LINKED).sum()).item())

    # 覆盖率：脊柱里有多少笔订单拿到了这项数据。命中率高而覆盖率低是最危险的组合。
    # 分母只算预期有这项数据的订单，分子同样收窄，否则覆盖率会超过 100%。
    expected = spine.keys_where(role, metric.expect)
    report.spine_keys_total = len(known)
    report.spine_keys = len(expected)
    report.expect_label = metric.expect_label if len(expected) != len(known) else ""
    hit_keys = set(frame.filter(pl.col(LINKED)).get_column(LINK_KEY).unique().to_list())
    report.covered_keys = hit_keys & expected

    frame = _inherit_context(frame, spine, role)
    return frame, report


def _without_link(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.lit(None, dtype=pl.Utf8).alias(LINK_KEY),
        pl.lit(True).alias(LINKED),
    )


#: 被规则链显式排除的行的关联键。这类行不参与核算，也不算异常。
EXCLUDED_KEY = "__excluded__"


def _keys_from_chain(
    frame: pl.DataFrame, template: Template, bridges: dict[str, dict[str, str]]
) -> tuple[pl.DataFrame, ChainStats]:
    """按模板的取键规则链逐行取键。"""
    compiled = compile_key_rules(template.key_rules)
    fields = sorted({r.matcher.field for r in compiled if r.matcher.field in frame.columns})
    if not fields:
        return frame.with_columns(pl.lit(None, dtype=pl.Utf8).alias(LINK_KEY)), ChainStats()

    stats = ChainStats()

    def resolve(row: dict) -> str | None:
        got = resolve_key(row, compiled, bridges, stats)
        if got == EXCLUDED:
            return EXCLUDED_KEY
        return got or None

    keys = (
        frame.select(pl.struct(fields).alias("s"))
        .get_column("s")
        .map_elements(resolve, return_dtype=pl.Utf8)
    )
    return frame.with_columns(keys.alias(LINK_KEY)), stats


def _extract_keys(frame: pl.DataFrame, rule: LinkRule) -> pl.Expr:
    """取关联键。声明了 extract 就从文本正则提取，否则直接归一原值。"""
    if rule.key not in frame.columns:
        return pl.lit(None, dtype=pl.Utf8)
    col = pl.col(rule.key).cast(pl.Utf8)
    if rule.extract:
        col = col.str.extract(rule.extract, 1)
    return col.map_elements(
        lambda v: normalize_key(v) or None, return_dtype=pl.Utf8, skip_nulls=True
    )


def _inherit_context(frame: pl.DataFrame, spine: Spine, role: str) -> pl.DataFrame:
    """挂上订单的行从脊柱继承店铺与账期，不要求上传方自己填对。"""
    lookup = spine.index(role)

    def pick(idx: int):
        def fn(key: str | None) -> str | None:
            if not key:
                return None
            ctx = lookup.get(key)
            return (ctx[idx] or None) if ctx else None

        return fn

    return frame.with_columns(
        pl.col(LINK_KEY).map_elements(pick(0), return_dtype=pl.Utf8).alias("__spine_store__"),
        pl.col(LINK_KEY).map_elements(pick(1), return_dtype=pl.Utf8).alias("__spine_period__"),
    )


# --------------------------------------------------------------------------- #
# 常用提取规则（作为模型数据的参考，引擎不内置任何一条）
# --------------------------------------------------------------------------- #

#: 支付宝备注里订单号的三种格式：花括号、圆括号、直接拼接。
#: 这条正则属于模型数据，写在这里只作为文档，引擎不会自动使用。
ALIPAY_ORDER_IN_REMARK = r"[{(（]?(\d{15,25})[})）]?"
