"""缺口清单：这家店这个账期有什么不对。

一份快照里本来有五处在讲「哪里不对」，分散在五个地方的后果是没有一处能回答
「这张损益表我该不该信」。这里盯的是收成一份清单之后最容易失守的几点：

    该报的报了（尤其是那条只有跨账期才看得出来的「上个月还有、这个月成了 0」）；
    不该报的不报（一分两分的尾差、偶发科目的覆盖率、公司级主表的命中率）；
    每条都有落点，点得进去。

「不该报的不报」和「该报的报了」一样重要。清单一长，人就整份不看，那比不报更糟。
"""

from __future__ import annotations

import pytest
from conftest import MODELS

from ledger import gaps
from ledger.model.loader import load_model


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


def _payload(**kw) -> dict:
    base = {
        "statement": [], "findings": [], "sources": [], "missing_sources": [],
        "quality": [], "unclassified": [], "unlinked_total": 0.0, "unlinked_buckets": [],
    }
    return base | kw


def _line(id: str, name: str, value: float, **kw) -> dict:
    return {
        "id": id, "name": name, "value": value, "available": True,
        "display": "amount", "is_total": False, "missing_sources": [],
    } | kw


def _kinds(rows: list[dict]) -> list[str]:
    return [r["kind"] for r in rows]


class TestAnEmptyLineIsNotAZero:
    """出不来数和算出来是 0 必须分开报，否则人不知道该补表还是该放心。"""

    def test_a_line_with_no_data_source_is_reported_as_empty(self, model):
        rows = gaps.gaps(_payload(statement=[
            _line("n_ad", "推广费用", 0.0, available=False, missing_sources=["推广"]),
        ]), model)
        assert _kinds(rows) == ["empty"]
        assert "推广" in rows[0]["detail"]

    def test_a_real_zero_with_no_rows_behind_it_is_not_reported(self, model):
        """表没交、也没有行，这一项是 0 就是 0，没什么可看的。"""
        rows = gaps.gaps(_payload(statement=[_line("n_ad", "推广费用", 0.0)]), model)
        assert rows == []

    def test_a_zero_with_rows_behind_it_is_reported(self, model):
        """表交了、行读进来了、钱一分没进这一项——十次里九次是绑错了列。"""
        rows = gaps.gaps(_payload(
            statement=[_line("n_goods", "商品成本", 0.0)],
            quality=[{"metric": "goods_cost", "name": "商品成本", "rows": 3000,
                      "hit_rate": 0.9, "coverage": None}],
        ), model)
        assert _kinds(rows) == ["zero"]
        assert "3,000 行" in rows[0]["detail"]
        assert rows[0]["node"] == "n_goods"

    def test_an_occasional_item_at_zero_is_left_alone(self, model):
        """交易赔付这个月一笔都没有是常态。

        质量报告给的行数是整张对账表的行数，不是赔付自己那几行，所以这里拿它报警
        会让每家店每个月都多出五六条「这一项是 0.00」，而每一条都是废话。
        """
        rows = gaps.gaps(_payload(
            statement=[_line("n_compensation", "交易赔付", 0.0)],
            quality=[{"metric": "trade_compensation", "name": "交易赔付", "rows": 1782,
                      "hit_rate": 1.0, "coverage": None}],
        ), model)
        assert rows == []

    def test_totals_and_ratios_are_never_reported_as_zero(self, model):
        """合计行和利润率不报。合计是 0 是它底下几项相抵的结果，不是缺数据。"""
        rows = gaps.gaps(_payload(
            statement=[
                _line("net_profit", "利润", 0.0, is_total=True),
                _line("net_margin", "利润率", 0.0, display="percent"),
            ],
            quality=[{"metric": "ad_cost", "name": "推广费用", "rows": 10,
                      "hit_rate": 1.0, "coverage": None}],
        ), model)
        assert rows == []


