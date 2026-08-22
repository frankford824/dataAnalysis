"""改判到一个本平台没有指标的科目，钱会掉出损益表——这道关卡拦住它。

界面上「改判」那一栏是最常用的功能，而指标是按平台各自一套配的。人把某个费项
改判到另一个科目，那个科目在这个平台没有指标，投影时就没人认领这批行：损益表上
少一笔钱，自检全绿，界面上什么都看不出来。

踩过两次，两次都是人工逐项对账才发现的：

  2026-08-19  拼多多的多多进宝配成营销费用，拼多多没有营销费用指标，
              4.79 元进不了「平台营销费用」。
  2026-08-20  1688 的大分销抽佣等配成平台服务费、仅退款补贴配成交易赔付，
              1688 两个指标都没有，1,720 行 349.57 元直接从损益表消失——
              销售支出从 -375.43 收到 -25.86，利润反而多出同样的数。
              费用科目之间搬家却动了利润，那就是钱掉了的样子。

判据是「别家平台认这个科目、偏偏这个平台没指标」，不是「没指标」。广告预充值、
保证金、提现、往来款这些本来就不进损益，一个平台都没给指标，那是对的。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.audit import audit
from ledger.engine.types import ClassifyReport, Completeness
from ledger.model.schema import Check, Metric, Model, SourceContract, Store, ValueExpr

CHECK = Check(id="chk", name="科目有指标接着", kind="major_has_metric")


def _model() -> Model:
    """淘宝有平台服务费指标，1688 没有。广告预充值两家都没有。"""
    return Model(
        id="t", name="t",
        stores=(
            Store(id="tb", name="淘宝甲店", platform="taobao"),
            Store(id="ali", name="1688乙店", platform="alibaba1688"),
        ),
        sources=(SourceContract(id="settlement", name="对账",
                                owner_role="shop_owner", cadence="monthly"),),
        metrics=(
            Metric(id="software_fee", name="平台服务费", source="settlement",
                   platform="taobao", major="software_fee",
                   value=ValueExpr(op="sum", of=["outgo"])),
            Metric(id="trade_expense_1688", name="销售支出", source="settlement",
                   platform="alibaba1688", major="trade_expense_1688",
                   value=ValueExpr(op="sum", of=["outgo"])),
        ),
        checks=(CHECK,),
    )


def _facts(store: str, majors: list[tuple[str, float]]) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "metric_id": "trade_expense_1688", "source_id": "settlement",
            "store": store, "period": "2026-05", "major": major, "minor": None,
            "subject": None, "amount": amount, "linked": False, "counted": False,
            "link_key": None, "grain": "order",
            "file_sha": "sha", "file_name": "f.xlsx", "sheet": "S", "row_no": i + 1,
        }
        for i, (major, amount) in enumerate(majors)
    ])


def _run(store: str, majors: list[tuple[str, float]]):
    model = _model()
    result = audit(model, _facts(store, majors), {}, ClassifyReport(),
                   Completeness(), {})
    return next(f for f in result.findings if f.check_id == "chk")


class TestReassignedSubjectNeedsAMetric:
    def test_a_subject_other_platforms_have_but_this_one_lacks_blocks(self):
        got = _run("1688乙店", [("software_fee", -349.57)])
        assert not got.passed
        assert got.blocking
        assert "software_fee" in got.message
        assert got.detail["majors"] == [
            {"major": "software_fee", "rows": 1, "amount": -349.57},
        ]

    def test_the_same_subject_passes_where_it_has_a_metric(self):
        assert _run("淘宝甲店", [("software_fee", -349.57)]).passed

    def test_a_subject_no_platform_books_is_not_reported(self):
        """广告预充值一个平台都没指标，那是资产划转，报它只会教人忽略这条。"""
        assert _run("1688乙店", [("ad_topup", -90000.0)]).passed

    def test_the_metrics_own_major_always_passes(self):
        assert _run("1688乙店", [("trade_expense_1688", -25.86)]).passed

    def test_one_physical_row_is_counted_once(self):
        """同一行会被多个指标各投影一遍，按行去重才不会把一笔钱数成几笔。"""
        model = _model()
        one = _facts("1688乙店", [("software_fee", -100.0)])
        twice = pl.concat([one, one.with_columns(
            pl.lit("software_fee_1688").alias("metric_id"))])
        result = audit(model, twice, {}, ClassifyReport(), Completeness(), {})
        got = next(f for f in result.findings if f.check_id == "chk")
        assert got.detail["majors"] == [
            {"major": "software_fee", "rows": 1, "amount": -100.0},
        ]
