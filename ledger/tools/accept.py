"""端到端验收：引擎跑一遍，和人工 Excel 表的结果对账。

判定标准很直白——人工表算出来的毛利和利润是多少，引擎就该算出多少。
对不上就要能说清楚差在哪一项、差多少、为什么差，不允许"差不多"。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import math
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine import ingest, run  # noqa: E402
from ledger.engine.normalize import to_number  # noqa: E402
from ledger.engine.parse import ParseOptions, parse  # noqa: E402
from ledger.model import load_model  # noqa: E402

DATA = Path("/home/wsfwk/data/platform")
MODEL = Path(__file__).resolve().parents[2] / "models/cn-ecommerce"

#: 人工表里这些列存的是正数但含义是费用，对账时要取反才能和引擎口径比。
#: 三家店共用同一批成本列名，只有平台侧的列各不相同。
STORED_POSITIVE_COSTS = frozenset({
    "聚水潭成本", "代发成本", "补发成本", "发货运费", "推广费用",
    "客服打款", "小额打款", "刷单/本金佣金", "物流运费", "销售支出",
})

#: 淘宝那批差异的共同原因。
#:
#: 这个结论是这么得出来的，不是猜的：
#:   1. 逐列查公式形状——聚水潭成本和销售收入两列 21,988 行公式完全一致、
#:      没有一格手填值，所以差异不来自人工调整。
#:   2. 公式里引用的是外部工作簿：聚水潭是 '[7]聚水潭订单5+6月'、对账是 [1] 的
#:      「对账」表、小额打款是 [3]。这些文件没交上来。交付的对账文件里甚至没有
#:      叫「对账」的工作表，只有支付宝、微信两张明细和两张透视汇总。
#:   3. 按交付文件原样重算，得到的正是引擎的数（聚水潭 167,956.22）。
#:   4. 逐子订单比对十个科目，所有差异只落在 156 个子订单上，占 21,988 行的 0.7%。
#:   5. 抽查两笔单笔差异——刷单 37.20（子订单 5116738248077003418）和
#:      客服打款 10.00（子订单 3300564902149006974）——这两个订单号在交付的
#:      刷单表 1,202 行和小额打款表 2,687 行里都查不到。
#:
#: 所以这批差异是数据源版本不一致，引擎按交付的数据算出的结果是对的。
#: 要真正消掉它，得让对方把公式引用的那几个工作簿一起交上来，不是改算法。
_TAOBAO_STALE = (
    "人工表的 SUMIFS 引用了没交上来的外部工作簿，和交付文件不是同一份。"
    "全部差异只落在 156 个子订单上（21,988 行的 0.7%），"
    "抽查的两笔在交付的源表里根本查不到。详见 _TAOBAO_STALE 上方的取证过程。"
)

#: 业务改口径造成的差异。和数据版本无关，是有意的，人工表还停在老口径上。
#:
#: 改了三条：保证金解冻的费项为空（不进损益，和原先一致）、保证金锁定改判交易赔付
#: （原先当纯划转排除）、商家集运物流责任货值赔付改判交易赔付（原先是物流运费）。
#:
#: 影响算得清，两条加起来正好对上：
#:   交易赔付 -1,036.86 —— 锁定那 107 行进账，集运货值赔付那 31 行搬过来且不再取反；
#:   利润     -717.45   —— 就是上面的 1,036.86，减去物流运费那一行退出带来的 319.41。
#: 各自叠上原有的数据版本差异（交易赔付 +39.11、利润 +339.81），就是登记的这两个数。
#:
#: 人工表里还有一列物流运费 -363.79，引擎不再出这一项：费项分类对照表五个平台
#: 加起来没有「物流运费」这个业务大类，唯一撑着它的费项已经改判走了。
_TAOBAO_RECLASSIFIED = (
    "业务在 2026-08 改了保证金锁定和集运货值赔付的费项归属，人工表还是老口径。"
    "详见 _TAOBAO_RECLASSIFIED 上方的逐笔拆解。"
)

#: 抖音换挂钩键造成的差异。业务在 2026-08 补的规则把抖音对账从「一条净额挂子订单」
#: 改成「拆五个费项、挂主订单号、除以 countifs」，这里比的那张人工表还是老口径。
#:
#: 差额全部来自同一个主订单 6953149768301877000 上的两笔货款结算入账，各 20.87：
#: 一笔的子订单号是空的，另一笔写的子订单号（…F00）在订单明细里没有，
#: 而这个主订单本身在脊柱上（有 4 个子订单）。按子订单号挂就漏掉这 41.74，
#: 按主订单号挂能认领——这正是业务改键的原因。
#:
#: 销售收入 +43.30 比利润 +41.74 多出的 1.56，是拆项之后销售退款单独成行，
#: 不再抵在收入里。
#:
#: 为什么不拿他们新给的那份表当验收目标：那份表里每一项都正好是原始行的两倍。
#: 新对账文件右侧那个透视表自己就是两倍——交易收款 12,500.74 对原始行 6,250.37，
#: 退款 -9.08 对 -4.54，服务费 -13.60 对 -6.80，货款直投千川 -5,280.18 对 -2,640.09，
#: 一项不差。订单明细的 SUMIFS 取的是这个透视表，所以整表跟着翻倍。
#: 原始导出行是对的，翻倍出在透视表的源区域上（京东、拼多多两份新表同样如此）。
_DOUYIN_REKEYED = (
    "业务在 2026-08 把抖音对账改成拆费项、挂主订单号，人工表还是老口径的净额挂子订单。"
    "差额是同一个主订单上两笔各 20.87 的收款，按子订单挂会漏。"
    "详见 _DOUYIN_REKEYED 上方的取证过程。"
)

#: 三家店的成本侧列名一致，抽出来复用。
_COST_COLUMNS = {
    "聚水潭成本": "n_goods",
    "代发成本": "n_dropship",
    "补发成本": "n_reshipment",
    "发货运费": "n_freight",
    "推广费用": "n_ad",
    "刷单/本金佣金": "n_brushing",
    "毛利": "gross_profit",
    "利润": "net_profit",
}


@dataclass(frozen=True)
class Case:
    """一家店的验收口径。

    每家店的人工表列名和利润公式都不一样，这不是能统一掉的差异——
    三份订单明细表第一行各自写着自己的 SUMIFS 公式。验收要照着各自的口径对，
    对得上才说明引擎复现了现行账，不是凑出一个平均值。
    """

    store: str
    platform: str
    detail: str
    sheet: str
    key: str
    #: 人工表列名 → 损益节点。
    columns: dict[str, str]
    #: 已查明原因的差异：列名 → (引擎减人工的差额, 原因)。
    #:
    #: 不是所有差异都该被消掉。有的是引擎比人工更完整，有的是人工表链接的外部
    #: 工作簿和交上来的文件不是同一份。这些差异登记下来、写清原因，验收就能把
    #: 「已经查清楚的老差异」和「这次新冒出来的差异」分开——不登记的话，每次跑
    #: 都是一片「差异大」，真正的回归会被淹掉。
    #: 登记值和实测不符也要报出来：那说明原因变了，得重新查。
    explained: dict[str, tuple[float, str]] = field(default_factory=dict)


CASES = {
    "淘宝喜必顺": Case(
        store="淘宝喜必顺", platform="taobao",
        detail="订单明细/订单明细-淘宝喜必顺.xlsx", sheet="5月", key="子订单编号",
        columns={
            "销售收入": "n_receipt", "销售退款": "n_refund",
            "软件服务费": "n_software", "物流运费": "n_logistics",
            "交易赔付": "n_compensation", "营销费用": "n_marketing",
            "客服打款": "n_small_payment", **_COST_COLUMNS,
        },
        explained={
            "软件服务费": (52.38, _TAOBAO_STALE),
            "营销费用": (27.66, _TAOBAO_STALE),
            "销售退款": (-12.91, _TAOBAO_STALE),
            "发货运费": (27.41, _TAOBAO_STALE),
            "客服打款": (10.00, _TAOBAO_STALE),
            "刷单/本金佣金": (37.20, _TAOBAO_STALE),
            "毛利": (114.58, _TAOBAO_STALE),
            "交易赔付": (-997.75, _TAOBAO_RECLASSIFIED),
            "利润": (-377.64, _TAOBAO_RECLASSIFIED),
            "聚水潭成本": (675.85,
                      "人工表的公式引用外部工作簿 '[7]聚水潭订单5+6月'，和交上来的"
                      "「聚水潭成本-淘宝喜必顺.xlsx」不是同一份。逐子订单比对：21,988 行里"
                      "有 72 行金额不一致，差额合计正好 675.85，其中若干子订单在交付文件里"
                      "根本查不到。公式形状全表一致、没有手填值，按交付文件原样重算得到的"
                      "正是引擎的 167,956.22。"),
            "销售收入": (-561.28,
                     "人工表的公式引用外部工作簿 [1] 里一张叫「对账」的表，交上来的"
                     "「对账-淘宝喜必顺.xlsx」里没有这张表（只有支付宝、微信两张明细和"
                     "两张透视汇总）。逐子订单比对：60 个子订单不一致，差额合计正好 561.28。"
                     "抽查主订单 3300490887757001161：分配率 0.7939、汇总表交易收款 89.89，"
                     "引擎算 71.36；人工缓存值 90.51 反推需要交易收款 114.01，"
                     "比交付文件里的多 24。是数据源版本差异，不是分摊算法差异。"),
        },
    ),
    "1688星泽气球派对": Case(
        store="1688星泽气球派对", platform="alibaba1688",
        detail="订单明细/订单明细-1688星泽气球派对.xlsx", sheet="Sheet1", key="订单号",
        columns={
            # 含 14 行订单售后退款，金额为负，引擎也归在同一个科目上。
            "销售收入": "n_receipt",
            "销售支出": "n_expense",
            "小额打款": "n_small_payment",
            **_COST_COLUMNS,
        },
        explained={
            "发货运费": (-72.48,
                     "引擎比人工多认出 32 个运单。订单明细的运单号列有 429 行是空的，"
                     "人工按运单号做 SUMIFS 就漏了这些；引擎会拿运单号去聚水潭查快递单号，"
                     "查到它们属于本店订单。这 72.48 元本该进成本，引擎的算法更完整。"),
            "小额打款": (3.00,
                     "人工表的公式引用的是外部工作簿 [3]客服打款，和交上来的"
                     "「小额打款-1688星泽气球派对.xlsx」不是同一份：人工那笔 3.00 挂在订单"
                     "5118532524280005716 上，交上来的文件里这笔的订单号是 ...001243，"
                     "是另一个订单。数据源版本不一致，不是算法差异。"),
            "利润": (-69.48, "上面两项之和：发货运费多认 -72.48、小额打款少认 3.00。"),
        },
    ),
    "抖音浅花涧节日装饰": Case(
        store="抖音浅花涧节日装饰", platform="douyin",
        detail="订单明细/订单详情-抖音浅花涧节日装饰.xlsx", sheet="订单明细", key="子订单编号",
        columns={
            "销售收入": "n_receipt",
            "小额打款": "n_small_payment",
            **_COST_COLUMNS,
        },
        explained={
            "销售收入": (43.30, _DOUYIN_REKEYED),
            "毛利": (43.30, _DOUYIN_REKEYED),
            "利润": (41.74, _DOUYIN_REKEYED),
        },
    ),
}


def human_totals(path: Path, sheet: str, key_column: str, wanted: dict[str, str]) -> dict[str, float]:
    """读人工表各列合计，去掉表底合计行。

    走引擎的解析而不是直接开 openpyxl：这些表里的订单号常常是合并单元格，
    引擎读的时候会按合并区域把值还原到每一行，直接读则一半的行订单号是空的，
    会被下面的空行判断丢掉，合计只剩一半。对账的两边得用同一套读法才比得出真差异。
    """
    tables = [t for t in parse(str(path), ParseOptions(header_row=1)) if t.ref.sheet == sheet]
    if not tables:
        raise SystemExit(f"{path.name} 里没有工作表 {sheet}")
    table = tables[0]
    key_idx = table.headers.index(key_column) if key_column in table.headers else 0
    sums: dict[str, float] = {}
    for row in table.rows:
        key = str(row.cells[key_idx]).strip() if key_idx < len(row.cells) else ""
        if not key or key in ("合计", "总计"):
            continue
        for j, name in enumerate(table.headers):
            if name not in wanted or j >= len(row.cells):
                continue
            n = to_number(row.cells[j])
            if n is not None:
                sums[name] = sums.get(name, 0.0) + n
    return sums


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None, choices=list(CASES), help="缺省逐个验收全部门店")
    args = ap.parse_args()

    cases = [CASES[args.store]] if args.store else list(CASES.values())
    gaps = {}
    for case in cases:
        gaps[case.store] = run_case(case)

    if len(cases) > 1:
        print("\n" + "=" * 96 + "\n全店汇总\n" + "=" * 96)
        for store, gap in gaps.items():
            case = CASES[store]
            # 毛利和利润是分项加总出来的，算进去会把同一笔差异数两遍。
            derived = {n for n, nid in case.columns.items() if nid in ("gross_profit", "net_profit")}
            items = {n: a for n, (a, _) in case.explained.items() if n not in derived}
            known = math.fsum(abs(a) for a in items.values())
            tail = f"，另有已解释差异 {known:,.2f}（{len(items)} 项）" if items else ""
            print(f"  {store:<22} 未解释 {gap:>10,.2f}{tail}")
    return 0


def run_case(case: Case) -> float:
    print("\n" + "#" * 96)
    print(f"# {case.store}（{case.platform}）")
    print("#" * 96)

    files = sorted(p for p in DATA.rglob("*.xlsx") if case.store in p.name)
    print(f"输入 {len(files)} 个文件：")
    for p in files:
        print(f"   {p.parent.name}/{p.name}  {p.stat().st_size / 1024:,.0f} KB")

    model = load_model(MODEL)
    print(f"\n模型 {model.name}：模板 {len(model.templates)}、指标 {len(model.metrics)}\n")

    print("=" * 96 + "\n识别与解析\n" + "=" * 96)
    ing = ingest([str(p) for p in files], model, known_stores=[case.store])
    print(ing.summary())
    for item in ing.unknown:
        print(f"  认不出：{item.ref.label()}  {item.error or item.recognition.reason}")
    for item in ing.known:
        rows = item.frame.height if item.frame is not None else 0
        print(f"  {item.template.name:<26} {rows:>8,} 行   {item.ref.label()[:46]}")
        for n in item.notes:
            print(f"      note: {n}")

    print("\n" + "=" * 96 + "\n核算\n" + "=" * 96)
    result = run(ing, platform=case.platform)
    print(f"脊柱 {result.spine_rows:,} 行，源事实 {result.facts.height:,} 条，"
          f"脊柱事实 {result.spine_facts.height:,} 条")
    for n in result.notes[:25]:
        print(f"  {n}")

    print("\n投影情况：")
    for mid, proj in result.projections.items():
        metric = next(m for m in model.metrics if m.id == mid)
        cover = 1 - proj.uncovered_rows / result.spine_rows if result.spine_rows else 0
        flag = "" if proj.orphan_keys == 0 else f"   孤儿 {proj.orphan_keys:,} 键 / {proj.orphan_amount:,.2f} 元"
        print(f"  {metric.name:<12} 覆盖 {cover:>6.1%}{flag}")

    if not result.slices:
        print("\n没有产出任何店铺期间切片，无法对账")
        return float("nan")

    for (store, period), sl in sorted(result.slices.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        print("\n" + "=" * 96)
        print(f"{store} · {period}")
        print("=" * 96)
        for node in model.statement:
            if node.id not in sl.nodes:
                continue
            nv = sl.nodes[node.id]
            v = nv.value if nv.available else None
            indent = "  " * (node.level - 1)
            shown = (
                f"（不出数：缺 {'、'.join(nv.missing_sources)}）" if v is None
                else (f"{v:>16,.2f}" if node.display != "percent" else f"{v:>15.2%}")
            )
            print(f"  {indent}{node.name:<16} {shown}")
        print(f"\n  自检：{'可结账' if sl.can_close else '不可结账'}")
        for f in sl.audit.findings:
            print(f"    [{'通过' if f.passed else ('拦截' if f.blocking else '提示')}] {f.name}：{f.message.strip()}")

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 96 + "\n与人工 Excel 表逐项对账\n" + "=" * 96)
    human = human_totals(DATA / case.detail, case.sheet, case.key, case.columns)
    target = next(iter(sorted(result.slices.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or ""))))[1]

    print(f"  {'项目':<16}{'人工表':>16}{'引擎':>16}{'差':>14}   ")
    print("  " + "-" * 66)
    total_gap = 0.0
    for name, node_id in case.columns.items():
        if name not in human:
            continue
        h = human[name]
        if name in STORED_POSITIVE_COSTS:
            h = -h
        nv = target.nodes.get(node_id)
        if nv is None:
            # 人工表有这一列、模型里没有这一行。和「缺数据源」是两回事，
            # 前者是口径决定，后者是数据没交齐，混在一句话里会让人去找不存在的表。
            print(f"  {name:<16}{h:>16,.2f}{'本模型不出这一项':>16}")
            continue
        if not nv.available:
            print(f"  {name:<16}{h:>16,.2f}{'未出数（缺数据源）':>16}")
            continue
        e = nv.value
        gap = e - h
        known = case.explained.get(name)
        if known is not None and abs(gap - known[0]) < 0.02:
            mark = "  ← 已解释"
        elif known is not None:
            mark = f"  ← 登记的是 {known[0]:,.2f}，原因可能变了"
            total_gap += abs(gap)
        else:
            mark = "" if abs(gap) < 0.02 else ("  ←" if abs(gap) < 1 else "  ← 差异大")
            if node_id not in ("gross_profit", "net_profit"):
                total_gap += abs(gap)
        print(f"  {name:<16}{h:>16,.2f}{e:>16,.2f}{gap:>14,.2f}{mark}")
    print("  " + "-" * 66)
    print(f"  未解释的分项绝对差合计 {total_gap:,.2f}")
    # 同一个原因往往一次影响好几个科目，按原因归组，说明只写一遍。
    groups: dict[str, list[str]] = {}
    for name, (amount, why) in case.explained.items():
        groups.setdefault(why, []).append(f"{name} {amount:+,.2f}")
    for why, items in groups.items():
        print(f"\n  已解释：{'、'.join(items)}")
        print(f"    {why}")
    return total_gap


if __name__ == "__main__":
    raise SystemExit(main())
