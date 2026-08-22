"""规则链求值。取关联键与归类都用它。

为什么需要规则链而不是单列查表：实测支付宝账务明细取订单号要走 7 条规则
（业务基础订单号 → 商户订单号正则 → 备注里三种格式 → 经运单号回查聚水潭 →
显式排除余利宝申购这类非经营流水），归类要走 8 条规则（先查科目字典，查不到
按备注关键词兜底，最后两条是显式排除）。

规则链的语义固定为"按声明顺序尝试，第一条命中的生效"。规则内容全部是模型数据。

显式排除这一档很重要：余利宝申购、转出到网商银行根本不是经营流水，如果只是让它
挂不上订单，它就会混在"看起来是订单的钱"里占用用户注意力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from ..model.schema import Bridge, ClassifyRule, FieldMatch, KeyRule, normalize_header

#: 规则链求值结果里表示"显式排除"的哨兵。与"没命中"必须区分开。
EXCLUDED = "\x00excluded"

#: 界面按对账表列名配规则。引擎帧上是角色名，而且同一列名在不同模板上
#: 可能绑到不同角色：1688 收款的「场景类型」是 subject，付款的「场景明细」才是
#: subject。解析时按这个顺序找第一个实际存在的列。
CLASSIFY_FIELD_ROLES: dict[str, tuple[str, ...]] = {
    "subject": ("subject",),
    "remark": ("remark",),
    "biz_type": ("biz_type",),
    "douyin_scene": ("subject",),
    "jd_fee_name": ("subject",),
    "jd_fee_meaning": ("fee_meaning",),
    "scene_type": ("scene_type", "subject"),
    "scene_detail": ("minor_subject", "subject"),
    "bill_type": ("biz_type",),
}


def resolve_classify_field(field: str, columns) -> str | None:
    """规则上的字段 id 落到这一帧真正有的角色列。没有就当这一环不适用。"""
    for role in CLASSIFY_FIELD_ROLES.get(field, (field,)):
        if role in columns:
            return role
    return None


@dataclass
class ChainStats:
    """规则链每一环的命中数。用来回答"这条规则还有没有用"。"""

    hits: dict[int, int] = field(default_factory=dict)
    excluded: int = 0
    unmatched: int = 0
    total: int = 0

    def record(self, index: int | None, excluded: bool = False) -> None:
        self.total += 1
        if excluded:
            self.excluded += 1
        elif index is None:
            self.unmatched += 1
        else:
            self.hits[index] = self.hits.get(index, 0) + 1

    def describe(self, labels: list[str]) -> list[str]:
        out = []
        for i, label in enumerate(labels):
            n = self.hits.get(i, 0)
            share = f"{n / self.total:.1%}" if self.total else "—"
            flag = "   ← 这条规则一次都没用上" if n == 0 else ""
            out.append(f"  规则 {i + 1} {label[:46]:<48} {n:>7,} 行 {share:>7}{flag}")
        if self.excluded:
            out.append(f"  显式排除 {self.excluded:,} 行")
        if self.unmatched:
            out.append(f"  一条都没命中 {self.unmatched:,} 行")
        return out


#: Python 的 re 支持、Rust 的 regex 不支持的写法。撞上就不能交给 Polars 批量跑。
#:
#: 断言类（前后向查看）和反向引用不在 Rust regex 的能力范围内——它保证线性时间，
#: 代价就是放弃这几样。真写了这种模式，Polars 那边不是算错而是直接报错，
#: 但与其等它报错，不如提前认出来走逐行那条路。
_NOT_IN_RUST_REGEX = re.compile(r"\(\?[=!<]|\\\d")


class Matcher:
    """把 FieldMatch 编译成一个可反复调用的判定函数。"""

    __slots__ = ("field", "_extract", "_contains", "_equals", "_matches", "_notnull",
                 "_normalized")

    def __init__(self, spec: FieldMatch) -> None:
        self.field = spec.field
        self._normalized = spec.normalized
        fold = normalize_header if spec.normalized else (lambda v: str(v))
        self._extract = re.compile(spec.extract) if spec.extract else None
        self._contains = tuple(fold(v) for v in spec.contains)
        self._equals = {fold(v) for v in spec.equals}
        self._matches = re.compile(spec.matches) if spec.matches else None
        self._notnull = spec.notnull

    @property
    def vectorizable(self) -> bool:
        """这一环能不能交给 Polars 整列一次算完。"""
        for pattern in (self._matches, self._extract):
            if pattern is not None and _NOT_IN_RUST_REGEX.search(pattern.pattern):
                return False
        return True

    def mask(self, text: pl.Expr) -> pl.Expr:
        """这一环适用于哪些行。`text` 是已经转成字符串并去过空白的那一列。

        和 `apply` 必须逐条对齐，包括一个容易看漏的地方：`apply` 最后返回的是
        `text or None`，也就是空串也算不适用——即使 `notnull` 是关的。所以这里
        无条件带上「非空」，而不是只在 `_notnull` 时才加。

        归一那一档也必须两边对齐：非空判断用原值（归一之后全角空格会变成空串，
        那是「这一格填了东西」而不是「这一格是空的」），比对用归一值。
        """
        cond = text.is_not_null() & (text != "")
        if self._normalized:
            text = normalize_expr(text)
        if self._equals:
            cond = cond & text.is_in(sorted(self._equals))
        if self._contains:
            # contains_any 底下是 Aho-Corasick，一趟扫完所有关键词，
            # 比逐个 contains 再 or 起来快得多，关键词越多差距越大。
            cond = cond & text.str.contains_any(list(self._contains))
        if self._matches is not None:
            cond = cond & text.str.contains(self._matches.pattern)
        if self._extract is not None:
            got = self.value(text)
            cond = cond & got.is_not_null() & (got != "")
        return cond.fill_null(False)

    def value(self, text: pl.Expr) -> pl.Expr:
        """这一环取到的值。没有正则提取就是整个字段值。"""
        if self._extract is None:
            return text
        group = 1 if self._extract.groups else 0
        return text.str.extract(self._extract.pattern, group)

    def apply(self, value: object) -> str | None:
        """返回提取到的值，或 None 表示这一环不适用。

        归一（`normalized`）作用在整个字段值上：之后的比对和正则提取都基于归一值。
        整列版 `mask` 必须给出同一个结果，两边任何一处不归一，同一条规则在
        「走得了整列」和「退回逐行」两种场合会有不同的命中集合。
        """
        if value is None:
            return None
        text = str(value).strip()
        if self._notnull and not text:
            return None
        if self._normalized:
            text = normalize_header(text)
        if self._equals and text not in self._equals:
            return None
        if self._contains and not any(c in text for c in self._contains):
            return None
        if self._matches is not None and not self._matches.search(text):
            return None
        if self._extract is None:
            return text or None
        m = self._extract.search(text)
        if m is None:
            return None
        return (m.group(1) if m.groups() else m.group(0)) or None


@dataclass
class CompiledKeyRule:
    matcher: Matcher
    via: Bridge | None
    exclude: bool
    label: str


def compile_key_rules(rules: tuple[KeyRule, ...]) -> list[CompiledKeyRule]:
    out = []
    for r in rules:
        label = r.note or _describe(r.when)
        if r.exclude:
            label = "排除：" + label
        out.append(CompiledKeyRule(Matcher(r.when), r.via, r.exclude, label))
    return out


def resolve_key(
    row: dict[str, object],
    rules: list[CompiledKeyRule],
    bridges: dict[str, dict[str, str]],
    stats: ChainStats | None = None,
) -> str | None:
    """沿规则链取关联键。返回 None 表示取不到，返回 EXCLUDED 表示显式排除。

    bridges 是各中间表的回查索引：数据源 id → 匹配值 → 取出值。
    """
    for i, rule in enumerate(rules):
        got = rule.matcher.apply(row.get(rule.matcher.field))
        if got is None:
            continue
        if rule.exclude:
            if stats:
                stats.record(i, excluded=True)
            return EXCLUDED
        if rule.via is not None:
            got = bridges.get(rule.via.source, {}).get(_norm(got))
            if not got:
                continue  # 回查失败，继续试下一条规则
        if stats:
            stats.record(i)
        return _norm(got)
    if stats:
        stats.record(None)
    return None


@dataclass
class CompiledClassifyRule:
    dictionary: bool
    matcher: Matcher | None
    major: str | None
    minor: str | None
    exclude: bool
    count_without_order: bool
    label: str


def compile_classify_rules(rules: tuple[ClassifyRule, ...]) -> list[CompiledClassifyRule]:
    out = []
    for r in rules:
        if r.dictionary:
            label = r.note or "查科目字典"
        else:
            label = r.note or f"{_describe(r.when)} → {'排除' if r.exclude else r.major}"
        out.append(
            CompiledClassifyRule(
                r.dictionary, Matcher(r.when) if r.when else None,
                r.major, r.minor, r.exclude, r.count_without_order, label,
            )
        )
    return out


def resolve_class(
    row: dict[str, object],
    rules: list[CompiledClassifyRule],
    lookup,
    stats: ChainStats | None = None,
) -> tuple[str | None, str | None, bool, bool, str]:
    """沿规则链归类。返回 (口径项, 业务小类, 是否排除, 没挂上订单也进账, 哪一环)。

    lookup 是科目字典查表函数：原始科目名 → (口径项, 小类, 是否天然无订单号) 或 None。
    第五项是给人看的：下钻时要能指回字典、模板或界面上那条配置。没命中是空串。
    """
    for i, rule in enumerate(rules):
        if rule.dictionary:
            raw = row.get("subject")
            if raw in (None, ""):
                continue
            found = lookup(str(raw))
            if found is None:
                continue
            if stats:
                stats.record(i)
            return found[0], found[1], False, False, rule.label
        assert rule.matcher is not None
        col = resolve_classify_field(rule.matcher.field, row)
        if rule.matcher.apply(row.get(col) if col else None) is None:
            continue
        if stats:
            stats.record(i, excluded=rule.exclude)
        if rule.exclude:
            return None, None, True, False, rule.label
        # 没写细项就留空。填大类等于把 `software_fee` 这种内部代号当科目名摆到
        # 界面上，而界面退回显示平台原始科目名才是人认得的东西。
        return rule.major, rule.minor or None, False, bool(rule.count_without_order), rule.label
    if stats:
        stats.record(None)
    return None, None, False, False, ""


def _describe(spec: FieldMatch | None) -> str:
    if spec is None:
        return "（无条件）"
    field = {
        "subject": "业务描述",
        "remark": "备注",
        "biz_type": "业务类型",
        "douyin_scene": "动帐场景",
        "jd_fee_name": "费用名称",
        "jd_fee_meaning": "费用项含义",
        "scene_type": "场景类型",
        "scene_detail": "场景明细",
        "bill_type": "账单类型",
    }.get(spec.field, spec.field)
    bits = [field]
    if spec.equals:
        bits.append("完全一致「" + " / ".join(list(spec.equals)[:2]) + "」")
    if spec.contains:
        bits.append("包含「" + " / ".join(list(spec.contains)[:2]) + "」")
    if spec.matches:
        bits.append(f"匹配 {spec.matches}")
    if spec.extract:
        bits.append("按格式提取")
    return " ".join(bits)


_NOISE = re.compile(r"[\s\u3000'\"]+")


def _norm(value: object) -> str:
    if value is None:
        return ""
    s = _NOISE.sub("", str(value)).lstrip("@")
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def norm_expr(col: pl.Expr) -> pl.Expr:
    """`_norm` 的整列版本。两边必须给出同一个结果。

    `.0` 结尾那一段是在还原一个具体的坑：订单号列被 Excel 当数字存过，
    读出来是 `12345.0`，和另一张表里的 `12345` 挂不上。只有纯数字才砍，
    否则会把 `V1.0` 这种真名字砍成 `V1`。

    开头的 `@` 是另一个 Excel 痕迹：长数字被标成文本时，格子里会留下 `@435…`。
    支付宝备注抠出的运单号没有这个前缀，聚水潭快递单号有，不折掉就回查不上。
    """
    s = col.fill_null("").str.replace_all(r"[\s\u3000'\"]+", "").str.strip_chars_start("@")
    stem = s.str.strip_suffix(".0")
    return pl.when(stem.str.contains(r"^\d+$")).then(stem).otherwise(s)


def normalize_expr(col: pl.Expr) -> pl.Expr:
    """`normalize_header` 的整列版本。两边必须给出同一个结果。

    `replace_many` 底下是 Aho-Corasick，九个全角字符一趟换完。写成九次
    `replace_all` 也对，但那是九趟列扫描。
    """
    return (
        col.fill_null("")
        .str.replace_all(r"[\s\u3000\ufeff]+", "")
        .str.replace_many(list("（）［］｛｝：，．／"), list("()[]{}:,./"))
    )


def text_expr(frame_dtype: pl.DataType, field: str) -> pl.Expr:
    """把一列取成「Python 里 `str(v).strip()` 之后的样子」。

    非字符串列要先转字符串，这一步的分歧是这里唯一需要小心的地方：Polars 把
    Float64 的 12345 转成 `12345.0`，Python 的 `str()` 也是 `12345.0`，一致；
    但 Int64 到字符串两边都是 `12345`，也一致。真正对不上的是浮点走科学计数法
    的极端值，而规则链看的都是科目名、备注、业务类型这些文本列，不会撞上。
    """
    col = pl.col(field)
    if frame_dtype != pl.Utf8:
        col = col.cast(pl.Utf8)
    return col.str.strip_chars()
