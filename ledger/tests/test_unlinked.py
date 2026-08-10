"""没进利润的钱怎么报。

这块的要求不是「算得准」，是「报得准」。挂不上订单的钱必须让人看见——不能悄悄
丢掉，也不能硬摊进利润。但报得过头一样有害：淘宝的未归属曾经虚报到 120 万，
1688 曾经虚报 54.8 万，这种量级会让人干脆不看这个提示，等于白报。

三个虚报的来源都不是数据问题，是统计口径：
一是同一物理行被多个指标各算一次，二是公司级主表里别家店的钱被算进本店，
三是把「规则已排除的非经营流水」和「其他账期的订单」也算成了要人查的钱。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.audit import (
    BUCKET_EXCLUDED_FLOW,
    BUCKET_NEEDS_WORK,
    BUCKET_OTHER_PERIOD,
    _bucket_unlinked,
)
from ledger.engine.link import EXCLUDED_KEY
from ledger.model.schema import (
    Metric,
    Model,
    SourceContract,
    ValueExpr,
)


def _model(*, company_wide: tuple[str, ...] = ()) -> Model:
    sources = tuple(
        SourceContract(
            id=sid, name=sid, owner_role="shop_owner", cadence="monthly",
            company_wide=sid in company_wide,
        )
        for sid in ("settlement", "freight")
    )
    metrics = tuple(
        Metric(
            id=mid, name=mid, source="settlement",
            value=ValueExpr(op="sum", of=["income"]), major=mid,
        )
        for mid in ("trade_receipt", "software_fee", "logistics_fee")
    ) + (
        Metric(
            id="freight_cost", name="发货运费", source="freight",
            value=ValueExpr(op="sum", of=["amount"]), major="freight_cost",
        ),
    )
    return Model(id="t", name="t", sources=sources, metrics=metrics)


def _facts(rows: list[dict]) -> pl.DataFrame:
    base = {
        "metric_id": "", "source_id": "settlement", "store": "s", "period": "p",
        "link_key": None, "linked": False, "amount": 0.0,
        "subject": None, "major": None, "minor": None,
        "file_sha": "sha", "file_name": "f.xlsx", "sheet": "Sheet1", "row_no": 0,
    }
    return pl.DataFrame([{**base, **r} for r in rows])


class TestOnePhysicalRowCountedOnce:
    """一张表被多个指标共用时，一个物理行只能算一次。

    淘宝的七项费用全从同一张对账表出数，引擎让每个指标对整表求值，所以同一行
    在源事实里出现多次——实测那 31,618 行各出现 6 次。损益表投影时按科目过滤过
    所以金额是对的，但未归属统计直接读源事实，不去重就报了 6 遍。
    """

    def test_deduplicates_across_metrics(self):
        rows = [
            {"metric_id": m, "major": "software_fee", "amount": a, "row_no": 7}
            for m, a in (("trade_receipt", -100.0), ("software_fee", -100.0),
                         ("logistics_fee", 100.0))
        ]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -100.0, "同一物理行报了不止一次"
        assert sum(c for _, c, _ in buckets) == 1

    def test_keeps_the_metric_matching_the_row_subject(self):
        """保留科目和这行相符的那个指标算出的金额，符号才对。

        同一行在不同指标下符号可能相反（取数口径不同）。这行的科目是
        software_fee，就该取 software_fee 指标算出的 -100，不是 logistics_fee
        算出的 +100。
        """
        rows = [
            {"metric_id": "logistics_fee", "major": "software_fee", "amount": 100.0, "row_no": 7},
            {"metric_id": "software_fee", "major": "software_fee", "amount": -100.0, "row_no": 7},
        ]
        _buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -100.0

    def test_different_rows_both_counted(self):
        """去重只针对同一物理行，不同行照样各算一次。"""
        rows = [
            {"metric_id": "software_fee", "major": "software_fee", "amount": -10.0, "row_no": 1},
            {"metric_id": "software_fee", "major": "software_fee", "amount": -20.0, "row_no": 2},
        ]
        _buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -30.0


class TestCompanyWideTables:
    """公司级主表里别家店的钱不算本店未归属。

    运费表交上来是全公司的：30 万条运单里只有 2,576 条属于 1688 星泽，其余挂不上。
    算进本店的话，136.71 元的真问题会被埋在 54.8 万里，没人会去看。
    """

    def test_excluded_from_total_but_still_listed(self):
        rows = [
            {"metric_id": "freight_cost", "source_id": "freight", "major": "freight_cost",
             "amount": -5000.0, "row_no": 1},
            {"metric_id": "software_fee", "major": "software_fee", "amount": -10.0, "row_no": 2},
        ]
        buckets, total = _bucket_unlinked(_facts(rows), _model(company_wide=("freight",)))
        assert total == -10.0, "公司级主表那部分不该算进本店"
        labels = {label for label, _, _ in buckets}
        assert any("其他店" in x for x in labels), "但必须仍然列出来让人看得见"

    def test_counted_when_not_declared_company_wide(self):
        """没声明公司级的表照旧全算本店——不能默认帮人排除掉。"""
        rows = [
            {"metric_id": "freight_cost", "source_id": "freight", "major": "freight_cost",
             "amount": -5000.0, "row_no": 1},
        ]
        _buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -5000.0


class TestNothingUnlinked:
    def test_all_linked_reports_nothing(self):
        rows = [{"metric_id": "software_fee", "major": "software_fee",
                 "amount": -10.0, "row_no": 1, "linked": True}]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert buckets == [] and total == 0.0


class TestBucketsByReason:
    """按「为什么挂不上」分桶，因为不同原因要的处置完全不同。

    淘宝 5 月曾经报出 -30.29 万「看起来是订单的钱」，而这个数字没有任何业务含义，
    它是三样东西加在一起的净额：

      -47.78 万   64 行，规则链显式判定为非经营流水（余利宝申购、保证金、广告预充值）
      +17.52 万   31,549 行，订单号是对的但订单不在本期（跨期结算，或导出日期选宽了）
        -308.31 元  5 行，连订单号都取不出来 —— 只有这 308 块真要人查

    把三样加起来摆在界面上，用户要么白查一场，要么学会无视这个数。两种都比不报更坏。
    """

    def test_excluded_flow_not_in_total(self):
        """规则链已经认出这行是什么并决定不算了，再报成「挂不上要查」是自相矛盾。"""
        rows = [{"metric_id": "trade_receipt", "link_key": EXCLUDED_KEY,
                 "amount": -477800.64, "row_no": 1}]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == 0.0
        assert buckets[0][0] == BUCKET_EXCLUDED_FLOW
        assert buckets[0][2] == -477800.64, "不进总额，但金额要照实列出来"

    def test_key_found_but_not_in_this_period(self):
        """订单号取到了、格式也对，只是订单不在本期。账期边界不是数据问题。"""
        rows = [{"metric_id": "trade_receipt", "link_key": "4502253026216007946",
                 "amount": 175201.61, "row_no": 1}]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == 0.0
        assert buckets[0][0] == BUCKET_OTHER_PERIOD

    def test_no_key_is_the_only_thing_that_counts(self):
        rows = [{"metric_id": "software_fee", "link_key": None,
                 "amount": -308.31, "row_no": 1}]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -308.31
        assert buckets[0][0] == BUCKET_NEEDS_WORK

    def test_empty_key_counts_as_no_key(self):
        rows = [{"metric_id": "software_fee", "link_key": "", "amount": -5.0, "row_no": 1}]
        _buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -5.0

    def test_three_reasons_separated(self):
        """三类混在一起时，总额只留要人查的那一类。"""
        rows = [
            {"metric_id": "trade_receipt", "link_key": EXCLUDED_KEY,
             "amount": -477800.64, "row_no": 1},
            {"metric_id": "trade_receipt", "link_key": "4502253026216007946",
             "amount": 175201.61, "row_no": 2},
            {"metric_id": "software_fee", "link_key": None, "amount": -308.31, "row_no": 3},
        ]
        buckets, total = _bucket_unlinked(_facts(rows), _model())
        assert total == -308.31, "净额 -30.29 万没有业务含义，不该是报出来的那个数"
        assert len(buckets) == 3
        assert {label for label, _, _ in buckets} == {
            BUCKET_EXCLUDED_FLOW, BUCKET_OTHER_PERIOD, BUCKET_NEEDS_WORK,
        }

    def test_needs_work_sorts_first(self):
        """要人查的排最前。它是唯一需要行动的，埋在中间就等于没报。"""
        rows = [
            {"metric_id": "trade_receipt", "link_key": EXCLUDED_KEY,
             "amount": -477800.64, "row_no": 1},
            {"metric_id": "trade_receipt", "link_key": "45022530262160079",
             "amount": 175201.61, "row_no": 2},
            {"metric_id": "software_fee", "link_key": None, "amount": -308.31, "row_no": 3},
        ]
        buckets, _total = _bucket_unlinked(_facts(rows), _model())
        assert buckets[0][0] == BUCKET_NEEDS_WORK

    def test_company_wide_wins_over_other_reasons(self):
        """公司级主表优先：那些行属于哪家店都还没确定，谈不上账期或规则。"""
        rows = [{"metric_id": "freight_cost", "source_id": "freight",
                 "link_key": "12345", "amount": -5000.0, "row_no": 1}]
        buckets, total = _bucket_unlinked(_facts(rows), _model(company_wide=("freight",)))
        assert total == 0.0
        assert "其他店" in buckets[0][0]
