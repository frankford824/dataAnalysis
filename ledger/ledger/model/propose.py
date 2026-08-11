"""接表提议器：看一眼没见过的表头，草拟一份模板。

这个模块是「以后所有店铺自助接入」的那块拼图。没有它，每接一张新表都要有人打开
templates.yaml 逐列写映射——那意味着接第四家店得等开发排期，系统再通用也没用。

提议的全部依据都来自模型自己，不来自代码里的平台知识：

  一、模型已有的词汇表。`买家实付金额` 在三个模板里都绑到 `buyer_paid`，
      那第四张表里的这一列几乎不用问。模型越大，提议越准，而且不用改代码。
  二、近似模板。平台改版加减几列是最常见的情形，此时基准模板的映射直接可复用，
      真正要人看的只有新增和消失的那几列。
  三、值的形态。列名没见过时，采样几百行看看是钱、是日期、还是订单号，
      至少能把「这列是什么」缩小到一类，让人做选择而不是填空。

有一条红线：**近似列名永远只能是低置信度的建议，绝不自动采用**。这个系统里最贵的
一次事故就是有人把 `线上子订单号` 当成 `线上子订单编号` 的同义写法，少一个「编」字，
全部商品成本静默挂不到订单，不报错、不为零，只是悄悄少算。字面像不代表语义同，
提议器可以指出「最像的是哪个」，但必须由人拍板。

同样地，提议本身不写任何文件。它产出一份草案，人确认之后才有 `commit` 落库。
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from ..engine.derivative import PIVOT_PREFIXES
from ..engine.normalize import to_date
from .loader import ModelError
from .schema import (
    ColumnBinding,
    Model,
    ParseOptions,
    Template,
    normalize_header,
    signature_of,
)

#: 一列的形态。从采样值看出来的，不看列名。
Shape = Literal["number", "time", "id", "text", "empty"]

#: 提议的可信程度。只有 exact 能默认勾上，其余都要人点一下。
Confidence = Literal["exact", "likely", "guess", "unknown"]

#: 近似列名要报为「最像的」至少得有这个相似度。
_FUZZY_FLOOR = 0.78

#: match_columns 取几列。太少会误匹配别的表，太多会让平台一改版就失配。
#: 现有模板手写的都在 5～6 列，跟着这个数。
_MATCH_WIDTH = 6

#: 采样多少行来看值的形态。几百行足够判断，再多只是变慢。
_SAMPLE_ROWS = 300


# --------------------------------------------------------------------------- #
# 从模型里反读出「已知的角色」和「已知的列名」
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoleFacts:
    """一个字段角色，以及它在模型里是怎么被用的。

    这些都是从模型反读的，不是另维护一份角色注册表。多一份注册表就多一处会过期。
    """

    role: str
    #: 从用法反推的形态：被指标求和的是钱，挂在时间槽位上的是时间，用来关联的是键。
    kind: Shape
    #: 哪些模板用过它。
    templates: tuple[str, ...] = ()
    #: 在别的表里，这个角色对应过哪些列名。给人做参照用。
    columns: tuple[str, ...] = ()
    #: 供给哪些指标。人要判断「这列该不该映射」时，看的其实是这个。
    metrics: tuple[str, ...] = ()

    @property
    def hint(self) -> str:
        parts = []
        if self.columns:
            parts.append("别处叫：" + "、".join(self.columns[:4]))
        if self.metrics:
            parts.append("供给：" + "、".join(self.metrics[:3]))
        return "；".join(parts)


def role_facts(model: Model, source_id: str = "") -> dict[str, RoleFacts]:
    """模型里所有已知的字段角色。

    给 source_id 就只看这个数据源的角色。接表向导里这一点很要紧：往 `order_detail`
    加一张表时，能选的角色应该是订单明细那套，而不是把全模型几十个角色摊给人选。

    形态（是钱、是日期还是编号）反过来看全模型，不按数据源收窄。脊柱就是个现成的
    反例：`order_detail` 一个指标都没有——脊柱不产生金额，它只被别人挂。
    只看本数据源的话，`sub_order_id` 和 `buyer_paid` 全被判成文本，形态校验白做。
    角色名在这套模型里是通行约定，哪张表里的 `buyer_paid` 都是钱。
    """
    kinds = _role_kinds(model)
    templates = model.templates_of(source_id) if source_id else model.templates
    columns: dict[str, list[str]] = {}
    owners: dict[str, list[str]] = {}
    for tpl in templates:
        for b in tpl.bindings:
            owners.setdefault(b.role, []).append(tpl.id)
            for c in b.columns:
                if c not in columns.setdefault(b.role, []):
                    columns[b.role].append(c)

    metrics: dict[str, list[str]] = {}
    for m in model.metrics:
        if source_id and m.source != source_id:
            continue
        for role in (*m.value.of, *(p.field for p in m.where)):
            metrics.setdefault(role, []).append(m.id)
        if m.link and m.link.key:
            metrics.setdefault(m.link.key, []).append(m.id)

    scoped = set(owners) | set(metrics)
    # 没给数据源就把全模型的角色都列出来；给了就只列这个数据源沾到的，
    # 但形态一律取全局。
    roles = scoped if source_id else scoped | set(kinds)
    return {
        role: RoleFacts(
            role=role,
            kind=kinds.get(role, "text"),
            templates=tuple(owners.get(role, ())),
            columns=tuple(columns.get(role, ())),
            metrics=tuple(dict.fromkeys(metrics.get(role, ()))),
        )
        for role in sorted(roles)
    }


def _role_kinds(model: Model) -> dict[str, Shape]:
    """从全模型的用法反推每个角色是什么形态。

    这是判断「名字对但内容不对」的唯一依据，所以证据要取全：时间槽位、关联键、
    关联目标、被求和的列、分摊比例。少取一处就多一类漏判。
    """
    kinds: dict[str, Shape] = {}
    for tpl in model.templates:
        for role in tpl.time_slots.values():
            kinds[role] = "time"
        for rule in tpl.key_rules:
            # 只有「整列就是键」才算编号列。
            # 带 extract 的是从文本里抠出键——支付宝订单号埋在备注里，备注是文本；
            # 带 contains/equals/matches 或 exclude 的是在筛条件，被筛的那列同样是文本。
            # 不分清楚的话备注列会被判成编号，形态校验反过来乱报。
            w = rule.when
            if w.extract is None and not rule.exclude and not (w.contains or w.equals or w.matches):
                kinds.setdefault(w.field, "id")
    for m in model.metrics:
        for p in m.where:
            if p.op in ("gt", "lt"):
                kinds.setdefault(p.field, "number")
        if m.link:
            if m.link.key and not m.link.extract:
                kinds.setdefault(m.link.key, "id")
            # `to` 形如 `order.sub_order_id`，点号后面是脊柱上的角色。
            # 脊柱自己没有指标，它的键只能从这里认出来。
            if m.link.to and "." in m.link.to:
                kinds.setdefault(m.link.to.split(".", 1)[1], "id")
        if m.allocate and m.allocate.by:
            kinds.setdefault(m.allocate.by, "number")
        if m.value.op in ("sum", "sum_product"):
            for role in m.value.of:
                kinds.setdefault(role, "number")
    return kinds


def vocabulary(model: Model) -> dict[str, tuple[tuple[str, int], ...]]:
    """模型见过的列名 → 它被绑成过哪些角色，按出现次数从多到少。

    同一个列名映到两个角色是有的（`金额` 在不同表里含义不同），所以值是个列表，
    不是单个角色。这种歧义必须让人看见，不能默默取第一个。
    """
    seen: dict[str, dict[str, int]] = {}
    for tpl in model.templates:
        for b in tpl.bindings:
            for c in b.columns:
                key = normalize_header(c)
                if key:
                    counts = seen.setdefault(key, {})
                    counts[b.role] = counts.get(b.role, 0) + 1
    return {
        col: tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        for col, counts in seen.items()
    }


def known_columns(model: Model, template_id: str = "") -> set[str]:
    """某个模板认得的全部列名；不给模板就是全模型的。"""
    templates = [model.template(template_id)] if template_id else list(model.templates)
    out: set[str] = set()
    for tpl in templates:
        out |= {normalize_header(c) for c in tpl.match_columns}
        out |= {normalize_header(c) for b in tpl.bindings for c in b.columns}
    return {c for c in out if c}


# --------------------------------------------------------------------------- #
# 值的形态
# --------------------------------------------------------------------------- #

_DATE_MARKS = ("-", "/", ":", "年", "月", "日")

#: 编号的长相：字母数字加连字符，没有小数点也没有空格。
_ID_LIKE = re.compile(r"^[A-Za-z0-9\-]+$")

#: 严格判数：数字、正负号、一个小数点、科学计数。
_NUMBER_LIKE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

#: 判数之前可以去掉的装饰：千分位、货币符号、百分号、表示负数的括号。
_DECOR = str.maketrans("", "", ",，¥￥$%（）() 元")


def shape_of(values: list[Any], null_tokens: tuple[str, ...] = ()) -> tuple[Shape, tuple[str, ...]]:
    """采样值是什么形态，附带几个样例。

    编号单独归一类，不算数字：19 位的订单号超过 float64 能精确表示的范围，
    转一次就变成另一个订单号，而且看不出来。判错方向的代价不对称——
    金额被当成编号，只是后面有指标对它求和时明确报错；编号被当成金额，
    是静默改数据。所以先判编号。

    判数用自己的严格规则，不用 `normalize.to_number`。那个函数是故意宽松的
    （要吃「1,234.56元」这种写法），它会把运单号 `YT7624571865374` 的字母剥掉
    再读成数字——用它判形态，整列运单号会被判成金额。

    `null_tokens` 要传：各平台表示空值的写法五花八门（拼多多广告和抖店 ROI 用 `-`），
    一个 `-` 混在金额列里就会把整列判成文本，而文本列不会被转成数值——
    这一列的钱就全变成 0 了。
    """
    blank = {"", *null_tokens}
    samples = [
        s for v in values
        if v is not None and (s := str(v).strip()) not in blank
    ]
    show = tuple(dict.fromkeys(samples))[:5]
    if not samples:
        return "empty", ()

    probe = samples[:200]
    # 长度看最长的那个：同一列里混着 8 位和 15 位的运单号是常事，
    # 按最短判会把整列当成金额。
    if all(_ID_LIKE.match(s) for s in probe) and max(len(s) for s in probe) >= 11:
        return "id", show
    if all(_NUMBER_LIKE.match(s.translate(_DECOR)) for s in probe):
        return "number", show
    if all(
        to_date(s) is not None and any(mark in s for mark in _DATE_MARKS)
        for s in probe
    ):
        return "time", show
    return "text", show


# --------------------------------------------------------------------------- #
# 草案
# --------------------------------------------------------------------------- #


@dataclass
class ColumnGuess:
    """一列的提议。界面上一行就是这个。"""

    column: str
    #: 这一列在表头里的位置（0 起）。界面回传映射时用它寻址，不用列名——
    #: 列名会重复，重名列用列名寻址就没法分别设置，两列会拿到同一个角色。
    index: int = 0
    role: str = ""
    confidence: Confidence = "unknown"
    #: 为什么这么提。人要凭它决定信不信，所以必须是人话且带证据。
    why: str = ""
    #: 其他候选角色，按可能性排。
    alternatives: tuple[str, ...] = ()
    shape: Shape = "text"
    samples: tuple[str, ...] = ()
    #: 重复列名时这是第几个（0 起）。1688 订单表就有两列都叫「订单号」。
    occurrence: int = 0
    #: 看着是他们在表里自己算出来的结果列。映射它会重复计算。
    derived: bool = False
    #: 列名跟这个数据源现有的角色都对不上，只有形态是同一类。
    #:
    #: 单独看这个判断很弱，不足以说明任何事。但它在数量上是决定性的：万相台那张表
    #: 78 列里有 71 列落在这一档，而它们几乎全是平台自带的展现量、转化率、投产比。
    #: 界面靠这个标记把它们收拢成一组、共用一句说明——不然同一段话重复七十遍，
    #: 真正要人拍板的那几列就被埋掉了。
    no_name_match: bool = False
    #: 大模型对这一列的建议。规则提议之后叠上来的，不覆盖 `role`。
    #:
    #: 分成两种情况，界面上要看得出区别：规则没提而模型提了，模型的建议进 `role`
    #: 并把可信度记成 guess；规则和模型给的不一样，`role` 保持规则那份，冲突记在
    #: 这里让人拍板。悄悄采纳模型的说法是不行的——那样人看到的「规则提议」
    #: 其实混着模型的猜测，就再没有一处能对照了。
    model_role: str = ""
    #: 模型给的理由。人凭它决定信不信，所以原样带上来。
    model_why: str = ""
    #: `role` 这个值是模型填上去的，规则原本是空的。
    #:
    #: 和「模型同意规则的判断」必须分得开。两种情况下 `role` 和 `model_role` 都相等，
    #: 但一个是两条独立的路走到了同一个结论，另一个只有模型一家之言——界面上写成
    #: 同一句话，人就会把后者当成有两重依据。
    model_filled: bool = False

    @property
    def settled(self) -> bool:
        """能默认勾上吗。只有模型里见过同名同角色的才算。"""
        return self.confidence == "exact" and bool(self.role)


@dataclass
class Draft:
    """一份待人确认的模板草案。

    草案不是模板：它带着每一列的证据和可信度，也带着「哪里可能撞车」的警告。
    确认之后才由 `template()` 生成真正的模板对象。
    """

    signature: str
    headers: tuple[str, ...]
    #: revision 是平台改版（有近似的基准模板），new 是全新的表。
    kind: Literal["revision", "new"] = "new"
    #: 基准模板 id。改版时有。
    base: str = ""
    #: 建议挂到哪个数据源。全新表时可能为空，得让人选。
    source: str = ""
    columns: list[ColumnGuess] = field(default_factory=list)
    #: 基准模板要求、这张表却没有的列。平台删列或改名都会落在这里。
    vanished: tuple[str, ...] = ()
    #: 基准模板认得的列名（已归一）。用来挑出能把两版区分开的签名列。
    base_columns: frozenset[str] = frozenset()
    #: 得让人看一眼的地方。**全部由当前这份列映射推出来**，映射一改就作废。
    warnings: list[str] = field(default_factory=list)
    #: 跟列映射无关的提醒，比如「这张表其实已经认得出来了」。重算警告时留着。
    #:
    #: 和 `warnings` 分开，是因为映射会被改：人改一列、模型补一列，警告都得重算。
    #: 混在一条列表里就只能整份重来，那句跟映射无关的提醒会一起消失；不重算的话
    #: 更糟——屏幕上会同时出现「spend 没映上」和一行映着 spend 的表格，人这时候
    #: 该信哪个？自相矛盾的警告比没有警告坏，它会让人开始忽略所有警告。
    notices: list[str] = field(default_factory=list)
    #: 解析参数的建议（表头在第几行之类）。
    parse: ParseOptions = field(default_factory=ParseOptions)
    #: 时间槽位。账期是按时间分的，不给槽位这张表算不出属于哪个月。
    time_slots: dict[str, str] = field(default_factory=dict)
    #: 合计行标记角色。表底那行合计不丢掉，每一列金额会刚好翻倍。
    total_row_marker: str | None = None

    @property
    def mapped(self) -> list[ColumnGuess]:
        return [c for c in self.columns if c.role]

    @property
    def unknown(self) -> list[ColumnGuess]:
        return [c for c in self.columns if not c.role]

    @property
    def needs_review(self) -> list[ColumnGuess]:
        """要人拍板的列。全 exact 的话向导可以一路点下去。"""
        return [c for c in self.columns if not c.settled]

    @property
    def folded(self) -> list[ColumnGuess]:
        """列名跟现有角色都对不上、只是形态相同的那些。界面把它们收拢成一组。"""
        return [c for c in self.columns if c.no_name_match]

    def summary(self) -> str:
        """一句话说清这张表要花多少功夫。

        分档必须跟界面上的分组对得上。之前这里说「75 列要你确认」而界面只列出 10 列
        要拍板，两个数字打架——人会去找剩下那 65 列在哪，或者以为界面漏了。
        """
        settled = sum(1 for c in self.columns if c.settled)
        folded = len(self.folded)
        decide = len(self.columns) - settled - folded
        parts = [f"{len(self.columns)} 列"]
        if self.kind == "revision":
            parts.append(f"看着是「{self.base}」改版")
        parts.append(f"{settled} 列有把握")
        if decide:
            parts.append(f"{decide} 列要你拍板")
        if folded:
            parts.append(f"{folded} 列默认不映射")
        return "，".join(parts)

    def template(
        self,
        template_id: str,
        *,
        source: str = "",
        name: str = "",
        roles: dict[int, str] | None = None,
        match_columns: tuple[str, ...] = (),
    ) -> Template:
        """按草案（外加人改过的映射）生成模板对象。

        `roles` 是人最终确认的「列序号 → 角色」，空角色表示这列不要。它覆盖草案的提议：
        草案只是默认值，落库的必须是人确认的那份。

        按序号而不是列名寻址，是因为列名会重复。用列名的话，两列都叫「推广主体ID」
        就只能一起设同一个角色——实测这会把角色绑到几乎全空的那一列上，
        于是绝大多数行被当成合计行丢掉，账里只剩零头，全程不报错。
        """
        final = dict(roles) if roles is not None else {c.index: c.role for c in self.columns}
        bindings = []
        for guess in self.columns:
            role = (final.get(guess.index) or "").strip()
            if not role:
                continue
            bindings.append(ColumnBinding(
                role=role,
                columns=(guess.column,),
                occurrence=guess.occurrence,
                # 新接的表一律 required: false。缺列该由自检层按覆盖率报，
                # 不该让整张表解析不出来——那会把「少一列」升级成「这个月没数据」。
                required=False,
                # 把看出来的类型写下来，别让引擎再去按角色名猜。
                kind=_binding_kind(guess.shape),
            ))
        if dup := [r for r, n in Counter(b.role for b in bindings).items() if n > 1]:
            raise ModelError(
                "这几个角色被映到了多列上：" + "、".join(sorted(dup))
                + "。一个角色只能来自一列，多映的话取哪一列是不确定的。"
                  "重名列请只留一列，其余选「不映射」。"
            )
        picked = match_columns or self._match_columns(final)
        slots = {
            slot: role for slot, role in self.time_slots.items()
            if role in {b.role for b in bindings}
        }
        return Template(
            id=template_id,
            source=source or self.source,
            name=name,
            match_columns=picked,
            bindings=tuple(bindings),
            parse=self.parse,
            time_slots=slots,
            total_row_marker=(
                self.total_row_marker
                if self.total_row_marker in {b.role for b in bindings}
                else None
            ),
        )

    def _match_columns(self, final: dict[str, str]) -> tuple[str, ...]:
        """挑几列当识别签名。

        挑的是「映射上了、而且在这张表里独一份」的列。重复列名不能进签名——
        签名只判断列名在不在，两列同名对识别没有帮助。

        宁可挑得少而稳：签名列越多，平台改版删掉任意一列就整张表失配，
        表现是「这个月数据没到」，比认错表更难查。

        改版的表另有一条硬要求：签名里必须有基准模板没有的列。少了这一条，
        新签名就是老模板列集的子集，老版的表会被新模板抢走——老账不会报错，
        只在下一次重算时悄悄变了口径。所以新增列优先进签名。
        """
        counts: dict[str, int] = {}
        for guess in self.columns:
            counts[guess.column] = counts.get(guess.column, 0) + 1
        pool = [
            g.column for g in self.columns
            if final.get(g.index) and counts[g.column] == 1
        ]
        pool = list(dict.fromkeys(pool))
        fresh = [c for c in pool if normalize_header(c) not in self.base_columns]
        shared = [c for c in pool if normalize_header(c) in self.base_columns]
        # 新增列不占满整个签名：全用新增列的话，共有的那几列反而不参与识别，
        # 一张列名完全不同的表也可能凑巧命中。两边都留一些。
        picked = fresh[:2] + shared + fresh[2:]
        return tuple(picked)[:_MATCH_WIDTH]


# --------------------------------------------------------------------------- #
# 提议
# --------------------------------------------------------------------------- #


def propose(
    headers: list[str],
    rows: list[tuple] | None,
    model: Model,
    *,
    near_misses: list[tuple[str, tuple[str, ...]]] | None = None,
    source_hint: str = "",
    parse: ParseOptions | None = None,
) -> Draft:
    """给一张没见过的表草拟映射。

    `rows` 是采样出来的数据行，用来看值的形态；没有也能提议，只是列名没见过时
    就只能说「不知道」而不能说「这列看着是钱」。
    """
    base, base_source = _pick_base(model, near_misses or [], headers)
    draft = Draft(
        signature=signature_of(headers),
        headers=tuple(headers),
        kind="revision" if base else "new",
        base=base,
        base_columns=frozenset(known_columns(model, base)) if base else frozenset(),
        parse=parse or ParseOptions(),
    )

    vocab = vocabulary(model)
    base_roles = _base_roles(model, base)
    globals_ = role_facts(model)
    samples = _sample_columns(headers, rows or [])
    present = {normalize_header(h) for h in headers if normalize_header(h)}
    derived = _derived_names(model)
    seen: dict[str, int] = {}

    for i, raw in enumerate(headers):
        col = normalize_header(raw)
        if not col:
            continue
        occurrence = seen.get(col, 0)
        seen[col] = occurrence + 1
        shape, show = shape_of(samples.get(i, []), draft.parse.null_tokens)
        guess = _guess_column(col, base, base_roles, vocab, present)
        guess.index = i
        guess.occurrence = occurrence
        guess.shape = shape
        guess.samples = show
        if occurrence:
            # 同名的第二列默认不映。一个角色只能来自一列，两列都映上等于让引擎
            # 在两列之间随机取一列。哪一列才对只有人知道：1688 订单表里两列都叫
            # 「订单号」，第 1 列是平台导出的，第 2 列是人把合并单元格展开后贴的副本。
            guess.why = (
                f"这张表里有 {occurrence + 1} 列都叫「{col}」，这是第 {occurrence + 1} 列。"
                f"默认只映第 1 列——一个角色只能来自一列。"
                f"要是该用这一列，在这里选上，同时把第 1 列改成不映射。"
                + (f" 第 1 列的依据：{guess.why}" if guess.why else "")
            )
            guess.role = ""
            guess.confidence = "unknown"
            guess.alternatives = ()
            draft.columns.append(guess)
            continue
        _check_shape(guess, globals_)
        _flag_derived(guess, derived)
        draft.columns.append(guess)

    # 数据源要从角色反推，不能从列名重合度看。淘宝订单明细和抖音订单明细列名几乎
    # 不重合（`买家实付金额` 对 `订单应付金额`），但认出来的角色是同一套
    # ——`sub_order_id`、`order_id`、`order_time`——那就该挂同一个数据源。
    draft.source = source_hint or base_source or _pick_source(model, draft)

    # 认不出的列给一份短候选：这个数据源用得到、草案又还没映上的角色。
    # 把全模型三十几个角色摊给人选，等于没提议。
    _offer_candidates(draft, model, globals_)
    _fill_slots(draft, model, globals_)

    draft.vanished = tuple(c for c in base_roles.keys() - present) if base else ()
    refresh_warnings(draft, model)
    return draft


def refresh_warnings(draft: Draft, model: Model) -> None:
    """按当前这份列映射重算警告。

    映射被改过之后必须调一次。警告说的全是「照现在这份映射落库会出什么事」——
    映射变了警告不变，说的就是另一份映射的事，而人没法知道这一点。
    """
    draft.warnings.clear()
    _add_warnings(draft, model, role_facts(model, draft.source))


def _guess_column(
    col: str,
    base: str,
    base_roles: dict[str, str],
    vocab: dict[str, tuple[tuple[str, int], ...]],
    present: set[str],
) -> ColumnGuess:
    """一列的提议。证据强度决定可信度，不硬凑。"""
    # 一等证据：近似模板里这一列就是这个角色。改版表的绝大多数列走这条。
    if base and (role := base_roles.get(col)):
        return ColumnGuess(
            column=col, role=role, confidence="exact",
            why=f"「{base}」里这一列就是 {role}",
        )

    # 二等证据：模型别处见过同名列。同名映到多个角色时必须让人选。
    if hits := vocab.get(col):
        role, count = hits[0]
        others = tuple(r for r, _ in hits[1:])
        if len(hits) == 1:
            where = f"{count} 个模板" if count > 1 else "模型里"
            return ColumnGuess(
                column=col, role=role, confidence="exact",
                why=f"{where}都把「{col}」绑成 {role}",
                alternatives=others,
            )
        return ColumnGuess(
            column=col, role=role, confidence="likely",
            why=(
                f"「{col}」在模型里绑过 {len(hits)} 种角色："
                + "、".join(f"{r}（{n} 处）" for r, n in hits)
                + "。同名不同义，得你定"
            ),
            alternatives=others,
        )

    # 三等证据：列名最像哪个见过的列。只能是低置信度，且要说清风险。
    #
    # 排掉这张表里已经出现的列名：`线上子订单号` 和 `线上子订单编号` 同时在场时，
    # 它们必然是两个不同的列，把前者提议成后者的角色就是在复刻那次事故。
    pool = [c for c in vocab if c not in present]
    if close := difflib.get_close_matches(col, pool, n=1, cutoff=_FUZZY_FLOOR):
        like = close[0]
        role = vocab[like][0][0]
        return ColumnGuess(
            column=col, role=role, confidence="guess",
            why=(
                f"没见过「{col}」，字面最像「{like}」（那边是 {role}）。"
                f"字面像不等于同一个东西，必须你确认：曾经有人把「线上子订单号」"
                f"当成「线上子订单编号」的同义写法，全部商品成本静默挂不上订单，"
                f"不报错也不为零，只是少算"
            ),
            alternatives=tuple(r for r, _ in vocab[like][1:]),
        )

    # 什么证据都没有。候选清单等数据源定下来再补（见 _offer_candidates）。
    return ColumnGuess(column=col, confidence="unknown")


def _binding_kind(shape: Shape) -> str:
    """采样看出来的形态翻成绑定上的类型声明。

    只写 number 和 time，看着是文本也留空。原因是这个声明的分量不对称：写下
    `number` 是把一列钱救回来（角色名没命中引擎那份英文词表时它本来会留成文本），
    而写下 `text` 是把一列钱按死——一旦形态判错，这一列的金额就全变成 0，
    而且是静默的。留空则退回引擎按角色名猜，那是原本的行为，错也错得有限。

    编号也留空：19 位的订单号超过 float64 能精确表示的范围，转一次就变成另一个
    订单号，而且看不出来。
    """
    return {"number": "number", "time": "time"}.get(shape, "")


def _fill_slots(draft: Draft, model: Model, facts: dict[str, RoleFacts]) -> None:
    """把时间槽位和合计行标记按现有模板的惯例填上。

    这两项都不显眼，但漏了后果很重：没有时间槽位，这张表算不出属于哪个账期；
    没有合计行标记，表底那行合计会被当成数据，每一列金额刚好翻倍。
    两者都不该指望人在向导里想起来，得默认带上。
    """
    got = {g.role for g in draft.mapped}

    # 时间槽位：别的模板把哪个角色放在哪个槽位上，这里照着放。
    for tpl in model.templates:
        if draft.source and tpl.source != draft.source:
            continue
        for slot, role in tpl.time_slots.items():
            if role in got:
                draft.time_slots.setdefault(slot, role)

    if draft.total_row_marker:
        return
    # 合计行标记取这张表的主键那一类角色：合计行的特征就是键为空而金额有值。
    for tpl in model.templates:
        if draft.source and tpl.source != draft.source:
            continue
        if tpl.total_row_marker and tpl.total_row_marker in got:
            draft.total_row_marker = tpl.total_row_marker
            return
    for guess in draft.mapped:
        if facts.get(guess.role, RoleFacts(role="", kind="text")).kind == "id":
            draft.total_row_marker = guess.role
            return


def _shape_words(shape: Shape) -> str:
    return {
        "number": "值看着是数字",
        "time": "值看着是日期",
        "id": "值看着是订单号或运单号这类编号",
        "text": "值是文本",
        "empty": "采样到的全是空值——可能整列都没数据",
    }[shape]


# --------------------------------------------------------------------------- #
# 认不出的列：给一份排过序的短候选
# --------------------------------------------------------------------------- #


def _pick_source(model: Model, draft: Draft) -> str:
    """按认出来的角色反推该挂哪个数据源。

    比列名重合度可靠得多：各平台的列名叫法千差万别，但角色是这套模型自己的语言。
    """
    got = {g.role for g in draft.mapped}
    if not got:
        return ""
    best, score = "", 0.0
    for src in model.sources:
        roles = {b.role for t in model.templates_of(src.id) for b in t.bindings}
        if not roles:
            continue
        # 用交集占「本数据源角色」的比例，而不是占「认出来的角色」的比例：
        # 后者会偏向角色少的数据源，随便命中一两个就赢。
        hit = len(roles & got) / len(roles)
        if hit > score:
            best, score = src.id, hit
    return best if score >= 0.4 else ""


def _offer_candidates(draft: Draft, model: Model, globals_: dict[str, RoleFacts]) -> None:
    """给每个认不出的列排一份候选角色。

    候选池是「这个数据源用得到、草案还没映上」的角色——通常只有几个，人扫一眼就能定。
    排序看两件事：值的形态对不对得上，以及列名和这个角色在别处的叫法有多像。
    """
    facts = role_facts(model, draft.source) if draft.source else globals_
    taken = {g.role for g in draft.mapped}
    pool = [f for r, f in facts.items() if r not in taken]

    for guess in draft.columns:
        # 重名列的第二个已经有专门的说明（默认只映第 1 列、怎么改），别覆盖掉它。
        if guess.role or guess.derived or guess.occurrence:
            continue
        scored = [(s, n, f) for f in pool if (s := _affinity(guess, f)) > 0 for n in (_name_score(guess, f),)]
        scored.sort(key=lambda x: (-x[0], x[2].role))
        guess.alternatives = tuple(f.role for _, _, f in scored[:6])
        head = f"没见过「{guess.column}」，{_shape_words(guess.shape)}"
        if not scored:
            guess.why = head + "。这个数据源现有的角色都对不上，可能得加个新角色。"
            continue
        _, name_score, top = scored[0]
        # 只有列名真的像才敢说「最像的是它」。光形态对得上不能算依据：
        # 淘宝万相台那张表 72 列数字，而推广这个数据源只有 spend 一个数字角色，
        # 逐列都写「最像的是 spend」——每列都指向同一个角色，这句话就不含信息了，
        # 还会诱导人把「展现量」映成花费。
        if name_score >= _NAME_FLOOR:
            guess.why = head + f"。最像的是 {top.role}" + (f"（{top.hint}）" if top.hint else "")
        else:
            # 这一档的说明不写在每一行上：落在这里的列会有几十个，同一段话重复几十遍
            # 就把真正要拍板的那几列埋掉了。界面收拢成一组共用一句。
            guess.no_name_match = True
            guess.why = head + f"，形态上和 {'、'.join(guess.alternatives[:2])} 是同一类。"


#: 列名相似度要到这个数才算得上候选。
#:
#: 拿真实表标出来的：`买家实付金额` 对 buyer_paid 的 `订单应付金额` 是 0.50、
#: `物流单号` 对 tracking_no 的 `运单号` 是 0.57，都对；而 `退款金额` 对
#: `订单应付金额` 是 0.40、`退款状态` 对 `实付款（元）` 是 0.20，都不对。
#: 卡在这两档之间。
_NAME_FLOOR = 0.45


def _affinity(guess: ColumnGuess, fact: RoleFacts) -> float:
    """这一列有多像这个角色。0 表示不该列为候选。

    只排序、不采用：候选再像也只是下拉框里的默认高亮，角色仍然是空的，
    得人点一下才算。这条界限不能松——同一套字面相似度，`线上子订单号` 对
    `线上子订单编号` 能打到 0.92，而那两个是完全不同的两列。
    """
    if fact.kind == guess.shape and guess.shape != "text":
        shape_score = 1.0
    elif guess.shape == "empty":
        # 整列空的看不出形态，别因此排除任何角色。
        shape_score = 0.3
    elif fact.kind == "text" or guess.shape == "text":
        shape_score = 0.4
    elif {fact.kind, guess.shape} == {"id", "number"}:
        # 编号常被存成纯数字，位数不够长就会被看成数字。这一对不算矛盾。
        shape_score = 0.6
    else:
        return 0.0

    name_score = _name_score(guess, fact)
    # 形态严丝合缝的可以只靠形态入选（新表的日期列列名千奇百怪）；
    # 形态只是不矛盾的，就必须列名也像，否则会把整个角色表都列成候选，
    # 排在最前面的那个还会被人当成建议。
    if shape_score < 1.0 and name_score < _NAME_FLOOR:
        return 0.0
    return shape_score + name_score


def _name_score(guess: ColumnGuess, fact: RoleFacts) -> float:
    """列名和这个角色在别处的叫法有多像。

    `买家实付金额` 对上 buyer_paid 的 `订单应付金额`、`物流单号` 对上 tracking_no 的
    `运单号`，靠的都是这个。
    """
    return max(
        (difflib.SequenceMatcher(None, guess.column, c).ratio() for c in fact.columns),
        default=0.0,
    )


# --------------------------------------------------------------------------- #
# 结果列：映射它就是重复计算
# --------------------------------------------------------------------------- #


def _derived_names(model: Model) -> dict[str, str]:
    """模型里的口径名与损益项名 → 它是什么。

    店长的表大多是在平台导出上手工加列算出来的：淘宝订单明细那 30 列里，
    `销售收入`、`毛利`、`利润率`、`李秋雨提成` 全是表里自己算的。
    这些列一旦被映成原始数据，同一笔钱会既从对账表进来又从这列进来，翻倍。
    """
    out: dict[str, str] = {}
    for n in model.statement:
        if key := normalize_header(n.name):
            out[key] = f"损益表的「{n.name}」"
    for m in model.metrics:
        if key := normalize_header(m.name):
            out.setdefault(key, f"口径「{m.name}」")
    return out


def _flag_derived(guess: ColumnGuess, derived: dict[str, str]) -> None:
    # 透视字段前缀是客观痕迹，不用猜：`求和项:花费` 是 Excel 透视表留下的，
    # 它是花费那一列汇总出来的结果。判据直接用引擎那份，别另写一套会分叉。
    if any(guess.column.startswith(p) for p in PIVOT_PREFIXES):
        guess.why = (
            f"「{guess.column}」带着 Excel 数据透视表的字段前缀，是从别的列汇总出来的。"
            f"映射它等于把同一笔钱记两遍。"
        )
        guess.role = ""
        guess.confidence = "unknown"
        guess.alternatives = ()
        guess.derived = True
        return
    if (what := derived.get(guess.column)) is None:
        return
    guess.why = (
        f"这列名和{what}同名，看着是他们在表里自己算出来的结果。"
        f"结果列不该映射——同一笔钱会既从原始表进来又从这列进来，直接翻倍。"
        + (f" 原提议：{guess.role}（{guess.why}）" if guess.role else "")
    )
    guess.role = ""
    guess.confidence = "unknown"
    guess.alternatives = ()
    guess.derived = True


def _check_shape(guess: ColumnGuess, facts: dict[str, RoleFacts]) -> None:
    """提议的角色和值的形态对不上就降级。

    这是唯一一处能挡住「名字对、内容不对」的地方：平台把某列的含义换掉但列名不变
    是真会发生的，而按列名匹配的提议对此完全无感。
    """
    if not guess.role or guess.shape == "empty":
        return
    fact = facts.get(guess.role)
    if fact is None or fact.kind == "text":
        return
    ok = {
        "number": {"number"},
        "time": {"time"},
        # 编号列被存成纯数字很常见，反过来金额列不会是 11 位整数。
        "id": {"id", "number", "text"},
    }.get(fact.kind, set())
    if guess.shape in ok:
        return
    guess.confidence = "guess"
    guess.why += (
        f"。但对不上：{guess.role} 在模型里是{_kind_words(fact.kind)}，"
        f"这列的值却是{_kind_words(guess.shape)}"
        + (f"（如 {guess.samples[0]}）" if guess.samples else "")
    )


def _kind_words(kind: str) -> str:
    return {"number": "数字", "time": "日期", "id": "编号", "text": "文本", "empty": "空"}.get(kind, kind)


def _pick_base(
    model: Model,
    near_misses: list[tuple[str, tuple[str, ...]]],
    headers: list[str],
) -> tuple[str, str]:
    """挑一个基准模板。取差得最少的那个。

    识别阶段算出来的 near_misses 直接用；没有的话自己按列名重合度找一遍，
    因为提议器也会被单独调用（比如人想拿现成的表重新生成模板）。
    """
    if near_misses:
        tpl_id = min(near_misses, key=lambda x: len(x[1]))[0]
        try:
            return tpl_id, model.template(tpl_id).source
        except KeyError:
            return "", ""

    present = {normalize_header(h) for h in headers if normalize_header(h)}
    best, score = "", 0.0
    for tpl in model.templates:
        known = known_columns(model, tpl.id)
        if not known:
            continue
        overlap = len(known & present) / len(known)
        if overlap > score:
            best, score = tpl.id, overlap
    if score >= 0.5:
        return best, model.template(best).source
    return "", ""


def _base_roles(model: Model, template_id: str) -> dict[str, str]:
    """基准模板的列名 → 角色。"""
    if not template_id:
        return {}
    try:
        tpl = model.template(template_id)
    except KeyError:
        return {}
    out: dict[str, str] = {}
    for b in tpl.bindings:
        for c in b.columns:
            if key := normalize_header(c):
                out.setdefault(key, b.role)
    return out


def _sample_columns(headers: list[str], rows: list[tuple]) -> dict[int, list[Any]]:
    """按列收集采样值。"""
    out: dict[int, list[Any]] = {i: [] for i in range(len(headers))}
    for row in rows[:_SAMPLE_ROWS]:
        for i in range(min(len(row), len(headers))):
            out[i].append(row[i])
    return out


def _add_warnings(draft: Draft, model: Model, facts: dict[str, RoleFacts]) -> None:
    """把「落库之后可能出什么问题」提前说清。

    向导最容易骗人的地方是：模板加上了，表也解析了，人以为接完了，但没有任何指标
    从这个数据源取数，钱一分都没进损益表。这必须在确认之前就说。
    """
    if not draft.source:
        draft.warnings.append("还没定挂到哪个数据源。数据源决定这张表供给哪些指标，不能留空。")
    else:
        try:
            metrics = model.metrics_of(draft.source)
        except KeyError:
            metrics = ()
        if not metrics:
            draft.warnings.append(
                f"数据源「{draft.source}」目前没有任何指标从它取数。"
                f"这张表接上之后能解析、能查，但不会进损益表——要进得再配指标。"
            )

    if draft.vanished:
        draft.warnings.append(
            f"「{draft.base}」有 {len(draft.vanished)} 列在这张表里没有了："
            + "、".join(draft.vanished[:6])
            + "。要么平台改名了（在上面把新列名映到同一个角色），"
            + "要么真删了（那要看有没有指标靠它取数）。"
        )

    if left := [c for c in draft.unknown if not c.derived and c.shape == "number"]:
        draft.warnings.append(
            f"有 {len(left)} 列是数字但没映上："
            + "、".join(c.column for c in left[:8])
            + ("…" if len(left) > 8 else "")
            + "。店长的表大多是在平台导出上手工加列算出来的，这些通常就是那些中间列，"
              "不映是对的。但要是里面有该进账的原始数据，现在不指出来，以后不会有人再看这张表。"
        )

    if guesses := [c for c in draft.columns if c.confidence == "guess"]:
        draft.warnings.append(
            f"{len(guesses)} 列是按字面猜的："
            + "、".join(c.column for c in guesses[:6])
            + "。猜错不会报错，只会静默少算钱，请逐列核对。"
        )

    if dup := _duplicate_roles(draft):
        draft.warnings.append(
            "同一个角色被映了多列：" + "、".join(dup)
            + "。一个角色只能来自一列，否则取数取哪列是不确定的。"
        )

    missing = _missing_required(draft, model, facts)
    if missing:
        draft.warnings.append(
            "这个数据源的指标要用到、但这张表里没映上的角色："
            + "、".join(missing)
            + "。缺了这些指标就取不到数。"
        )

    for role, (level, who) in missing_spine_roles(draft, model).items():
        draft.warnings.append(
            f"脊柱少了 {role}：{who}。"
            + ("缺了引擎算到那一步会直接报错。" if level == "hard"
               else "缺了不报错，但口径会悄悄放宽。")
        )


def missing_spine_roles(draft: Draft, model: Model) -> dict[str, tuple[str, str]]:
    """草案是脊柱表时，别人指望它提供、它却没有的角色。"""
    try:
        source = model.source(draft.source) if draft.source else None
    except KeyError:
        source = None
    if source is None or not source.is_spine:
        return {}
    mapped = {g.role for g in draft.mapped}
    return {r: v for r, v in spine_roles(model).items() if r not in mapped}


def _duplicate_roles(draft: Draft) -> list[str]:
    counts: dict[str, int] = {}
    for guess in draft.mapped:
        counts[guess.role] = counts.get(guess.role, 0) + 1
    return sorted(r for r, n in counts.items() if n > 1)


def _missing_required(draft: Draft, model: Model, facts: dict[str, RoleFacts]) -> list[str]:
    """这个数据源的指标要用、草案却没映上的角色。"""
    if not draft.source:
        return []
    mapped = {g.role for g in draft.mapped}
    needed: set[str] = set()
    for m in model.metrics_of(draft.source):
        needed |= set(m.value.of)
        needed |= {p.field for p in m.where}
        if m.link and m.link.key:
            needed.add(m.link.key)
    return sorted(r for r in needed - mapped if r in facts)


def spine_roles(model: Model) -> dict[str, tuple[str, str]]:
    """脊柱必须提供的角色 → (硬还是软, 谁要用它)。

    这些要求不写在脊柱自己的数据源上，而散落在别人的指标里：对账表要按
    `alloc_ratio` 分摊、运费要挂到 `order_id`、覆盖率的分母要按 `pay_time` 筛。
    换句话说，接一张新的订单明细表时，「这张表必须有哪几列」这个问题的答案
    不在这张表的定义里。

    没有这个函数，人在向导里只能看到「这个数据源没有指标」，然后一路点确认，
    落库之后引擎在分摊那一步直接抛异常——而异常信息说的是 `alloc_ratio`，
    跟他刚才做的事看不出关系。

    硬的缺了引擎会抛异常；软的缺了只是悄悄放宽口径，也得说。
    """
    out: dict[str, tuple[str, str]] = {}
    for m in model.metrics:
        who = m.name or m.id
        if m.link and m.link.to and "." in m.link.to:
            out.setdefault(m.link.to.split(".", 1)[1], ("hard", f"{who} 要挂到它"))
        if m.allocate and m.allocate.by:
            out[m.allocate.by] = ("hard", f"{who} 要按它分摊")
        for p in m.expect:
            out.setdefault(p.field, ("soft", f"{who} 的覆盖率分母要按它筛"))
    return out


__all__ = [
    "ColumnGuess",
    "Confidence",
    "Draft",
    "RoleFacts",
    "Shape",
    "known_columns",
    "propose",
    "role_facts",
    "shape_of",
    "vocabulary",
]
