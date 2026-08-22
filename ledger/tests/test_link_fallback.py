"""对账表备注里给的可能是子订单号，主订单号挂不上时要换算过去。

淘宝对账表没有订单号列，订单号埋在备注里，平台给的有时是主订单号、有时是子订单号。
人工表用 XLOOKUP 在主订单编号、子订单编号两列里查，命中都返回主订单编号，再按收入
分配率摊到子订单。引擎原来只认主订单号，给了子订单号的行全部落进「挂不上订单」：
天猫皇莉诗 2026-06 有 119 行、营销费用 -58.39 元，报表上少这笔钱而一处不报错。
"""

from __future__ import annotations

import polars as pl
import pytest
from conftest import MODELS

from ledger.engine.link import Spine, link
from ledger.model.loader import load_model
from ledger.model.schema import LinkRule, Metric, ValueExpr


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")

#: 两个主订单。M1 下面两个子订单，号和主订单号不同；M2 只有一个子订单，
#: 子订单号就等于主订单号（实测天猫皇莉诗 41,889 个子订单里 19,525 个是这种）。
_SPINE = [
    {"order_id": "M1", "sub_order_id": "S11", "store": "店", "period": "2026-06"},
    {"order_id": "M1", "sub_order_id": "S12", "store": "店", "period": "2026-06"},
    {"order_id": "M2", "sub_order_id": "M2", "store": "店", "period": "2026-06"},
]


def _metric(**link_kw) -> Metric:
    base = dict(key="base_order_id", to="order.order_id", grain="order")
    base.update(link_kw)
    return Metric(
        id="marketing_fee",
        name="平台营销费用",
        source="settlement",
        occasional=True,
        value=ValueExpr(op="sum", of=["outgo"]),
        link=LinkRule(**base),
    )


def _fees(keys: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"base_order_id": keys, "outgo": [-1.0] * len(keys)})


class TestSubOrderKeysGetMappedToMainOrder:
    def test_sub_order_key_does_not_link_without_fallback(self) -> None:
        """修之前的行为：备注给了子订单号，这一行挂不上，钱留在未挂钩那一桶。"""
        frame, report = link(
            _fees(["M1", "S12"]), _metric(), Spine(frame=pl.DataFrame(_SPINE))
        )
        assert report.linked_rows == 1
        assert report.fallback_rows == 0
        assert frame.get_column("__linked__").to_list() == [True, False]

    def test_sub_order_key_links_via_fallback(self) -> None:
        frame, report = link(
            _fees(["M1", "S12"]),
            _metric(fallback_to=("order.sub_order_id",)),
            Spine(frame=pl.DataFrame(_SPINE)),
        )
        assert report.linked_rows == 2
        assert report.fallback_rows == 1

    def test_key_is_rewritten_to_the_main_order(self) -> None:
        """换算后键必须是主订单号，否则分摊会挂在子订单级，收入分配率对不上。"""
        frame, _ = link(
            _fees(["S11", "S12"]),
            _metric(fallback_to=("order.sub_order_id",)),
            Spine(frame=pl.DataFrame(_SPINE)),
        )
        assert frame.get_column("__link_key__").to_list() == ["M1", "M1"]

    def test_single_sub_order_main_order_is_left_alone(self) -> None:
        """子订单号等于主订单号的那批已经挂上了，不该再被换算表改写一遍。"""
        frame, report = link(
            _fees(["M2"]),
            _metric(fallback_to=("order.sub_order_id",)),
            Spine(frame=pl.DataFrame(_SPINE)),
        )
        assert report.fallback_rows == 0
        assert report.linked_rows == 1
        assert frame.get_column("__link_key__").to_list() == ["M2"]

    def test_unknown_key_still_does_not_link(self) -> None:
        """既不是主订单也不是子订单的号（别期或别店的单）照旧挂不上。"""
        _, report = link(
            _fees(["X9"]),
            _metric(fallback_to=("order.sub_order_id",)),
            Spine(frame=pl.DataFrame(_SPINE)),
        )
        assert report.linked_rows == 0
        assert report.fallback_rows == 0


class TestTaobaoSettlementMetricsAllHaveIt:
    """七个淘宝对账科目共用一套人工口径，不能只给营销费用开这条路。"""

    def test_all_seven_declare_the_fallback(self, model) -> None:
        ids = [
            "trade_receipt", "trade_refund", "software_fee", "logistics_fee",
            "cross_border_fee", "trade_compensation", "marketing_fee",
        ]
        for mid in ids:
            m = model.metric(mid)
            assert m.platform == "taobao", mid
            assert m.link is not None and m.link.to == "order.order_id", mid
            assert m.link.fallback_to == ("order.sub_order_id",), mid
