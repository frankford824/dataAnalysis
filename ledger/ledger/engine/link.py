"""原语四：挂钩。按声明的键做关联，并把结果归集到声明的层级。

层级由模型声明，引擎不猜：订单级、商品级、期间级、店铺级。

关联键支持从文本正则提取——支付宝账务明细没有订单号列，订单号埋在备注里，
格式还有花括号、圆括号、直接拼接三种。提取规则带版本，失败率超阈值告警。

每次关联产出命中率，进入自检层。实测基线：
  淘宝 `聚水潭.线上子订单编号 = 宝贝报表.子订单编号`  99.1%
  拼多多 `聚水潭.线上订单号 = 宝贝报表.订单号`        99.9%
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..model.schema import LinkRule, Metric, Predicate, Template
from .predicate import compile_where, missing_fields
from .rules import (
    EXCLUDED,
    ChainStats,
    _norm,
    compile_key_rules,
    norm_expr,
    resolve_key,
    text_expr,
)
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

    def crosswalk(self, source: str, target: str) -> dict[str, str]:
        """一个角色的键换算到另一个角色的键，例如子订单编号换算到主订单编号。

        同一行上两个角色的值天然配对，所以换算表直接从脊柱读，不需要另建索引。
        """
        if self.frame.is_empty():
            return {}
        if source not in self.frame.columns or target not in self.frame.columns:
            return {}
        out: dict[str, str] = {}
        for a, b in self.frame.select([source, target]).iter_rows():
            ka, kb = normalize_key(a), normalize_key(b)
            if ka and kb and ka not in out:
                out[ka] = kb
        return out

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


def normalize_key(value: object) -> str:
    """关联键归一。去空白与引号，去掉 Excel 把长数字标成文本时加的 @ 前缀，以及末尾 .0。

    和 `_norm` 必须是同一个函数：回查索引建键、规则链取值、脊柱挂单，三处对上才挂得上。
    """
    return _norm(value)


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
    frame, report.fallback_rows = _via_fallback(frame, rule, spine, role, known)
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


def _via_fallback(
    frame: pl.DataFrame,
    rule: LinkRule,
    spine: Spine,
    role: str,
    known: set[str],
) -> tuple[pl.DataFrame, int]:
    """主角色挂不上的行，用备用角色再试，命中后把键换算成主角色的值。

    换算表里要先剔掉那些本身就是主角色键的（单子订单主订单两个号相等，实测
    天猫皇莉诗 41,889 个子订单里有 19,525 个是这种），否则会把已经挂上的行
    再改写一遍——值一样，白算一趟。
    """
    if not rule.fallback_to:
        return frame, 0
    hit = 0
    for alt in rule.fallback_to:
        alt_role = target_role(alt)
        if not alt_role or alt_role == role:
            continue
        remap = {
            k: v for k, v in spine.crosswalk(alt_role, role).items() if k not in known
        }
        if not remap:
            continue
        pending = pl.col(LINK_KEY).is_in(list(remap))
        hit += int(frame.select(pending.fill_null(False).sum()).item())
        frame = frame.with_columns(
            pl.when(pending)
            .then(pl.col(LINK_KEY).replace_strict(remap, default=None, return_dtype=pl.Utf8))
            .otherwise(pl.col(LINK_KEY))
            .alias(LINK_KEY)
        )
    return frame, hit


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

    fast = _keys_vectorized(frame, compiled, bridges)
    if fast is not None:
        keys, stats = fast
        return frame.with_columns(keys.alias(LINK_KEY)), stats

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


#: 没有任何一环命中。
_NO_RULE = -1

#: 收缩候选集时用来记住行原来在第几行。`__row__` 已经被锚点占了——它记的是这一行
#: 在源文件里的行号，不是它在这个 frame 里的下标，两回事，撞名字会静悄悄地串。
_ROWNO = "__rowno__"
_HIT = "__chainhit__"


def _keys_vectorized(
    frame: pl.DataFrame, compiled: list, bridges: dict[str, dict[str, str]]
) -> tuple[pl.Series, ChainStats] | None:
    """整列版的取键规则链。有一环批量跑不了就整条返回 None，让调用方走逐行。

    和归类那条链同一个思路：每一环「适用于哪些行」是一次列扫描，「第一条命中的生效」
    是一次 coalesce。区别在于取键还要把值取出来——正则提取、回中间表查、归一，
    这三步在 Polars 里分别是 `str.extract`、`replace_strict`、几次字符串替换，
    都不用回 Python。

    实测支付宝账务明细取一次键要过 7 环规则，169 万行就是 169 万次 Python 调用。
    """
    plan: list[tuple[int, pl.Expr, pl.Expr]] = []
    for i, rule in enumerate(compiled):
        m = rule.matcher
        if not m.vectorizable:
            return None
        if m.field not in frame.columns:
            continue  # 字段不在这张表里，这一环对谁都不适用（和逐行版一致）
        text = text_expr(frame.schema[m.field], m.field)
        mask = m.mask(text)

        if rule.exclude:
            value = pl.lit(EXCLUDED_KEY, dtype=pl.Utf8)
        elif rule.via is not None:
            # 回中间表查。查不到不算这一环命中，要继续往下试——所以把「查到了」
            # 并进掩码，而不是让它产出一个空值。
            looked = norm_expr(m.value(text)).replace_strict(
                bridges.get(rule.via.source, {}), default=None, return_dtype=pl.Utf8
            )
            mask = mask & looked.is_not_null() & (looked != "")
            value = norm_expr(looked)
        else:
            value = norm_expr(m.value(text))

        plan.append((i, mask, value))

    if not plan:
        return None

    #: 逐环收缩候选集，而不是把每一环都在全表上算一遍。
    #:
    #: 规则链是「命中即停」，逐行版天然省掉了后面几环——绝大多数行第一环就走了，
    #: 剩下六环它连看都不看。整列版一开始没占到这个便宜：七环掩码在 169 万行上
    #: 各扫一遍，正则一次不落，实测 CPU 反而从 30 秒涨到 50 秒，算得更快却更费。
    #:
    #: 于是把「命中即停」翻译成「命中即从候选集里拿走」：第一环在全表上算，
    #: 第二环只看第一环没接住的，越往后候选集越小。取值那几步（正则提取、回表查、
    #: 归一）更是只在真命中的行上算——一环接住一万行，就只算这一万行。
    stats = ChainStats()
    stats.total = frame.height
    todo = frame.with_row_index(_ROWNO)
    picked: list[pl.DataFrame] = []
    for i, mask, value in plan:
        if todo.height == 0:
            break
        todo = todo.with_columns(mask.fill_null(False).alias(_HIT))
        hit = todo.filter(pl.col(_HIT))
        if hit.height:
            if compiled[i].exclude:
                stats.excluded += hit.height
            else:
                stats.hits[i] = hit.height
            picked.append(hit.select(pl.col(_ROWNO), value.alias("v")))
            todo = todo.filter(~pl.col(_HIT))
        todo = todo.drop(_HIT)
    stats.unmatched = todo.height

    if not picked:
        return pl.Series("k", [None] * frame.height, dtype=pl.Utf8), stats

    keys = (
        pl.DataFrame({_ROWNO: pl.arange(0, frame.height, eager=True)})
        .join(pl.concat(picked), on=_ROWNO, how="left")
        .select(
            # 归一之后可能是空串。逐行版在这种情况下照样算「这一环命中了」，只是键
            # 为空，不会接着往下试——所以空串留到这最后一步才转成 null。
            pl.when(pl.col("v") == "").then(None).otherwise(pl.col("v")).alias("k")
        )
        .get_column("k")
    )
    return keys, stats


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
