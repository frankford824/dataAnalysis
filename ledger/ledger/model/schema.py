"""建模层：六类对象的结构定义。

引擎解释执行这批对象，它们是数据不是代码。换一家公司只换这批数据，引擎代码不动。

六类对象
    SourceContract  数据源契约：需要哪些数据、归谁维护、缺了影响谁
    Template        模板：表头签名到字段角色的映射，以及该版本固定的格式与符号约定
    Metric          指标五元组：数据源、取值、关联键与层级、符号方向、责任来源
    StatementNode   公式树：损益表的结构，层数不限
    DictionaryEntry 科目字典：平台原始科目到统一科目
    Check           校验规则：自检层在结账前执行的拦截条件
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

#: 关联层级。引擎不猜层级，由模型声明。
Grain = Literal["order", "product", "period", "store", "unlinked"]

#: 金额符号约定。实测同一列内存在正负混存，符号不能从数据推断，只能绑模板版本。
SignRule = Literal["as_is", "negate", "abs_negate", "abs_positive", "by_direction"]

#: 时间语义槽位。各平台叫法不同，归入五类。
TimeSlot = Literal["order_date", "pay_date", "ship_date", "confirm_date", "settle_date", "spend_date"]

#: 责任角色。完整度机制靠它回答"缺什么、该找谁"。
OwnerRole = Literal["shop_owner", "warehouse", "logistics", "operations", "finance"]

Cadence = Literal["daily", "weekly", "monthly", "once", "on_demand"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# 表达式
# --------------------------------------------------------------------------- #


class ValueExpr(Base):
    """指标取值表达式。算子集合刻意保持最小。

    实测 2288 个 DAX 度量值只用了 5 个函数，以下算子足以覆盖全部：

        sum          SUM(列)，给多个列就是逐列相加后求和
        sum_product  SUMX(表, 列A * 列B)
        count        计数
        constant     常量
    """

    op: Literal["sum", "sum_product", "count", "constant"]
    of: tuple[str, ...] = ()
    value: float | None = None

    @model_validator(mode="after")
    def _check(self) -> ValueExpr:
        need = {"sum": 1, "sum_product": 2, "count": 0, "constant": 0}[self.op]
        if self.op == "constant":
            if self.value is None:
                raise ValueError("constant 取值必须给 value")
        elif len(self.of) < need:
            raise ValueError(f"{self.op} 至少需要 {need} 个字段角色，收到 {len(self.of)}")
        return self


class NodeExpr(Base):
    """公式树节点表达式。操作数是指标 id 或其他节点 id。"""

    op: Literal["add", "negate", "ratio", "constant"]
    of: tuple[str, ...] = ()
    value: float | None = None

    @model_validator(mode="after")
    def _check(self) -> NodeExpr:
        if self.op == "ratio" and len(self.of) != 2:
            raise ValueError("ratio 需要恰好 2 个操作数（分子, 分母）")
        if self.op == "negate" and len(self.of) != 1:
            raise ValueError("negate 需要恰好 1 个操作数")
        if self.op == "constant" and self.value is None:
            raise ValueError("constant 节点必须给 value")
        if self.op == "add" and not self.of:
            raise ValueError("add 至少需要 1 个操作数")
        return self


class FieldMatch(Base):
    """从哪个字段角色取值、什么条件下适用。

    规则链的每一环都是一个 FieldMatch。实测支付宝账务明细取订单号需要 7 条规则、
    归类需要 8 条规则，都是"按优先级依次尝试，第一条命中的生效"。
    """

    field: str
    #: 正则提取，取第 1 个捕获组。不给就用整个字段值。
    extract: str | None = None
    #: 字段值包含任一子串才适用。
    contains: tuple[str, ...] = ()
    #: 字段值等于任一值才适用。
    equals: tuple[str, ...] = ()
    #: 字段值匹配这个正则才适用。用于 contains 表达不了的组合条件。
    #:
    #: 实测支付宝同一笔费用的扣和退在备注里只差最后两个字：
    #: 「品牌新享-首单拉新计划(KY_ITEM)(订单号)扣款」是软件服务费，
    #: 结尾换成「退款」就是交易退款。光看包含哪个费用名分不开，
    #: 光看是不是「退款」结尾也不行——营销费用的退回也是这个后缀。
    matches: str | None = None
    #: 字段非空才适用。
    notnull: bool = True

    @field_validator("extract", "matches")
    @classmethod
    def _compilable(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class Bridge(Base):
    """跨表回查。

    运费表本身没有订单号，只有运单号，要先去订单明细按物流单号回查主订单编号，
    查不到再去聚水潭按快递单号回查原始线上订单号。这是两级回查，不是简单关联。
    """

    #: 中间表的数据源 id。
    source: str
    #: 中间表里用于匹配的字段角色。
    match: str
    #: 中间表里取出的字段角色。
    take: str


class KeyRule(Base):
    """取关联键的一条规则。按声明顺序尝试，第一条命中的生效。"""

    when: FieldMatch
    via: Bridge | None = None
    #: 命中即判定为"这笔钱不参与核算"。用于显式排除——余利宝申购、转出到网商银行
    #: 这类根本不是经营流水，必须显式排除而不是让它挂不上订单混在异常里。
    exclude: bool = False
    note: str = ""


class ClassifyRule(Base):
    """归类的一条规则。按声明顺序尝试，第一条命中的生效。"""

    #: 查科目字典。通常是链条的第一环。
    dictionary: bool = False
    when: FieldMatch | None = None
    major: str | None = None
    minor: str | None = None
    #: 命中即排除。保证金解冻、天猫保证金充值这类要清空费项。
    exclude: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> ClassifyRule:
        if self.dictionary:
            if self.when or self.major or self.exclude:
                raise ValueError("dictionary 规则不能同时带 when / major / exclude")
        elif not self.when:
            raise ValueError("非字典规则必须给 when")
        elif not (self.major or self.exclude):
            raise ValueError("非字典规则必须给 major 或 exclude")
        return self


class Predicate(Base):
    """过滤条件。对应 DAX 里 CALCULATE 的筛选参数。"""

    field: str
    op: Literal["eq", "ne", "in", "not_in", "contains", "not_contains", "gt", "lt", "notnull"]
    value: str | float | tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _check(self) -> Predicate:
        if self.op in ("in", "not_in") and not isinstance(self.value, tuple):
            raise ValueError(f"{self.op} 的 value 必须是列表")
        if self.op != "notnull" and self.value is None:
            raise ValueError(f"{self.op} 必须给 value")
        return self


# --------------------------------------------------------------------------- #
# 1. 数据源契约
# --------------------------------------------------------------------------- #


class SourceContract(Base):
    """声明这个系统需要哪些数据、每份数据归谁维护。

    这是完整度机制的基础。系统随时知道缺什么、该找谁，不需要额外维护责任人表。
    """

    id: str
    name: str
    platform: str = "*"
    owner_role: OwnerRole
    cadence: Cadence
    #: 这份数据供给哪些指标。缺失时这些指标显示为"数据未到"而不是 0。
    provides: tuple[str, ...] = ()
    #: 是否为订单脊柱。脊柱是其他数据挂钩的目标，缺了整个店无法核算。
    is_spine: bool = False
    #: 结账是否必须有它。
    required_for_close: bool = True
    #: 文件名里出现这些词就认为文件属于本数据源。
    #: 补发表是从聚水潭成本表按订单类型筛出来另存的，两张表表头一模一样，
    #: 靠表头签名区分不了，只能靠文件来源区分。
    filename_hints: tuple[str, ...] = ()
    #: 这些角色的组合唯一确定一行。多份文件落到同一个数据源时按它去重，而不是直接拼接。
    #:
    #: 有些数据是全公司一张主表（运费、小额打款），每个店长导出的都是同一份，
    #: 只是各自加了自己那套关联列。实测三个店交上来的运费文件逐行相同、
    #: 299,554 个运单号完全重合——直接拼接会让运费变成三倍。
    #:
    #: 这种事不能靠叮嘱店长「别重复传」来防：交叉重叠是协作的常态，
    #: 得让引擎在结构上不可能算错。声明了去重键，重复交多少份都是同一个结果。
    dedupe_key: tuple[str, ...] = ()
    #: 这份数据交上来是全公司的，每家店只取属于自己订单的那部分。
    #:
    #: 和 dedupe_key 是两件事：去重键管的是「同一份被交了好几遍」，
    #: 这个管的是「一份里混着所有店」。运费和小额打款两者都成立，但将来完全可能
    #: 出现财务统一交一份、不会重复交的公司级表。
    #:
    #: 为什么必须标出来：全公司运费表 30 万条运单里只有 2,576 条属于 1688 星泽，
    #: 其余挂不到这家店的订单上。不标的话这 54.8 万会被报成本店「没进利润的钱」，
    #: 而它本来就不是这家店的钱。这种误报比不报更糟——它会让人不再信这个提示。
    company_wide: bool = False
    note: str = ""


# --------------------------------------------------------------------------- #
# 2. 模板
# --------------------------------------------------------------------------- #


class ColumnBinding(Base):
    """字段角色到实际列名的绑定。

    一个角色可以有多个候选列名，但**候选之间必须语义等价**。现有系统的教训是
    把 `线上子订单号` 和 `线上子订单编号` 当成同一角色的候选，前者拼错了却
    因为能回退命中后者而不报错，全部商品成本静默挂不上订单。
    """

    role: str
    #: 实际列名。多个候选按顺序取第一个命中的。
    columns: tuple[str, ...]
    #: 重复列名时取第几个（0 起）。千牛明细有 3 种签名存在重复列名，涉及 87 个文件。
    occurrence: int = 0
    #: 取数后取反。用来把各平台不一致的符号约定拉齐。
    #:
    #: 同一家店的两张对账表，支出列的符号约定就是反的，而且列名里写着：
    #: 支付宝叫「支出金额（-元）」，括号里带减号，值本身是负数；
    #: 微信叫「支出金额(元)」，不带减号，值是正数。
    #:
    #: 不在绑定层拉齐，下游每个口径都得记住「这个来源的支出是正是负」，
    #: 迟早会漏。实测漏掉微信这一处，营销费用、销售退款、物流运费三项全部符号翻转。
    negate: bool = False
    required: bool = True

    @field_validator("columns")
    @classmethod
    def _nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("columns 不能为空")
        return v


class DedupRule(Base):
    """粒度归一。声明哪些字段是父级字段、聚合前按什么键去重。

    实测聚水潭应付金额直接求和相对去重后放大 1.54 到 3.03 倍。
    """

    #: 这些字段是父级（主订单级）字段，行级求和会重复计算。
    parent_fields: tuple[str, ...] = ()
    #: 去重键。聚水潭无自然唯一键（三键组合仍有重复），此时留空并依赖代理主键。
    key: tuple[str, ...] = ()


class ParseOptions(Base):
    """格式陷阱处理参数。按模板版本固定，不靠猜。"""

    encoding: str = "utf-8-sig"
    delimiter: str | None = None
    #: xlsx 声明的 <dimension> 常被写成 A1，不重置会把约 160 个文件读成只有 1 行。
    reset_xlsx_dimension: bool = True
    #: 制表符方向不一致：拼多多与千牛后置、抖店前导，必须双向去除。
    strip_tabs: bool = True
    #: 表头在第几行（0 起）。
    header_row: int = 0
    #: 数据起始行相对表头的偏移。部分平台表头下有一行汇总或说明。
    skip_after_header: int = 0
    sheet: str | None = None
    #: 空值表示。拼多多广告与抖店 ROI 用 `-`。
    null_tokens: tuple[str, ...] = ("", "-", "--", "无", "N/A", "null", "NULL")


class Template(Base):
    """一个表头签名对应一个模板版本。

    未登记的签名必须报警。静默丢列是这个行业最大的隐形杀手。
    """

    id: str
    source: str
    name: str = ""
    #: 判定命中所需的列名。全部出现才算命中。
    match_columns: tuple[str, ...]
    #: 用于区分同源不同版本：这些列出现则**不**是本模板。
    exclude_columns: tuple[str, ...] = ()
    bindings: tuple[ColumnBinding, ...] = ()
    parse: ParseOptions = ParseOptions()
    #: 金额符号约定绑模板版本。实测四种约定并存。
    sign: SignRule = "as_is"
    #: sign 为 by_direction 时，方向所在的字段角色与表示"支出"的取值。
    direction_role: str | None = None
    direction_outflow_values: tuple[str, ...] = ()
    dedup: DedupRule = DedupRule()
    #: 时间槽位映射：槽位 → 字段角色。
    time_slots: dict[TimeSlot, str] = Field(default_factory=dict)
    #: 取关联键的规则链。声明了就用它，指标的 link.key 只用于没有规则链的简单场合。
    key_rules: tuple[KeyRule, ...] = ()
    #: 归类的规则链。
    classify_rules: tuple[ClassifyRule, ...] = ()
    #: 表底合计行的特征：这个角色为空则整行是合计行，必须丢掉。
    #: 实测订单明细表底有一行合计，不丢会让每一列金额刚好翻倍。
    total_row_marker: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> Template:
        if self.sign == "by_direction" and not self.direction_role:
            raise ValueError(f"{self.id}: sign=by_direction 必须声明 direction_role")
        roles = {b.role for b in self.bindings}
        for slot, role in self.time_slots.items():
            if role not in roles:
                raise ValueError(f"{self.id}: 时间槽位 {slot} 指向未定义的角色 {role}")
        if self.total_row_marker and self.total_row_marker not in roles:
            raise ValueError(f"{self.id}: 合计行标记角色 {self.total_row_marker} 未定义")
        for i, rule in enumerate(self.key_rules, 1):
            if rule.when.field not in roles:
                raise ValueError(f"{self.id}: 取键规则 {i} 引用了未定义的角色 {rule.when.field}")
        for i, rule in enumerate(self.classify_rules, 1):
            if rule.when and rule.when.field not in roles:
                raise ValueError(f"{self.id}: 归类规则 {i} 引用了未定义的角色 {rule.when.field}")
        return self

    @property
    def signature(self) -> str:
        """表头签名。列名集合的哈希，与列顺序无关。"""
        return signature_of(self.match_columns)


def signature_of(columns: object) -> str:
    """计算表头签名。用于比对已登记模板版本。"""
    if isinstance(columns, str):
        columns = [columns]
    names = sorted({normalize_header(c) for c in columns if normalize_header(c)})  # type: ignore[union-attr]
    return hashlib.sha256("\x1f".join(names).encode()).hexdigest()[:16]


_WS = re.compile(r"[\s\u3000\ufeff]+")


def normalize_header(name: object) -> str:
    """表头归一。去空白、去 BOM、全角括号折半角。"""
    if name is None:
        return ""
    s = _WS.sub("", str(name))
    return s.translate(str.maketrans("（）［］｛｝：，．／", "()[]{}:,./"))


# --------------------------------------------------------------------------- #
# 3. 指标定义
# --------------------------------------------------------------------------- #


class LinkRule(Base):
    """关联规则。按声明的键关联，并归集到声明的层级。"""

    #: 本源用于关联的字段角色。
    key: str
    #: 从文本正则提取键。支付宝账务明细没有订单号列，订单号埋在备注里。
    extract: str | None = None
    #: 关联目标。形如 `order.sub_order_id`。
    to: str | None = None
    grain: Grain = "order"
    #: 命中率低于此值告警。
    min_hit_rate: float = 0.95

    @field_validator("extract")
    @classmethod
    def _compilable(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class Allocation(Base):
    """分摊方式。

    源数据的粒度常常粗于脊柱：对账表是主订单级，脊柱是子订单级；广告报表是商品级，
    脊柱是订单行级。把粗粒度金额落到脊柱行上有两种做法，实测两种都在用：

      ratio  按比例分摊。比例是脊柱上的一列（收入分配率 = 子订单收入 / 主订单收入）。
             淘宝的对账表是主订单级，所以必须按收入占比拆到子订单。
      even   组内均分。除数是**脊柱**里共享同一个键的行数，不是源表行数。
             广告费按该商品的订单行数均分，代发成本按该主订单的子订单数均分。

    除数来自脊柱这一点很关键：它决定了核算必须投影到脊柱行上，不能只在源表侧聚合。
    """

    mode: Literal["ratio", "even"]
    #: ratio 模式：比例所在的字段角色，取自脊柱。
    by: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Allocation:
        if self.mode == "ratio" and not self.by:
            raise ValueError("ratio 分摊必须声明 by（比例字段角色）")
        return self


class PlatformRule(Base):
    """某个平台上这条指标的差异写法。

    同一个科目在各平台的算法确实不同，而且差异集中在「怎么挂到订单上」和
    「怎么摊到脊柱行上」这两件事上。以发货运费为例，三家店的公式分别是：

        淘宝    按收入分配率比例摊到子订单
        1688    sumifs(运费表总金额, 运单号) / countifs(订单明细运单号)  → 组内均分
        抖音    sumifs(运费表总金额, 子订单) 直接挂，不摊

    把这些差异写成三个独立指标的话，损益表上「发货运费」这一行就要列出三个
    指标 id，每加一个平台就得改损益表。差异其实只在算法，科目还是同一个科目，
    所以让它留在同一条指标里，按平台覆盖需要改的那几项。
    """

    platform: str
    link: LinkRule | None = None
    #: 覆盖过滤条件。给空列表就是这个平台不过滤。
    #: （留空表达不了「不过滤」，那和「不覆盖」分不开，所以用 None 表示不覆盖。）
    where: tuple[Predicate, ...] | None = None
    #: 覆盖该平台的覆盖率分母。语义同 where：空列表表示按全部订单算。
    expect: tuple[Predicate, ...] | None = None
    allocate: Allocation | None = None
    #: 该平台不分摊，源金额直接落到脊柱行。用来清掉缺省的分摊设置。
    #: （单靠 allocate 留空表达不了「不摊」，那和「不覆盖」分不开。）
    direct: bool = False
    major: str | None = None
    #: 该平台不算这条指标。1688 的推广费用列人工填的全是 0，没有数据源。
    disabled: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> PlatformRule:
        if self.direct and self.allocate is not None:
            raise ValueError("direct 与 allocate 不能同时给：要么不摊，要么指定摊法")
        return self


class Metric(Base):
    """指标五元组。加一个新指标不需要改代码，只需新增一条定义。"""

    id: str
    name: str
    source: str
    #: 只对这个平台生效。`*` 表示各平台通用。
    #:
    #: 各平台的利润口径本来就不一样，这不是可以统一掉的差异：
    #: 淘宝按收入分配率比例分摊、收入支出拆成七个费项；
    #: 1688 按 COUNTIFS 均摊、收支各只有一条不拆费项、补发并在聚水潭成本里；
    #: 抖音的销售收入直接按子订单号取动账金额净额、不分摊。
    #: 三家店的订单明细表里各自写着自己的公式，模型层要能照实表达。
    platform: str = "*"
    value: ValueExpr
    #: 过滤条件，全部满足才纳入。
    where: tuple[Predicate, ...] = ()
    #: 这个科目预期覆盖脊柱上哪些订单。覆盖率的分母。
    #:
    #: 不是每个订单都该有每项成本：没发货的订单不会有出库成本，聚水潭里根本没有
    #: 这一行。分母若按全部订单算，覆盖率就永远不达标，而缺口是虚的——三家店实测
    #: 缺商品成本的订单里 80%~95% 没有运单号。
    #: 留空表示预期覆盖全部订单。
    expect: tuple[Predicate, ...] = ()
    #: expect 的人话说法，例如「已发货」。自检层要告诉用户分母是哪一批订单，
    #: 否则「1,060 笔里覆盖了 98%」这句话没法核对。
    expect_label: str = ""
    link: LinkRule | None = None
    sign: SignRule = "as_is"
    #: 时间归属依据。广告费按花费日而非下单日。
    time_basis: TimeSlot = "order_date"
    #: 该科目是否天然无订单号。为真时挂不上订单不算异常。
    naturally_unlinked: bool = False
    #: 分摊方式。为空表示源金额直接落到脊柱行，不拆。
    allocate: Allocation | None = None
    #: 只取归类为这个口径项的行。对账表一张表供给多个指标，靠它区分。
    major: str | None = None
    #: 各平台的差异写法。没列到的平台走上面的缺省算法。
    by_platform: tuple[PlatformRule, ...] = ()
    note: str = ""

    def for_platform(self, platform: str) -> Metric | None:
        """取这条指标在某个平台上的实际算法。该平台不算这条时返回 None。"""
        if self.platform not in ("*", platform) and platform != "*":
            return None
        rule = next((r for r in self.by_platform if r.platform == platform), None)
        if rule is None:
            return self
        if rule.disabled:
            return None
        return self.model_copy(update={
            "link": rule.link or self.link,
            "where": self.where if rule.where is None else rule.where,
            "expect": self.expect if rule.expect is None else rule.expect,
            "allocate": None if rule.direct else (rule.allocate or self.allocate),
            "major": rule.major or self.major,
        })


# --------------------------------------------------------------------------- #
# 4. 公式树
# --------------------------------------------------------------------------- #


class StatementNode(Base):
    """损益表节点。层数不限，结构完全由模型定义。"""

    id: str
    name: str
    #: 由子节点或指标加总而来。与 formula 二选一。
    children: tuple[str, ...] = ()
    #: 显式表达式。
    formula: NodeExpr | None = None
    #: 界面呈现层级。1 为主界面 5 组，2 为展开的 11 项。
    level: int = 1
    #: 呈现格式。
    display: Literal["amount", "percent", "count"] = "amount"
    #: 为真时该节点是最终结果行，数据不全时不出数。
    is_total: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> StatementNode:
        if bool(self.children) == bool(self.formula):
            raise ValueError(f"{self.id}: children 与 formula 必须且只能有一个")
        return self


# --------------------------------------------------------------------------- #
# 5. 科目字典
# --------------------------------------------------------------------------- #


class DictionaryEntry(Base):
    """平台原始科目到统一科目的映射。引擎只负责查表与未命中告警。"""

    platform: str
    #: 平台原始科目名。
    raw: str
    minor: str
    major: str
    #: 该科目天然无订单号。拼多多那张映射表已标注。
    naturally_unlinked: bool = False


# --------------------------------------------------------------------------- #
# 6. 校验规则
# --------------------------------------------------------------------------- #


class Check(Base):
    """自检层在结账前执行的拦截条件。"""

    id: str
    name: str
    kind: Literal[
        "link_rate",         # 关联命中率达标：附属数据挂上了订单
        "spine_coverage",    # 覆盖率达标：订单拿到了这项数据
        "no_unclassified",   # 无未归类科目
        "completeness",      # 数据源到齐
        "tie_out",           # 勾稽等式成立
        "unlinked_disclosed",  # 未归属金额已显式呈现
    ]
    #: 阻断结账。为假时只提示。
    blocking: bool = True
    #: kind 相关参数。
    metric: str | None = None
    threshold: float | None = None
    left: NodeExpr | None = None
    right: NodeExpr | None = None
    tolerance: float = 0.01
    #: 未通过时给用户看的人话。异常只说人话。
    message: str = ""


# --------------------------------------------------------------------------- #
# 7. 店铺注册表
# --------------------------------------------------------------------------- #


class Store(Base):
    """一家店。数据归属的单位，也是账期结算的单位。

    店铺和法人主体是多对一：实测 1688星泽气球派对 和 抖音浅花涧节日装饰 同属
    义乌星泽天成供应链管理有限公司。这层对应关系必须能配，不能从店名推——
    店名里带的是平台，不是主体。

    主体也不总能从数据里读到：1688 收款明细有「归属主体名称」、抖音对账单有
    「商户主体名称」，而淘宝的支付宝和微信账单根本不带主体信息。能读到的地方
    引擎拿来和这里配的核对，读不到的地方只能靠配。
    """

    id: str
    #: 店铺全名。交上来的文件名里带的就是这个，形如「聚水潭成本-淘宝喜必顺.xlsx」。
    name: str
    platform: str
    #: 法人主体名。数据里读不到的店只能靠配，留空则自检提示未配置。
    entity: str = ""
    entity_tax_id: str = ""
    #: 归档店铺不参与新账期，历史账仍可查。关店不等于删数据。
    archived: bool = False
    #: 文件名里认这家店的别名。改过名或简称都放这里。
    aliases: tuple[str, ...] = ()
    note: str = ""

    def owns(self, filename: str) -> bool:
        """这个文件名是不是这家店的。"""
        return any(a and a in filename for a in (self.name, *self.aliases))


#: 平台名的常见前缀写法。只用于给未登记店铺提建议，不参与任何计算——
#: 猜出来的东西不能进账，登记必须由人确认。
_PLATFORM_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("taobao", ("淘宝", "天猫", "TB", "tmall")),
    ("alibaba1688", ("1688", "阿里巴巴", "阿里")),
    ("douyin", ("抖音", "抖店")),
    ("pdd", ("拼多多", "PDD", "pdd")),
    ("jd", ("京东", "JD", "jd")),
)


def guess_platform(store_name: str) -> str:
    """从店名前缀猜平台。只用于提示，返回空串表示猜不出来。"""
    for platform, prefixes in _PLATFORM_PREFIXES:
        if any(store_name.startswith(p) for p in prefixes):
            return platform
    return ""


# --------------------------------------------------------------------------- #
# 模型容器
# --------------------------------------------------------------------------- #


class Model(Base):
    """一家公司的完整建模数据。"""

    id: str
    name: str
    version: str = "1"
    #: 统一记账货币。
    currency: str = "CNY"
    stores: tuple[Store, ...] = ()
    sources: tuple[SourceContract, ...] = ()
    templates: tuple[Template, ...] = ()
    metrics: tuple[Metric, ...] = ()
    statement: tuple[StatementNode, ...] = ()
    dictionary: tuple[DictionaryEntry, ...] = ()
    checks: tuple[Check, ...] = ()

    # -- 索引 ------------------------------------------------------------- #

    def store(self, sid: str) -> Store:
        return _pick(self.stores, sid, "店铺")

    def store_of(self, filename: str) -> Store | None:
        """这个文件属于哪家店。认不出返回 None，由调用方决定怎么提示。

        多家店同时匹配时取匹配串最长的那个：店名有长有短（「喜必顺」和
        「淘宝喜必顺」），短的会误伤长的，最长匹配才是最具体的那家。
        """
        best: Store | None = None
        best_len = 0
        for s in self.stores:
            for alias in (s.name, *s.aliases):
                if alias and alias in filename and len(alias) > best_len:
                    best, best_len = s, len(alias)
        return best

    def active_stores(self) -> tuple[Store, ...]:
        """在营的店。归档店不参与新账期，但历史账仍可重算。"""
        return tuple(s for s in self.stores if not s.archived)

    def source(self, sid: str) -> SourceContract:
        return _pick(self.sources, sid, "数据源")

    def template(self, tid: str) -> Template:
        return _pick(self.templates, tid, "模板")

    def metric(self, mid: str) -> Metric:
        return _pick(self.metrics, mid, "指标")

    def node(self, nid: str) -> StatementNode:
        return _pick(self.statement, nid, "公式树节点")

    def templates_of(self, source_id: str) -> tuple[Template, ...]:
        return tuple(t for t in self.templates if t.source == source_id)

    def metrics_of(self, source_id: str) -> tuple[Metric, ...]:
        return tuple(m for m in self.metrics if m.source == source_id)

    def lookup(self, platform: str, raw: str) -> DictionaryEntry | None:
        """科目字典查表。先按平台精确匹配，再回退到通用条目。"""
        key = normalize_header(raw)
        for entry in self.dictionary:
            if entry.platform == platform and normalize_header(entry.raw) == key:
                return entry
        for entry in self.dictionary:
            if entry.platform == "*" and normalize_header(entry.raw) == key:
                return entry
        return None

    def roots(self) -> tuple[StatementNode, ...]:
        """公式树的顶层节点，按声明顺序。"""
        referenced = {c for n in self.statement for c in n.children}
        referenced |= {o for n in self.statement if n.formula for o in n.formula.of}
        return tuple(n for n in self.statement if n.id not in referenced)

    # -- 完整性校验 -------------------------------------------------------- #

    @model_validator(mode="after")
    def _integrity(self) -> Model:
        errors: list[str] = []
        source_ids = {s.id for s in self.sources}
        metric_ids = {m.id for m in self.metrics}
        node_ids = {n.id for n in self.statement}

        for dup, label in ((self.sources, "数据源"), (self.metrics, "指标"), (self.statement, "节点")):
            seen: set[str] = set()
            for obj in dup:
                if obj.id in seen:
                    errors.append(f"{label} id 重复：{obj.id}")
                seen.add(obj.id)

        for t in self.templates:
            if t.source not in source_ids:
                errors.append(f"模板 {t.id} 指向不存在的数据源 {t.source}")

        # 口径项有两个来源：科目字典，以及模板上的归类规则链。实测「物流运费」
        # 就只由规则链产生（备注含"商家集运物流责任货值赔付"），字典里没有。
        majors = {e.major for e in self.dictionary}
        majors |= {
            r.major for t in self.templates for r in t.classify_rules if r.major
        }
        for m in self.metrics:
            if m.source not in source_ids:
                errors.append(f"指标 {m.id} 指向不存在的数据源 {m.source}")
            templates = self.templates_of(m.source)
            roles = {b.role for t in templates for b in t.bindings}
            if roles:
                for role in (*m.value.of, *(p.field for p in m.where)):
                    if role not in roles:
                        errors.append(f"指标 {m.id} 引用了数据源 {m.source} 没有的字段角色 {role}")
                has_chain = any(t.key_rules for t in templates)
                if m.link and m.link.key and not has_chain and m.link.key not in roles:
                    errors.append(f"指标 {m.id} 的关联键 {m.link.key} 不在数据源 {m.source} 的角色里")
            if m.major and majors and m.major not in majors:
                errors.append(
                    f"指标 {m.id} 要求口径项 {m.major}，但没有任何科目会归到它——"
                    f"科目字典里没有，模板的归类规则链里也没有。"
                    f"现有的口径项：{'、'.join(sorted(majors))}"
                )

        for s in self.sources:
            for mid in s.provides:
                if mid not in metric_ids:
                    errors.append(f"数据源 {s.id} 声明供给不存在的指标 {mid}")

        for n in self.statement:
            refs = n.children if n.children else (n.formula.of if n.formula else ())
            for r in refs:
                if r not in metric_ids and r not in node_ids:
                    errors.append(f"节点 {n.id} 引用了既非指标也非节点的 {r}")

        for c in self.checks:
            if c.kind == "link_rate" and c.metric and c.metric not in metric_ids:
                errors.append(f"校验 {c.id} 指向不存在的指标 {c.metric}")

        if cycle := _find_cycle(self):
            errors.append("公式树存在环：" + " → ".join(cycle))

        if errors:
            raise ValueError("模型校验失败：\n  - " + "\n  - ".join(errors))
        return self


def _pick(items: tuple, key: str, label: str):
    for it in items:
        if it.id == key:
            return it
    raise KeyError(f"{label}不存在：{key}")


def _find_cycle(model: Model) -> list[str] | None:
    node_ids = {n.id for n in model.statement}
    state: dict[str, int] = {}
    path: list[str] = []

    def walk(nid: str) -> list[str] | None:
        if state.get(nid) == 2:
            return None
        if state.get(nid) == 1:
            return [*path[path.index(nid):], nid]
        state[nid] = 1
        path.append(nid)
        node = model.node(nid)
        refs = node.children if node.children else (node.formula.of if node.formula else ())
        for r in refs:
            if r in node_ids and (found := walk(r)):
                return found
        path.pop()
        state[nid] = 2
        return None

    for n in model.statement:
        if found := walk(n.id):
            return found
    return None


Amount = Annotated[float, Field(description="有符号金额，单位为模型声明的货币")]