class TestTheLineThatQuietlyBecameZero:
    """整份清单里最值钱的一条，也是唯一一条要跨账期才看得出来的。"""

    def test_money_last_month_and_nothing_this_month_is_reported(self, model):
        before = _payload(statement=[_line("n_ad", "推广费用", -88091.88)])
        now = _payload(statement=[_line("n_ad", "推广费用", 0.0)])
        rows = gaps.gaps(now, model, before)
        assert _kinds(rows) == ["dropped"]
        assert "88,091.88" in rows[0]["detail"]

    def test_a_small_amount_last_month_is_not_worth_reporting(self, model):
        """几十块的科目本来就时有时无，报出来只会淹掉真问题。"""
        before = _payload(statement=[_line("n_ad", "推广费用", -12.5)])
        now = _payload(statement=[_line("n_ad", "推广费用", 0.0)])
        assert gaps.gaps(now, model, before) == []

    def test_a_drop_in_amount_is_not_reported(self, model):
        """从十万掉到三万是经营波动，天天都在发生。只报「有变成没有」。"""
        before = _payload(statement=[_line("n_ad", "推广费用", -100000.0)])
        now = _payload(statement=[_line("n_ad", "推广费用", -30000.0)])
        assert gaps.gaps(now, model, before) == []

    def test_without_a_previous_period_the_check_stays_quiet(self, model):
        now = _payload(statement=[_line("n_ad", "推广费用", 0.0)])
        assert gaps.gaps(now, model, None) == []


class TestWhatCountsAsWorthShowing:
    def test_a_couple_of_cents_unlinked_is_not_an_issue(self, model):
        assert gaps.gaps(_payload(unlinked_total=0.02), model) == []

    def test_real_unlinked_money_is_an_issue(self, model):
        rows = gaps.gaps(_payload(
            unlinked_total=308.31,
            unlinked_buckets=[{"label": "取不出订单号，要查归属", "count": 5, "amount": 308.31}],
        ), model)
        assert _kinds(rows) == ["unlinked"]
        assert "5 行" in rows[0]["detail"]
        assert rows[0]["node"] == "__unlinked__"

    def test_a_negative_unlinked_total_reads_as_an_amount_not_as_minus(self, model):
        """未归属总额是净额，可能是负的。「有 -357.06 挂不上订单」要在脑子里绕一圈。"""
        rows = gaps.gaps(_payload(
            unlinked_total=-357.06,
            unlinked_buckets=[{"label": "取不出订单号，要查归属", "count": 3, "amount": -357.06}],
        ), model)
        assert "357.06" in rows[0]["title"] and "-357.06" not in rows[0]["title"]
        assert rows[0]["amount"] == -357.06

    def test_parked_topup_is_not_an_unlinked_gap(self, model):
        """广告充值这类已经有解释的钱，不该再报成「要人查清归属」。

        真事：抖音浅花涧 2026-05 未归属总额 -357.06，全是 664 笔货款直投千川。
        卡片写「有 357.06 挂不上订单」，点进去却一行都没有——「要查」那一桶是空的，
        点开走的是那一桶。这些行在「没进利润的钱」里，标着货款直投千川。
        """
        rows = gaps.gaps(_payload(
            unlinked_total=-357.06,
            unlinked_buckets=[
                {"label": "其他店的数据（公司级主表）", "count": 299488, "amount": -552699.37},
                {"label": "货款直投千川", "count": 664, "amount": -357.06},
                {"label": "其他账期的订单", "count": 5, "amount": 125.83},
            ],
        ), model)
        assert "unlinked" not in _kinds(rows)

    def test_coverage_at_full_is_not_an_issue(self, model):
        rows = gaps.gaps(_payload(quality=[
            {"metric": "goods_cost", "name": "商品成本", "rows": 100, "hit_rate": 1.0,
             "coverage": 1.0, "covered": 100, "expected": 100, "expect_label": ""},
        ]), model)
        assert rows == []

    def test_occasional_items_have_no_coverage_and_are_left_alone(self, model):
        """偶发科目的覆盖率是 None，不能当成 0 来报——一列常年通红等于没有这一列。"""
        rows = gaps.gaps(_payload(quality=[
            {"metric": "trade_compensation", "name": "交易赔付", "rows": 12,
             "hit_rate": 1.0, "coverage": None},
        ]), model)
        assert rows == []

    def test_a_table_that_matched_nothing_is_called_out_separately(self, model):
        """命中率 0 和覆盖率 90% 不是一个量级：前者是整张表白读。"""
        rows = gaps.gaps(_payload(quality=[
            {"metric": "freight_cost", "name": "发货运费", "rows": 299567,
             "hit_rate": 0.0, "coverage": None},
        ]), model)
        assert _kinds(rows) == ["unmatched"]
        assert "299,567 行" in rows[0]["detail"]

    def test_a_shared_table_with_no_hit_rate_is_left_alone(self, model):
        """公司级主表不评命中率，view 那边给的是 None，这里不能当成 0。"""
        rows = gaps.gaps(_payload(quality=[
            {"metric": "brushing_cost", "name": "本金佣金", "rows": 1202,
             "hit_rate": None, "coverage": None},
        ]), model)
        assert rows == []


