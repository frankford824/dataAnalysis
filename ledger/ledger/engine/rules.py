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

from ..model.schema import Bridge, ClassifyRule, FieldMatch, KeyRule

#: 规则链求值结果里表示"显式排除"的哨兵。与"没命中"必须区分开。
EXCLUDED = "\x00excluded"


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

    __slots__ = ("field", "_extract", "_contains", "_equals", "_matches", "_notnull")

    def __init__(self, spec: FieldMatch) -> None:
        self.field = spec.field
        self._extract = re.compile(spec.extract) if spec.extract else None
        self._contains = tuple(spec.contains)
        self._equals = {str(v) for v in spec.equals}
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
        """
        cond = text.is_not_null() & (text != "")
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
        """返回提取到的值，或 None 表示这一环不适用。"""
        if value is None:
            return None
        text = str(value).strip()
        if self._notnull and not text:
            return None
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
                r.major, r.minor, r.exclude, label,
            )
        )
    return out


def resolve_class(
    row: dict[str, object],
    rules: list[CompiledClassifyRule],
    lookup,
    stats: ChainStats | None = None,
) -> tuple[str | None, str | None, bool]:
    """沿规则链归类。返回 (口径项, 业务小类, 是否排除)。

    lookup 是科目字典查表函数：原始科目名 → (口径项, 小类, 是否天然无订单号) 或 None。
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
            return found[0], found[1], False
        assert rule.matcher is not None
        if rule.matcher.apply(row.get(rule.matcher.field)) is None:
            continue
        if stats:
            stats.record(i, excluded=rule.exclude)
        if rule.exclude:
            return None, None, True
        return rule.major, rule.minor or rule.major, False
    if stats:
        stats.record(None)
    return None, None, False


def _describe(spec: FieldMatch | None) -> str:
    if spec is None:
        return "(无条件)"
    bits = [spec.field]
    if spec.equals:
        bits.append("等于 " + "/".join(list(spec.equals)[:2]))
    if spec.contains:
        bits.append("含 " + "/".join(list(spec.contains)[:2]))
    if spec.matches:
        bits.append(f"匹配 {spec.matches}")
    if spec.extract:
        bits.append("正则提取")
    return " ".join(bits)


_NOISE = re.compile(r"[\s\u3000'\"]+")


def _norm(value: object) -> str:
    if value is None:
        return ""
    s = _NOISE.sub("", str(value))
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def norm_expr(col: pl.Expr) -> pl.Expr:
    """`_norm` 的整列版本。两边必须给出同一个结果。

    `.0` 结尾那一段是在还原一个具体的坑：订单号列被 Excel 当数字存过，
    读出来是 `12345.0`，和另一张表里的 `12345` 挂不上。只有纯数字才砍，
    否则会把 `V1.0` 这种真名字砍成 `V1`。
    """
    s = col.fill_null("").str.replace_all(r"[\s\u3000'\"]+", "")
    stem = s.str.strip_suffix(".0")
    return pl.when(stem.str.contains(r"^\d+$")).then(stem).otherwise(s)


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
