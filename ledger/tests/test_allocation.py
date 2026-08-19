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
