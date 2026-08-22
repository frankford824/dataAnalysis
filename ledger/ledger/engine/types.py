"""引擎的内部表示。

这里没有任何公司知识，也没有任何科目名。只有"文件、行、角色、金额、层级"这类
与行业无关的概念。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..money import sum_amounts

#: 证据锚点列名。跟着数据一路传到最终结果，保证任何数字都能点回原始文件行号。
ANCHOR_SHA = "__sha__"
ANCHOR_FILE = "__file__"
ANCHOR_SHEET = "__sheet__"
ANCHOR_ROW = "__row__"
ANCHORS = (ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW)


@dataclass(frozen=True, slots=True)
class FileRef:
    """一份被解析的数据的来源。sha256 让重复上传天然幂等。"""

    sha256: str
    filename: str
    sheet: str | None = None

    def label(self) -> str:
        return f"{self.filename}" + (f" · {self.sheet}" if self.sheet else "")


@dataclass(frozen=True, slots=True)
class RawRow:
    """一行原始数据。row_no 是文件内的物理行号，1 起，表头占第 1 行。"""

    row_no: int
    cells: tuple[Any, ...]


@dataclass(slots=True)
class ControlTotal:
    """文件自己声明的一条控制总数。

    解析完用它自证：笔数对不上就是漏读或多读了行，金额对不上就是数值解析有问题。
    这是免费的正确性证据，不用它才是浪费。
    """

    label: str
    #: 文件声明的笔数。取不到为 None。
    count: int | None = None
    #: 文件声明的金额。
    amount: float | None = None
    #: 只统计满足这个方向的行：income 只看正数，outgo 只看负数，both 全看。
    direction: str = "both"
    #: 原文，核对不上时给人看。
    raw: str = ""


@dataclass
class RawTable:
    """解析产物。headers 保留原始顺序与重复，重复列名靠位置区分。"""

    ref: FileRef
    headers: list[str]
    rows: list[RawRow] = field(default_factory=list)
    #: 解析过程中的观察，进入自检层。
    notes: list[str] = field(default_factory=list)
    #: 文件自带的控制总数。支付宝导出的账务明细尾部写着
    #: `#支出合计：75171笔，共-540182.61元`，这是文件自己声明的正确答案，
    #: 解析完拿它对一遍就能确定有没有漏读或多读。比赌解析库够强靠谱得多。
    controls: list[ControlTotal] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class Recognition:
    """识别结果。认不出来必须说清为什么，绝不静默丢列。"""

    ref: FileRef
    signature: str
    header_count: int
    template_id: str | None = None
    source_id: str | None = None
    #: 给用户看的人话。
    reason: str = ""
    #: 未被任何模板消费的列。用于提示模板可能过时。
    unmapped_columns: list[str] = field(default_factory=list)
    #: 候选模板及其缺失列，供 AI 在确认界面里给出映射草案。
    near_misses: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.template_id is not None


@dataclass(slots=True)
class LinkReport:
    """一次关联的命中情况。命中率与覆盖率都进入自检层。

    命中率与覆盖率衡量的是两个不同的失败模式，缺一个就有盲区：

      命中率 = 附属数据里有多少行挂上了订单。低了说明键对不上（键写错、格式不一致）。
      覆盖率 = 订单里有多少笔拿到了这项数据。低了说明数据不全（只传了半个月的成本）。

    命中率 100% 而覆盖率 50% 是完全可能的，而且是最危险的一种：钱少算了一半，
    但所有关联指标都是绿的。
    """

    metric_id: str
    key_role: str
    grain: str
    total_rows: int = 0
    linked_rows: int = 0
    #: 天然无订单号的行数。这类挂不上不算异常。
    naturally_unlinked_rows: int = 0
    #: 正则提取失败的行数。
    extract_failed_rows: int = 0
    #: 靠备用角色换算才挂上的行数。见 LinkRule.fallback_to。
    fallback_rows: int = 0
    unlinked_amount: float = 0.0
    #: 被规则链显式排除的行数。这类不算异常。
    excluded_rows: int = 0
    #: 规则链各环的命中情况。用来回答"这条规则还有没有用"。
    chain: object | None = None
    #: 覆盖率的分母：脊柱上预期有这项数据的键数。指标没声明 expect 时等于全部键数。
    spine_keys: int = 0
    #: 脊柱全部键数。和 spine_keys 不等时说明分母被 expect 收窄了，展示时要讲清楚。
    spine_keys_total: int = 0
    #: 分母收窄的依据，人话。例如「已发货」。
    expect_label: str = ""
    #: 被本指标覆盖的脊柱键。存集合而不是计数，因为同一指标会被多个文件分批供给，
    #: 计数相加会重复。
    covered_keys: set[str] = field(default_factory=set)

    @property
    def eligible(self) -> int:
        return self.total_rows - self.naturally_unlinked_rows

    @property
    def hit_rate(self) -> float:
        return self.linked_rows / self.eligible if self.eligible else 1.0

    @property
    def spine_keys_covered(self) -> int:
        return len(self.covered_keys)

    @property
    def coverage(self) -> float:
        return self.spine_keys_covered / self.spine_keys if self.spine_keys else 1.0


@dataclass(slots=True)
class ClassifyReport:
    """归类结果。未命中的科目要带上金额，好让人判断值不值得处理。"""

    total_rows: int = 0
    classified_rows: int = 0
    #: 原始科目 → 物理行锚点 → 该行净额。
    #:
    #: 按物理行存而不是直接累加，因为一张对账表会被多个指标各归类一遍：淘宝有七个
    #: 科目从同一张表出数，同一行就被数了七次。实测余利宝申购那 88 行被报成 528 行。
    unmatched_rows: dict[str, dict[tuple[str, str, str], float]] = field(default_factory=dict)
    #: 被归类规则链显式排除的行数。
    excluded_rows: int = 0
    #: 规则链各环命中情况。
    chain: object | None = None

    def note_unmatched(self, label: str, row: tuple[str, str, str], amount: float) -> None:
        """记一行未归类。同一物理行重复报只留一次。"""
        self.unmatched_rows.setdefault(label, {})[row] = amount

    def for_rows(self, anchors: set[tuple[str, str, str]]) -> ClassifyReport:
        """只留这些物理行的未归类，其余字段照抄。

        归类是按表跑的，一张表跑完就是一份报告；而账是按（店，账期）结的。
        两者不是一回事，中间必须过滤一次。不过滤的后果是：全公司共用的那几张
        表——小额打款、运费、聚水潭成本——里任何一行归不上，会同时拦住每一家店
        每一个账期结账，而且每家店看到的都是同一句话，谁都不知道那行是不是自己的。

        实测小额打款表第 1671、1672 行网店名称、下单日期、摘要三列全空，只有
        收款人和 3 元金额。它们没进任何一家店的账（算不出账期），却让八家店
        全部 can_close=false，报出来的说法还是「业务描述为空」——那张表根本
        没有业务描述这一列。

        行数与命中率不跟着过滤：它们是「这批表认识多少科目」的统计，
        本身就不按店期分。真正卡结账的是未归类，只过滤它。
        """
        scoped = {
            label: {row: amt for row, amt in rows.items() if row in anchors}
            for label, rows in self.unmatched_rows.items()
        }
        return ClassifyReport(
            total_rows=self.total_rows,
            classified_rows=self.classified_rows,
            unmatched_rows={k: v for k, v in scoped.items() if v},
            excluded_rows=self.excluded_rows,
            chain=self.chain,
        )

    @property
    def unmatched(self) -> dict[str, tuple[int, float]]:
        """原始科目 → (行数, 金额)。未命中时调 AI 提建议，人工确认后写回字典。"""
        return {k: (len(v), float(sum_amounts(v.values()))) for k, v in self.unmatched_rows.items()}

    @property
    def hit_rate(self) -> float:
        return self.classified_rows / self.total_rows if self.total_rows else 1.0


@dataclass(slots=True)
class Finding:
    """自检产出的一条结论。message 必须是人话。"""

    check_id: str
    name: str
    passed: bool
    blocking: bool
    message: str
    #: 机器可读的细节，界面用来渲染下钻按钮。
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Completeness:
    """完整度。主界面的第一公民。"""

    arrived: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: 数据源 id → 缺失原因。区分"根本没传"和"传了但没有这个店的数据"，
    #: 否则会对着已经上传的人催传，用户马上就不信这套提示了。
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def required(self) -> int:
        return len(self.arrived) + len(self.missing)

    @property
    def ok(self) -> bool:
        return not self.missing

    def label(self) -> str:
        return f"{len(self.arrived)}/{self.required}"
