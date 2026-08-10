"""没进利润的钱怎么报。

这块的要求不是「算得准」，是「报得准」。挂不上订单的钱必须让人看见——不能悄悄
丢掉，也不能硬摊进利润。但报得过头一样有害：淘宝的未归属曾经虚报到 120 万，
1688 曾经虚报 54.8 万，这种量级会让人干脆不看这个提示，等于白报。

两个虚报的来源都不是数据问题，是统计口径：
一是同一物理行被多个指标各算一次，二是公司级主表里别家店的钱被算进本店。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.audit import _bucket_unlinked
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
