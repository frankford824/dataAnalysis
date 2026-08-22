"""分摊的数学。钱分下去，合计不能变。

平台的费用大多按主订单收，账要算到子订单，中间这一步就是分摊。淘宝按销售收入
占比分（分配率是平台给的），1688 按子订单笔数均分。分摊有两条硬要求：

  一、分下去的合计等于分之前的金额。差一分钱就是账不平。
  二、源表里有金额但脊柱上挂不上的，必须报出来，绝不静默丢弃。

第二条比第一条容易犯。挂不上的钱悄悄消失，损益表照样算得出来，看着还挺正常。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.link import Spine
from ledger.engine.project import project
from ledger.model.schema import Allocation, LinkRule, Metric, ValueExpr


def _metric(alloc: Allocation | None, **kw) -> Metric:
    base = dict(
        id="freight_cost",
        name="发货运费",
        source="freight",
        value=ValueExpr(op="sum", of=["amount"]),
        link=LinkRule(key="order_id", to="order.order_id", grain="order"),
        allocate=alloc,
    )
    base.update(kw)
    return Metric(**base)


def _spine(rows: list[dict]) -> Spine:
    return Spine(frame=pl.DataFrame(rows))


def _facts(pairs: list[tuple[str, float]], metric_id: str = "freight_cost") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "metric_id": [metric_id] * len(pairs),
            "link_key": [k for k, _ in pairs],
            "amount": [v for _, v in pairs],
        }
    )


class TestEvenAllocation:
    """按笔数均分。1688 的口径。"""

    def test_splits_equally_and_conserves_total(self):
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "2026-05"},
            {"order_id": "A", "store": "s", "period": "2026-05"},
            {"order_id": "A", "store": "s", "period": "2026-05"},
        ])
        proj = project(_facts([("A", 90.0)]), _metric(Allocation(mode="even")), spine)
        got = proj.facts.get_column("amount").to_list()
        assert got == [30.0, 30.0, 30.0]
        assert sum(got) == 90.0

    def test_indivisible_amount_still_conserves(self):
        """除不尽的金额也不能丢。10 元分 3 笔，合计还得是 10 元。"""
        spine = _spine([{"order_id": "A", "store": "s", "period": "p"} for _ in range(3)])
        proj = project(_facts([("A", 10.0)]), _metric(Allocation(mode="even")), spine)
        assert round(sum(proj.facts.get_column("amount").to_list()), 2) == 10.0


class TestFullyOffsetOrdersStayOnTheSpine:
    """一收一退相抵到 0 的订单，照样要留在脊柱上。

    脊柱行是「这笔钱算进了账」的凭据：`runtime._mark_counted` 就按这个标源行进没
    进账。整笔相抵的订单要是不留，标出来是「没进账」，和真正挂不上订单的钱并排
    列在同一张清单上——人分不出哪些要查，只能一单一单去核，核完发现是相抵的。

    金额上两种做法没有区别，加 0 不改变任何合计。差别全在能不能说清楚。

    实测 1688 朗歆 2026-06 有 8 单是这样，每单一行订单收入、一行订单售后退款，
    金额分毫不差（77.40 / 35.15 / 34.80 / 34.59 …）。财务逐单核对时把它们当成
    少算了的收入报了过来：单号对、费项大类对、二级科目也对，界面偏说没进账。
    """

    def test_an_offset_order_still_produces_a_spine_row(self):
        spine = _spine([{"order_id": "A", "store": "s", "period": "p"}])
        proj = project(_facts([("A", 77.4), ("A", -77.4)]),
                       _metric(Allocation(mode="even")), spine)
        assert proj.facts.height == 1, "相抵的订单不留在脊柱上，就会被标成没进账"
        assert proj.facts.get_column("amount").to_list() == [0.0]

    def test_a_spine_row_that_got_nothing_is_still_dropped(self):
        """没分到钱的脊柱行照旧丢掉。这条和上面那条长得一样，含义相反。"""
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "p"},
            {"order_id": "B", "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 50.0)]), _metric(Allocation(mode="even")), spine)
        assert proj.facts.get_column("link_key").to_list() == ["A"]

    def test_the_total_does_not_move(self):
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "p"},
            {"order_id": "B", "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 77.4), ("A", -77.4), ("B", 12.5)]),
                       _metric(Allocation(mode="even")), spine)
        assert round(sum(proj.facts.get_column("amount").to_list()), 2) == 12.5

    def test_every_sub_order_of_an_offset_order_is_kept(self):
        """一个主订单下几个子订单，相抵之后每个子订单都留一行 0。

        留一行、留几行，报表上都是 0。留全是因为下钻要能对上：这一单在脊柱上有
        几行，进账清单里就该看到几行，少了人又要问那几行去哪了。
        """
        spine = _spine([{"order_id": "A", "store": "s", "period": "p"} for _ in range(3)])
        proj = project(_facts([("A", 30.0), ("A", -30.0)]),
                       _metric(Allocation(mode="even")), spine)
        assert proj.facts.height == 3
        assert proj.facts.get_column("amount").to_list() == [0.0, 0.0, 0.0]


class TestRatioAllocation:
    """按占比分。淘宝的口径，分配率由平台给。"""

    def test_splits_by_ratio(self):
        spine = _spine([
            {"order_id": "A", "alloc_ratio": 0.7, "store": "s", "period": "p"},
            {"order_id": "A", "alloc_ratio": 0.3, "store": "s", "period": "p"},
        ])
        metric = _metric(Allocation(mode="ratio", by="alloc_ratio"))
        proj = project(_facts([("A", 100.0)]), metric, spine)
        got = proj.facts.get_column("amount").to_list()
        assert got == [70.0, 30.0]
        assert sum(got) == 100.0

    def test_null_ratio_counts_as_zero_and_is_reported(self):
        """分配率为空按 0 计，但要说出来——这部分钱没进利润。"""
        spine = _spine([
            {"order_id": "A", "alloc_ratio": 1.0, "store": "s", "period": "p"},
            {"order_id": "A", "alloc_ratio": None, "store": "s", "period": "p"},
        ])
        metric = _metric(Allocation(mode="ratio", by="alloc_ratio"))
        proj = project(_facts([("A", 100.0)]), metric, spine)
        assert any("为空" in n for n in proj.notes)

    def test_out_of_range_ratio_is_reported(self):
        """分配率大于 1 意味着子订单分到的比主订单总额还多，必须报出来。

        实测全量 205 万行里分配率取值区间是 -10.06 到 16.19，负值占 0.21%、
        大于 1 占 0.115%。不拦，但不能不说。
        """
        spine = _spine([
            {"order_id": "A", "alloc_ratio": 16.19, "store": "s", "period": "p"},
            {"order_id": "A", "alloc_ratio": -10.06, "store": "s", "period": "p"},
        ])
        metric = _metric(Allocation(mode="ratio", by="alloc_ratio"))
        proj = project(_facts([("A", 100.0)]), metric, spine)
        assert any("大于 1" in n and "为负" in n for n in proj.notes)

    def test_missing_ratio_column_falls_back_to_even_not_one(self):
        """脊柱上没有分摊率列时按笔数均摊，合计守恒。

        绝不能默默按 1：主订单有几个子订单就把钱记几遍。天猫千牛导出经常
        没有「收入分配率」这一列，缺列就崩成 500 更糟——表已经收下了，人只看到
        Internal Server Error，下次重传还会撞同一面墙。
        """
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "p"},
            {"order_id": "A", "store": "s", "period": "p"},
        ])
        metric = _metric(Allocation(mode="ratio", by="alloc_ratio"))
        proj = project(_facts([("A", 100.0)]), metric, spine)
        got = proj.facts.get_column("amount").to_list()
        assert got == [50.0, 50.0]
        assert any("笔数均摊" in n for n in proj.notes)

    def test_missing_ratio_uses_buyer_paid_when_present(self):
        """能从买家实付推占比就推，比均摊更接近淘宝原来的收入分配率。"""
        spine = _spine([
            {"order_id": "A", "buyer_paid": 70.0, "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 30.0, "store": "s", "period": "p"},
        ])
        metric = _metric(Allocation(mode="ratio", by="alloc_ratio"))
        proj = project(_facts([("A", 100.0)]), metric, spine)
        got = [round(x, 2) for x in proj.facts.get_column("amount").to_list()]
        assert got == [70.0, 30.0]
        assert any("买家实付" in n for n in proj.notes)


class TestDerivedRatioFollowsTheManualDefinition:
    """自己推分配率时，口径照财务表来：子订单收入 = 买家实付 - 退款金额。

    淘宝喜必顺那份订单明细是人工工作表原件，第一行逐列写着公式：
    子订单收入 = 买家实付金额 - 退款金额（报错取买家实付、负数计 0），
    主订单收入 = 按主订单编号汇总子订单收入，收入分配率 = 两者相除，
    各费项 = 按主订单号汇总的金额 × 收入分配率。天猫皇莉诗交上来的那份只有
    前九列，分配率得自己推，推的口径必须和这套对齐，否则同一笔平台费用
    落到哪个子订单、进而毛利和提成算给谁，两边就不一样。
    """

    def _metric(self):
        return _metric(Allocation(mode="ratio", by="alloc_ratio"))

    def test_refund_comes_off_before_taking_the_share(self):
        spine = _spine([
            {"order_id": "A", "buyer_paid": 70.0, "refund_amount": 20.0,
             "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 30.0, "refund_amount": 0.0,
             "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 80.0)]), self._metric(), spine)
        got = [round(x, 2) for x in proj.facts.get_column("amount").to_list()]
        # 净收入 50 和 30，占比 5:3
        assert got == [50.0, 30.0]
        assert round(sum(got), 2) == 80.0
        assert any("扣退款后" in n for n in proj.notes)

    def test_no_refund_filed_reads_as_zero_not_as_a_hole(self):
        """退款金额那格常填「无退款申请」，转数值后是空，得落回买家实付。

        人工公式是 IFERROR 回买家实付金额，按空值当 0 减就是同一个结果。
        """
        spine = _spine([
            {"order_id": "A", "buyer_paid": 70.0, "refund_amount": None,
             "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 30.0, "refund_amount": None,
             "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 100.0)]), self._metric(), spine)
        got = [round(x, 2) for x in proj.facts.get_column("amount").to_list()]
        assert got == [70.0, 30.0]

    def test_a_fully_refunded_sub_order_carries_nothing(self):
        """全退的子订单收入是 0，费用不该摊到它头上。

        退款比实付还多的也一样——人工公式写着「值 < 0 则计为 0」，
        不计零的话这一行会拿到负的分摊比例，把钱倒推给别的子订单。
        """
        spine = _spine([
            {"order_id": "A", "buyer_paid": 40.0, "refund_amount": 0.0,
             "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 39.6, "refund_amount": 39.6,
             "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 17.5, "refund_amount": 18.5,
             "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 60.0)]), self._metric(), spine)
        got = [round(x, 2) for x in proj.facts.get_column("amount").to_list()]
        # 分到 0 的行不落账，钱整笔留在唯一还有收入的那个子订单上
        assert got == [60.0]

    def test_an_all_refunded_order_keeps_its_fee_on_the_books(self):
        """一单全退到没有可比收入了，退回笔数均摊——钱不能丢。

        人工表这里分配率算成 0，挂在这单上的费用整块消失（实测天猫皇莉诗
        2026-06 有 3 单、5.45 元）。但佣金是真扣走了的，账上得留着，
        只是没法说清该摊给哪个子订单。
        """
        spine = _spine([
            {"order_id": "A", "buyer_paid": 39.6, "refund_amount": 39.6,
             "store": "s", "period": "p"},
            {"order_id": "A", "buyer_paid": 10.0, "refund_amount": 10.0,
             "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", -5.45)]), self._metric(), spine)
        got = proj.facts.get_column("amount").to_list()
        assert [round(x, 3) for x in got] == [-2.725, -2.725]
        assert round(sum(got), 2) == -5.45


class TestNoAllocation:
    """不分摊。抖音的结算净额本来就是一子订单一条。"""

    def test_direct_amount(self):
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "p"},
            {"order_id": "B", "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 10.0), ("B", 20.0)]), _metric(None), spine)
        assert sorted(proj.facts.get_column("amount").to_list()) == [10.0, 20.0]


class TestOrphans:
    """挂不上的钱要报出来。"""

    def test_orphan_amount_is_surfaced(self):
        """源表有 C 这笔钱，脊柱上没有 C 这个订单——这 50 元没进利润，必须说。"""
        spine = _spine([{"order_id": "A", "store": "s", "period": "p"}])
        proj = project(_facts([("A", 10.0), ("C", 50.0)]), _metric(None), spine)
        assert proj.orphan_keys == 1
        assert proj.orphan_amount == 50.0
        assert any("找不到对应订单" in n for n in proj.notes)

    def test_uncovered_spine_rows_counted(self):
        """脊柱上有订单但这个指标没覆盖到，要计入未覆盖行数——覆盖率就从这来。"""
        spine = _spine([
            {"order_id": "A", "store": "s", "period": "p"},
            {"order_id": "B", "store": "s", "period": "p"},
        ])
        proj = project(_facts([("A", 10.0)]), _metric(None), spine)
        assert proj.uncovered_rows == 1


class TestOrderlessStillEntersProfit:
    """挂不上订单、但归类规则声明了要进账的钱，必须进投影。

    淘宝联盟佣金代扣就是这种：人工对账表按费项2 SUMIFS，不要求订单在本期明细里。
    不进投影的话损益表上没有它，进账栏空着，而同一科目挂得上的那些行又在账上——
    一笔钱进不进利润，取决于它的订单号凑巧在不在这份明细里。
    """

    def test_a_flagged_missing_key_is_not_an_orphan(self):
        spine = _spine([{"order_id": "IN", "store": "s", "period": "p"}])
        facts = pl.DataFrame({
            "metric_id": ["freight_cost", "freight_cost"],
            "source_id": ["freight", "freight"],
            "store": ["s", "s"],
            "period": ["p", "p"],
            "link_key": ["IN", "OUT"],
            "amount": [10.0, -0.49],
            "count_without_order": [False, True],
        })
        proj = project(facts, _metric(None), spine)
        assert set(proj.facts.get_column("link_key").to_list()) == {"IN", "OUT"}
        assert proj.orphan_keys == 0
        out = proj.facts.filter(pl.col("link_key") == "OUT")
        assert out.get_column("amount").to_list() == [-0.49]
        assert out.get_column("factor").to_list() == [1.0]
        assert out.get_column("spine_row").to_list() == [None]

    def test_an_unflagged_missing_key_is_still_an_orphan(self):
        """没有这面旗的，行为不能变。否则所有挂不上的营销费用都会灌进利润。"""
        spine = _spine([{"order_id": "IN", "store": "s", "period": "p"}])
        facts = pl.DataFrame({
            "metric_id": ["freight_cost", "freight_cost"],
            "link_key": ["IN", "OUT"],
            "amount": [10.0, -0.49],
            "count_without_order": [False, False],
        })
        proj = project(facts, _metric(None), spine)
        assert proj.orphan_keys == 1
        assert proj.orphan_amount == -0.49
        assert "OUT" not in proj.facts.get_column("link_key").to_list()

    def test_it_does_not_open_a_period_the_spine_does_not_have(self):
        """结算日在下个月、下个月还没订单明细，不能凭空开出一个残缺账期。"""
        spine = _spine([{"order_id": "IN", "store": "s", "period": "p"}])
        facts = pl.DataFrame({
            "metric_id": ["freight_cost", "freight_cost"],
            "source_id": ["freight", "freight"],
            "store": ["s", "s"],
            "period": ["p", "other"],
            "link_key": ["IN", "JULY"],
            "amount": [10.0, -0.49],
            "count_without_order": [False, True],
        })
        proj = project(facts, _metric(None), spine)
        assert "JULY" not in proj.facts.get_column("link_key").to_list()
        assert proj.orphan_keys == 1
        assert proj.orphan_amount == -0.49

    def test_the_period_comes_from_the_first_physical_row(self):
        """同一个订单号跨了两个账期时，跟源文件里最早出现的那一行。

        取哪一行决定这笔钱落哪个月，所以不能让它随机。polars 的 group_by 默认
        不保证组内顺序，多线程下 first() 取到的是哪一行不定——同一份表跑两遍
        给出两个利润，那两个数就都不可信。实测京东皇莉诗 2026-05 的负基数合计
        在 -5,777.56 和 -5,779.26 之间跳，差的正是一笔 1.70 元有时进本期、
        有时落到别的月。
        """
        spine = _spine([
            {"order_id": "IN", "store": "s", "period": "p"},
            {"order_id": "IN2", "store": "s", "period": "q"},
        ])
        # 同一个 OUT 在源表里三行，账期先 q 后 p。行序倒着放也要拿到 q。
        facts = pl.DataFrame({
            "metric_id": ["freight_cost"] * 4,
            "source_id": ["freight"] * 4,
            "store": ["s"] * 4,
            "period": ["p", "q", "p", "p"],
            "link_key": ["IN", "OUT", "OUT", "OUT"],
            "amount": [10.0, -0.49, -0.49, -0.49],
            "count_without_order": [False, True, True, True],
            "file_sha": ["sha"] * 4,
            "sheet": ["S"] * 4,
            "row_no": [1, 20, 30, 40],
        })
        out = project(facts, _metric(None), spine).facts.filter(
            pl.col("link_key") == "OUT"
        )
        assert out.get_column("period").to_list() == ["q"]

    def test_the_first_row_wins_no_matter_how_the_rows_arrive(self):
        """把同样的行倒着交进来，结果必须一模一样。"""
        spine = _spine([
            {"order_id": "IN", "store": "s", "period": "p"},
            {"order_id": "IN2", "store": "s", "period": "q"},
        ])
        base = {
            "metric_id": ["freight_cost"] * 3,
            "source_id": ["freight"] * 3,
            "store": ["s"] * 3,
            "link_key": ["OUT"] * 3,
            "amount": [-0.49] * 3,
            "count_without_order": [True] * 3,
            "file_sha": ["sha"] * 3,
            "sheet": ["S"] * 3,
        }
        forward = pl.DataFrame({**base, "period": ["q", "p", "p"],
                                "row_no": [20, 30, 40]})
        backward = pl.DataFrame({**base, "period": ["p", "p", "q"],
                                 "row_no": [40, 30, 20]})
        got = [
            project(f, _metric(None), spine).facts.get_column("period").to_list()
            for f in (forward, backward)
        ]
        assert got[0] == got[1] == ["q"]
