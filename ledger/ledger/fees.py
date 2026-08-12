"""引擎到底认识哪些费项，以及和外部对照表的差集。

为什么需要这个模块
----------------
「引擎认识哪些费项」这个问题，答案不在一个地方。它散在两处：

    科目字典（dictionary.csv）   平台原始科目名 → 口径项，精确匹配
    模板归类规则链（templates）   备注/业务类型里含某个词 → 口径项，或者显式排除

两处缺一不可。实测淘宝对账表里量最大的那一项——「消费者体验提升计划服财务费」
19,584 行——字典里根本没有，全靠规则链的一条 contains 接住。只看字典会得出
「这一项没人管」的错误结论；只看规则链又会漏掉字典里那一百多条。

所以拿引擎和业务的费项对照表比对时，必须先把这两处合成一份，否则比出来的差集
既有假阳也有假阴，照着它改反而会把对的改错。

这个模块只回答「知道什么」，不回答「用到了什么」——某一条本期有没有命中、
命中多少钱，那要跑完账才知道，属于归类报告的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model.schema import Model, normalize_header


@dataclass(frozen=True)
class FeeItem:
    """引擎认识的一个费项。"""

    #: 平台原始科目名或关键词。
    key: str
    #: 归到哪个口径项。空串表示这一条是「显式排除，不进账」。
    major: str
    platform: str
    #: 从哪儿知道的：dictionary 或 模板 id。
    origin: str
    #: 匹配方式：exact（字典精确匹配）、contains、equals、regex。
    how: str
    #: 规则链匹配的是哪个字段角色。字典查的是科目名本身。
    field: str = "subject"
    #: 排除项：匹配上就不进账，不是归到某个口径项。
    excluded: bool = False

    @property
    def norm(self) -> str:
        return normalize_header(self.key)


@dataclass
class FeeDiff:
    """引擎和一份外部对照表的差集。"""

    #: 引擎认识、对照表没有。这些是对照表该补的。
    only_engine: list[FeeItem] = field(default_factory=list)
    #: 对照表有、引擎不认。这些进来会落未分类。
    only_table: list[tuple[str, str]] = field(default_factory=list)
    #: 两边都有。
    both: list[FeeItem] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.only_engine and not self.only_table


def known_fees(model: Model) -> list[FeeItem]:
    """引擎认识的全部费项，字典和规则链合在一起。

    同一个 key 在两处都出现是正常的：字典给精确匹配兜底，规则链按备注里的词
    命中——同一笔钱两条路都能到达同一个口径项。这里两条都留着，因为它们的
    维护方式不同：字典是数据，改它不用动模板；规则链写在模板里，改它要发版。
    区分开才知道补一条新费项该往哪儿加。
    """
    out: list[FeeItem] = []

    for e in model.dictionary:
        out.append(FeeItem(
            key=e.raw, major=e.major, platform=e.platform,
            origin="dictionary", how="exact", field="subject",
            excluded=not e.major,
        ))

    plat_of = {t.id: _template_platform(model, t.id) for t in model.templates}
    for t in model.templates:
        for rule in t.classify_rules:
            if rule.dictionary or rule.when is None:
                continue
            m = rule.when
            common = {
                "major": rule.major or "", "platform": plat_of[t.id],
                "origin": t.id, "field": m.field, "excluded": rule.exclude,
            }
            for how, values in (("contains", m.contains), ("equals", m.equals)):
                for v in values or ():
                    out.append(FeeItem(key=v, how=how, **common))
            for how, pattern in (("regex", m.matches), ("regex", m.extract)):
                if pattern:
                    out.append(FeeItem(key=pattern, how=how, **common))
    return out


def _template_platform(model: Model, template_id: str) -> str:
    """模板本身不带平台，靠 id 前缀推。

    取最长匹配，因为平台 id 自己就有前缀关系：`jd` 是 `jd_1688` 的前缀，按声明
    顺序撞上哪个算哪个的话，`jd_1688_xxx` 这张模板会被算成京东的。

    推不出来返回 `*`：模板和平台不是一对一，一张通用表可能几个平台共用。
    这一列只用于展示和分组，推错不影响任何计算。
    """
    hits = [p.id for p in model.platforms if template_id.startswith(p.id)]
    return max(hits, key=len) if hits else "*"


def diff_table(model: Model, table: dict[str, str], platforms: set[str] | None = None) -> FeeDiff:
    """和一份外部对照表比。

    `table` 是 业务描述 → 业务大类。大类不参与比对——两边的口径项一个是中文一个是
    英文 id，硬要对齐得先有一份人工映射，而那份映射本身就是要人来定的东西。
    这里只比「这个费项两边都知道吗」，那是能自动判定的部分。
    """
    fees = [f for f in known_fees(model)
            if platforms is None or f.platform in platforms or f.platform == "*"]
    by_norm: dict[str, FeeItem] = {}
    for f in fees:
        by_norm.setdefault(f.norm, f)

    table_norm = {normalize_header(k): (k, v) for k, v in table.items() if k}

    diff = FeeDiff()
    for key, f in sorted(by_norm.items()):
        (diff.both if key in table_norm else diff.only_engine).append(f)
    for key, (raw, major) in sorted(table_norm.items()):
        if key not in by_norm:
            diff.only_table.append((raw, major))
    return diff
