"""各平台的利润口径确实不一样，模型要如实表达。

这不是数据脏，是三个平台的账本来就这么算的——每家店的订单明细第一行就写着
自己的 SUMIFS 公式：

    淘宝    七项费用按销售收入占比分摊到子订单，补发订单从商品成本里排除
    1688    按 COUNTIFS 均摊，补发成本并进商品成本不单列
    抖音    直接取每个子订单的结算净额，不分摊

把它们强行统一成一种算法，算出来的利润和公司实际出的账对不上；给每个平台
复制一套指标，损益表就散了。所以指标带平台覆盖：一套对外口径，平台各自的算法
写在覆盖里。

覆盖有三态语义，最容易写错，所以这里逐条钉住：
不写 = 继承，写空 = 显式清掉，direct = 明确不分摊。
"""

from __future__ import annotations

from ledger.model.schema import (
    Allocation,
    LinkRule,
    Metric,
    PlatformRule,
    Predicate,
    ValueExpr,
)


def _metric(**kw) -> Metric:
    base = dict(
        id="goods_cost",
        name="商品成本",
        source="order_cost",
        value=ValueExpr(op="sum", of=["total_cost"]),
        link=LinkRule(key="sub_order_id", to="order.sub_order_id", grain="order"),
        allocate=Allocation(mode="ratio", by="alloc_ratio"),
        where=(Predicate(field="is_reshipment", op="ne", value="true"),),
    )
    base.update(kw)
    return Metric(**base)


class TestPlatformScope:
    def test_wildcard_applies_everywhere(self):
        m = _metric()
        assert m.for_platform("taobao") is not None
        assert m.for_platform("douyin") is not None

    def test_platform_specific_metric_is_skipped_elsewhere(self):
        """淘宝的软件服务费只有淘宝有，别的平台不该凭空出现这一项。"""
        m = _metric(id="software_fee", platform="taobao")
        assert m.for_platform("taobao") is not None
        assert m.for_platform("douyin") is None

    def test_disabled_for_one_platform(self):
        m = _metric(by_platform=(PlatformRule(platform="douyin", disabled=True),))
        assert m.for_platform("douyin") is None
        assert m.for_platform("taobao") is not None


class TestOverrideSemantics:
    def test_unmatched_rule_changes_nothing(self):
        m = _metric(by_platform=(PlatformRule(platform="douyin", direct=True),))
        got = m.for_platform("taobao")
        assert got.allocate is not None
        assert got.allocate.mode == "ratio"

    def test_link_override(self):
        """抖音的发货运费按子订单直接匹配，运费表自带子订单号。"""
        m = _metric(
            id="freight_cost",
            by_platform=(
                PlatformRule(
                    platform="douyin",
                    link=LinkRule(key="sub_order_id", to="order.sub_order_id", grain="order"),
                ),
            ),
        )
        assert m.for_platform("douyin").link.key == "sub_order_id"

    def test_direct_clears_allocation(self):
        """抖音的结算净额本来就是每个子订单一条，再分摊就错了。"""
        m = _metric(by_platform=(PlatformRule(platform="douyin", direct=True),))
        assert m.for_platform("douyin").allocate is None
        assert m.for_platform("taobao").allocate is not None

    def test_allocation_mode_override(self):
        """1688 按 COUNTIFS 均摊，不按收入占比。"""
        m = _metric(
            by_platform=(
                PlatformRule(platform="alibaba1688", allocate=Allocation(mode="even")),
            )
        )
        got = m.for_platform("alibaba1688")
        assert got.allocate.mode == "even"

    def test_empty_where_clears_filter(self):
        """1688 把补发成本并进商品成本，不像淘宝那样排除掉。

        这里必须能区分「没写 where」和「写了空 where」：前者继承淘宝的排除条件，
        后者是明确表示这个平台不排除。实测差 240.90 元就出在这上面。
        """
        m = _metric(by_platform=(PlatformRule(platform="alibaba1688", where=()),))
        assert m.for_platform("alibaba1688").where == ()
        assert len(m.for_platform("taobao").where) == 1

    def test_absent_where_inherits(self):
        m = _metric(by_platform=(PlatformRule(platform="alibaba1688", allocate=Allocation(mode="even")),))
        assert len(m.for_platform("alibaba1688").where) == 1, "没写 where 就该继承"

    def test_major_override(self):
        m = _metric(by_platform=(PlatformRule(platform="douyin", major="trade_net_douyin"),))
        assert m.for_platform("douyin").major == "trade_net_douyin"

    def test_override_does_not_mutate_original(self):
        """取平台视图不能改到原指标，否则平台之间会串味。"""
        m = _metric(by_platform=(PlatformRule(platform="douyin", direct=True),))
        m.for_platform("douyin")
        assert m.allocate is not None and m.allocate.mode == "ratio"