class TestTheOrderPeopleReadThemIn:
    def test_blocking_findings_come_first(self, model):
        rows = gaps.gaps(_payload(
            findings=[{"id": "c1", "name": "控制数不符", "message": "差 1,234.00",
                       "blocking": True, "passed": False}],
            unlinked_total=99999.0,
            unlinked_buckets=[{"label": "取不出订单号，要查归属", "count": 3, "amount": 99999.0}],
        ), model)
        assert rows[0]["kind"] == "blocking", "结不了账的原因必须排在金额最大的那条前面"

    def test_within_a_severity_the_bigger_money_comes_first(self, model):
        rows = gaps.gaps(_payload(unclassified=[
            {"label": "小的", "count": 1, "amount": -20.0},
            {"label": "大的", "count": 9, "amount": -5000.0},
        ]), model)
        assert [r["amount"] for r in rows] == [-5000.0, -20.0]

    def test_a_passing_finding_is_not_an_issue(self, model):
        rows = gaps.gaps(_payload(findings=[
            {"id": "c1", "name": "控制数", "message": "对上了", "blocking": True, "passed": True},
        ]), model)
        assert rows == []


class TestTheSummaryTheOverviewShows:
    """总览一格摆不下清单，摆得下「这里有 3 处要看」。"""

    def test_empty_and_odd_are_counted_apart(self, model):
        rows = gaps.gaps(_payload(
            statement=[_line("n_ad", "推广费用", 0.0, available=False)],
            missing_sources=["推广"],
            unclassified=[{"label": "认不出的", "count": 2, "amount": -500.0}],
        ), model)
        s = gaps.summary(rows)
        assert s["empty"] == 2, "缺表和出不来数都是空值项"
        assert s["odd"] == 1
        assert s["count"] == 3

    def test_the_worst_severity_is_what_the_badge_shows(self, model):
        rows = gaps.gaps(_payload(
            findings=[{"id": "c", "name": "拦住了", "message": "", "blocking": True,
                       "passed": False}],
            unclassified=[{"label": "认不出的", "count": 2, "amount": -500.0}],
        ), model)
        assert gaps.summary(rows)["worst"] == "blocking"

    def test_nothing_wrong_gives_an_empty_worst(self, model):
        assert gaps.summary([]) == {"count": 0, "empty": 0, "odd": 0, "worst": ""}


class TestARequiredTableMissingBlocksTheMonth:
    def test_a_table_needed_for_close_is_blocking(self, model):
        required = next(s for s in model.sources if s.required_for_close)
        rows = gaps.gaps(_payload(missing_sources=[required.name]), model)
        assert rows[0]["severity"] == "blocking"
        assert rows[0]["source"] == required.name

    def test_an_optional_table_is_only_a_note(self, model):
        optional = next(s for s in model.sources if not s.required_for_close)
        rows = gaps.gaps(_payload(missing_sources=[optional.name]), model)
        assert rows[0]["severity"] == "info"


class TestEveryIssueHasSomewhereToGo:
    """点不进去的提示等于让人自己去别处找。落点是这份清单存在的理由之一。"""

    def test_line_level_issues_carry_the_statement_node(self, model):
        rows = gaps.gaps(_payload(
            statement=[_line("n_ad", "推广费用", 0.0, available=False)],
            quality=[{"metric": "goods_cost", "name": "商品成本", "rows": 10,
                      "hit_rate": 1.0, "coverage": 0.5, "covered": 5, "expected": 10,
                      "expect_label": "已发货"}],
        ), model)
        by = {r["kind"]: r for r in rows}
        assert by["empty"]["node"] == "n_ad"
        assert by["coverage"]["metric"] == "goods_cost"
        assert by["coverage"]["node"] == "n_goods"
