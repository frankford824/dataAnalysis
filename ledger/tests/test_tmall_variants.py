"""同一个平台、同一个导出入口，表头可以不一样。

这一批测试全部来自 2026-08 接天猫皇莉诗旗舰店这一家店时踩到的坑。它们共同的形状是：
表头少了一列，识别悄悄走岔，界面上没有任何红字，只是账上某一项变成空或者零。

所以判据都写成「这个表头必须路由到哪个模板」，而不是「金额等于多少」——金额错是
后果，路由错是原因，而路由是纯函数，一条断言就能钉死。
"""

from __future__ import annotations

import pytest
from conftest import MODELS

from ledger.engine.recognize import match_headers
from ledger.engine.types import FileRef
from ledger.model.loader import load_model

#: 天猫皇莉诗那份微信账单：8 列，一列「淘宝订单编号」，写的是入「账」类型。
WECHAT_TMALL = [
    "入账时间", "支付流水号", "淘宝订单编号", "入账类型",
    "收入金额（元）", "支出金额", "业务描述", "备注",
]

#: 淘宝喜必顺那份微信账单：14 列，带商家昵称和主/子订单id，写的是入「帐」类型。
WECHAT_XIBISHUN = [
    "商家昵称", "入帐日期", "入帐时间", "支付流水号", "主订单id", "子订单id",
    "入帐类型", "收入金额(元)", "支出金额(元)", "业务描述", "备注",
    "收/付渠道", "数据创建时间", "数据修改时间",
]

#: 聚水潭成本导出。天猫这份没有末尾那列人手加的「总成本」。
JUSHUITAN = [
    "内部订单号", "线上订单号", "店铺名称", "下单时间", "应付金额", "已付金额",
    "状态", "快递单号", "订单类型", "旗帜", "平台站点", "子订单编号",
    "线上子订单编号", "原始线上订单号", "商品编码", "商品名称", "数量",
    "商品单价", "商品金额", "原价", "买家实付", "收入运费", "支出运费", "成本价",
]

#: 刷单表。天猫这份没有「总金额」，只有本金和佣金。
BRUSHING_TMALL = [
    "下单日期", "付款日期", "店铺", "订单号", "本金", "佣金",
    "平台费", "本金核对", "付款平台", "平台账号", "备注",
]


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


def route(model, headers: list[str], filename: str) -> str | None:
    """这个表头在这个文件名下会走到哪个模板。"""
    ref = FileRef(sha256="x" * 64, filename=filename, sheet=None)
    return match_headers(headers, model, ref).template_id


class TestTwoWechatLayouts:
    """千牛的微信账单有两种导出，差一个字和六列。

    「入账」和「入帐」是两个不同的字，表头归一只折全角括号，不会把它们变成一个。
    加上少了商家昵称、主订单id、子订单id，v1 的五个必需列只对上一个——连
    「接近某个模板」都报不出来，界面上只有一句「没见过这种表头（8 列）」。

    后果不是少算一点：这家店整条微信渠道不进账，一个月 4,009 笔交易收款、
    146,132.59 元完全不在收入里，而损益表上每一行都有数，看不出缺了什么。
    """

    def test_the_eight_column_layout_is_recognised(self, model) -> None:
        assert route(model, WECHAT_TMALL, "对账微信-天猫皇莉诗旗舰店.xlsx") == \
            "taobao_settlement_wechat_v2"

    def test_the_fourteen_column_layout_still_goes_to_v1(self, model) -> None:
        """加了 v2 不能把原来那份抢过来——喜必顺的历史账靠 v1 算出来的。"""
        assert route(model, WECHAT_XIBISHUN, "对账-淘宝喜必顺.xlsx") == \
            "taobao_settlement_wechat_v1"

    def test_the_two_layouts_do_not_collide(self, model) -> None:
        """两张表头互不认领对方的模板。写死这一条是因为它们像得很危险：
        支付流水号、业务描述、备注三列同名，金额列只差括号的全角半角。
        """
        assert route(model, WECHAT_TMALL, "对账微信-x.xlsx") != \
            route(model, WECHAT_XIBISHUN, "对账-x.xlsx")


class TestAHumanFormulaColumnMustNotBeRequired:
    """人手加的公式列不能当必需列。

    这两张表的公式列都是可以从原始列推出来的（总成本=成本价*数量，
    总金额=本金+佣金），算账本来也不读它们。可它们一旦写进必需列或必需绑定，
    没有这一列的那份导出就整张走岔，而且都不报错：

        聚水潭少「总成本」  → 模板匹配不上，表头相同的「补发订单成本」接过去，
                              商品成本整项为空，毛利和利润都算不出来
        刷单少「总金额」    → 绑定失败整张表解析不了，本金佣金恒为 0
    """

    def test_jushuitan_without_the_total_column_is_still_cost(self, model) -> None:
        assert route(model, JUSHUITAN, "聚水潭成本-天猫皇莉诗旗舰店.xlsx") == \
            "jushuitan_cost_v1"

    def test_jushuitan_with_the_total_column_is_still_cost(self, model) -> None:
        assert route(model, [*JUSHUITAN, "总成本"], "聚水潭成本-淘宝喜必顺.xlsx") == \
            "jushuitan_cost_v1"

    def test_the_reship_copy_is_still_routed_by_filename(self, model) -> None:
        """补发表是聚水潭表筛出来粘的副本，表头一模一样，只能靠文件名分开。
        放宽聚水潭的必需列之后这条路由必须还在，否则补发成本会被当成商品成本
        再算一遍。
        """
        assert route(model, JUSHUITAN, "补发-淘宝喜必顺.xlsx") == "reshipment_v1"

    def test_brushing_without_the_total_column_still_parses(self, model) -> None:
        assert route(model, BRUSHING_TMALL, "刷单-天猫皇莉诗旗舰店.xlsx") == \
            "brushing_v1"

    def test_brushing_amount_comes_from_principal_plus_commission(self, model) -> None:
        """本金佣金的金额必须自己加出来，不能引用总金额那一列。"""
        assert model.metric("brushing_cost").value.of == ("principal", "commission")


class TestStoreNamesThatContainEachOther:
    """「天猫皇莉诗旗舰店」整个包含着京东那家的别名「皇莉诗旗舰店」。

    归属按最长匹配定唯一一家，交表那条路一直是这么做的。这里钉的是离线那两条路
    （回放、验收）也走同一套规则：它们原先逐店问 `Store.owns`，两家都答是，
    于是天猫那份 1,459,425.47 元的支付宝对账会同时落进京东皇莉诗的账里——
    表现是京东那边凭空多出一堆「字典里没有的费项」，因为拿京东的字典查淘宝的科目名。
    """

    def test_one_file_belongs_to_exactly_one_store(self, model) -> None:
        names = ["对账支付宝-天猫皇莉诗旗舰店.xlsx", "运费-皇莉诗旗舰店.xlsx"]
        owners = {
            sid: model.files_of(sid, names) for sid in ("taobao_msy387nx", "jd_huanglishi")
        }
        assert owners["taobao_msy387nx"] == ["对账支付宝-天猫皇莉诗旗舰店.xlsx"]
        assert owners["jd_huanglishi"] == ["运费-皇莉诗旗舰店.xlsx"]

    def test_owns_alone_is_not_ownership(self, model) -> None:
        """`owns` 只是包含判断，两家店会同时答是。这条钉住这个事实，
        免得有人再拿它当归属用。
        """
        both = [
            s.id for s in model.stores if s.owns("对账支付宝-天猫皇莉诗旗舰店.xlsx")
        ]
        assert sorted(both) == ["jd_huanglishi", "taobao_msy387nx"]
