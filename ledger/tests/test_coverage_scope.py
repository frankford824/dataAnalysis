"""覆盖率的分母是「预期有这项数据的订单」，不是全部订单。

这是三家店都卡在结不了账的原因。没发货的订单不会有出库成本，聚水潭里根本没有
那一行；全额退款的订单钱进了又出，对账表上没有净收款那一行。把这些订单算进
分母，覆盖率就永远差十几个点，而缺口是虚的——实测缺商品成本的订单里没运单号的
占 80%(淘宝)、94%(1688)、95%(抖音)。

分母收窄有个反向风险：条件写得太宽会把真缺口一起豁免掉，所以分子必须同步收窄，
覆盖率不能靠"分子留全、分母减小"凑到 100% 以上。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.link import Spine, link
from ledger.model.schema import LinkRule, Metric, PlatformRule, Predicate, ValueExpr


def _metric(**kw) -> Metric:
    base = dict(
        id="goods_cost",
        name="商品成本",
        source="order_cost",
        value=ValueExpr(op="sum", of=["total_cost"]),
        link=LinkRule(key="sub_order_id", to="order.sub_order_id", grain="order"),
    )
    base.update(kw)
    return Metric(**base)


#: 四个子订单，两个发了货两个没发。成本表只提供了已发货那两个。
_SPINE = [
    {"sub_order_id": "A1", "tracking_no": "SF001", "store": "店", "period": "2026-05"},
    {"sub_order_id": "A2", "tracking_no": "SF002", "store": "店", "period": "2026-05"},
    {"sub_order_id": "B1", "tracking_no": None, "store": "店", "period": "2026-05"},
    {"sub_order_id": "B2", "tracking_no": "", "store": "店", "period": "2026-05"},
]


def _cost(keys: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"sub_order_id": keys, "total_cost": [10.0] * len(keys)})


class TestExpectNarrowsDenominator:
    def test_without_expect_unshipped_orders_drag_coverage_down(self):
        """不声明预期时分母是全部四笔，覆盖率只有一半——这是修之前的行为。"""
        _, report = link(_cost(["A1", "A2"]), _metric(), Spine(frame=pl.DataFrame(_SPINE)))
        assert report.spine_keys == 4
        assert report.coverage == 0.5
        assert report.expect_label == ""

    def test_expect_excludes_unshipped(self):
        m = _metric(
            expect=(Predicate(field="tracking_no", op="notnull"),),
            expect_label="已发货",
        )
        _, report = link(_cost(["A1", "A2"]), m, Spine(frame=pl.DataFrame(_SPINE)))
        assert report.spine_keys == 2
        assert report.spine_keys_total == 4
        assert report.coverage == 1.0
        assert report.expect_label == "已发货"

    def test_blank_string_counts_as_no_value(self):
        """解析器把空单元格统一成空串。空串当成有运单号的话分母会虚高。"""
        m = _metric(expect=(Predicate(field="tracking_no", op="notnull"),))
        _, report = link(_cost(["A1", "A2"]), m, Spine(frame=pl.DataFrame(_SPINE)))
        assert report.spine_keys == 2, "B2 的运单号是空串，不该算进分母"

    def test_real_gap_inside_scope_still_caught(self):
        """已发货却没成本，这才是真缺口，收窄分母不能把它豁免掉。"""
        m = _metric(expect=(Predicate(field="tracking_no", op="notnull"),))
        _, report = link(_cost(["A1"]), m, Spine(frame=pl.DataFrame(_SPINE)))
        assert report.spine_keys == 2
        assert report.coverage == 0.5

    def test_numerator_narrowed_too(self):
        """成本表给了没发货的订单，覆盖率也不能超过 100%。"""
        m = _metric(expect=(Predicate(field="tracking_no", op="notnull"),))
        _, report = link(_cost(["A1", "A2", "B1"]), m, Spine(frame=pl.DataFrame(_SPINE)))
        assert report.coverage == 1.0
        assert report.spine_keys_covered == 2

    def test_multi_condition(self):
        """淘宝的销售收入要求两个条件：已付款、未全额退款。"""
        spine = pl.DataFrame([
            {"order_id": "1", "pay_time": "2026-05-01", "refund_status": "没有申请退款"},
            {"order_id": "2", "pay_time": "2026-05-02", "refund_status": "退款成功"},
            {"order_id": "3", "pay_time": None, "refund_status": "没有申请退款"},
        ])
        m = _metric(
            link=LinkRule(key="order_id", to="order.order_id", grain="order"),
            expect=(
                Predicate(field="pay_time", op="notnull"),
                Predicate(field="refund_status", op="not_in", value=("退款成功",)),
            ),
        )
        frame = pl.DataFrame({"order_id": ["1"], "total_cost": [10.0]})
        _, report = link(frame, m, Spine(frame=spine))
        assert report.spine_keys == 1
        assert report.coverage == 1.0


class TestExpectDegradesSafely:
    """`expect` 是一句预期声明。写得不准该被看见，但不该让整个店算不出账。"""

    def test_unknown_field_falls_back_to_all_keys(self):
        m = _metric(
            expect=(Predicate(field="没有这一列", op="notnull"),),
            expect_label="已发货",
        )
        _, report = link(_cost(["A1", "A2"]), m, Spine(frame=pl.DataFrame(_SPINE)))
        assert report.spine_keys == 4
        assert report.expect_label == "", "分母没被收窄就不该声称按已发货算"

    def test_empty_spine(self):
        m = _metric(expect=(Predicate(field="tracking_no", op="notnull"),))
        _, report = link(_cost(["A1"]), m, Spine.empty())
        assert report.coverage == 1.0


class TestPlatformOverride:
    def test_platform_can_replace_expect(self):
        m = _metric(
            expect=(Predicate(field="tracking_no", op="notnull"),),
            by_platform=(
                PlatformRule(
                    platform="alibaba1688",
                    expect=(Predicate(field="pay_time", op="notnull"),),
                ),
            ),
        )
        assert m.for_platform("taobao").expect[0].field == "tracking_no"
        assert m.for_platform("alibaba1688").expect[0].field == "pay_time"

    def test_platform_can_clear_expect(self):
        """给空列表表示这个平台按全部订单要求，和"不覆盖"要分得开。"""
        m = _metric(
            expect=(Predicate(field="tracking_no", op="notnull"),),
            by_platform=(PlatformRule(platform="douyin", expect=()),),
        )
        assert m.for_platform("douyin").expect == ()
        assert m.for_platform("taobao").expect != ()

    def test_not_mentioning_expect_inherits(self):
        m = _metric(
            expect=(Predicate(field="tracking_no", op="notnull"),),
            by_platform=(PlatformRule(platform="douyin", major="其他"),),
        )
        assert m.for_platform("douyin").expect[0].field == "tracking_no"
