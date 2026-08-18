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


class TestTheSameThingSpelledTwoWays:
    """同一列在不同店的导出里用不同的词写同一件事，过滤条件必须两个都认。

    这一类错误的共同形状是：过滤条件看起来在把关，实际一行也匹配不上，而它不报错。
    一条永远匹配不上的过滤条件，和没有这条过滤是一回事。
    """

    @staticmethod
    def _values(predicates) -> set[str]:
        out: set[str] = set()
        for p in predicates:
            out |= set(p.value) if isinstance(p.value, (list, tuple)) else {p.value}
        return out

    def test_cancelled_orders_are_excluded_under_both_spellings(self, model) -> None:
        """聚水潭这一列在天猫那份写「取消」，别的四家写「已取消」。

        原先只认「已取消」，于是天猫那 9,203 行取消订单的成本 79,999.07 元
        （落进 2026-06 的是 6,074 行、54,622.45 元）被当成真成本计了进去，
        利润凭空少这么多。前四家店没暴露这个问题不是因为条件写对了，是因为那四家的
        店长按导表说明手工删掉了取消行。
        """
        for metric_id in ("goods_cost", "reshipment_cost"):
            states = [
                p for p in model.metric(metric_id).where if p.field == "order_state"
            ]
            assert states, f"{metric_id} 少了排除取消订单那条"
            values = self._values(states)
            assert {"取消", "已取消"} <= values, f"{metric_id} 只认了一种写法：{values}"

    def test_the_1688_override_keeps_excluding_cancelled_orders(self, model) -> None:
        """1688 那条 by_platform 覆盖了整个 where（它的商品成本口径含补发），
        取消那条必须自己带上，否则覆盖时会被一起清掉。
        """
        rule = next(
            r for r in model.metric("goods_cost").by_platform if r.platform == "alibaba1688"
        )
        states = [p for p in rule.where if p.field == "order_state"]
        assert {"取消", "已取消"} <= self._values(states)

    def test_an_empty_state_still_counts_as_cost(self, model) -> None:
        """不知道状态不等于已取消。因为不知道就丢掉一笔成本，账上只表现为利润高一点。"""
        state = next(
            p for p in model.metric("goods_cost").where if p.field == "order_state"
        )
        assert state.include_null


class TestOrderIdsHiddenBehindEnglishWords:
    """备注里的订单号不总是写在中文说法后面。

    取号规则链原先七条全是中文（订单号、订单编号、关联订单号、交易单号），淘宝联盟
    的代扣把订单号写成 `tradeid:***`，于是皇莉诗那 408 行代扣一条都挂不上。金额很碎
    （净 -206.05 元），但笔数在「要查归属」那一桶里排第一——几百笔查不动的碎账
    会让人学会无视整个提示，而那一桶是拦着结账的。
    """

    #: 真实备注，从对账支付宝-天猫皇莉诗旗舰店.xlsx 抄下来的。
    REMARK = ("代扣款（扣款用途：淘宝联盟佣金代扣 tradeid:3302710287203084298 "
              "memberid:3792292908 fee:0.49，付款方：杭州阿里妈妈淘联信息技术有限公司）")

    def _rules(self, model):
        return model.template("taobao_settlement_alipay_v1").key_rules

    def test_the_tradeid_rule_pulls_the_order_id_out(self, model) -> None:
        import re
        rule = next(
            r for r in self._rules(model)
            if r.when.field == "remark" and "tradeid" in (r.when.extract or "")
        )
        assert re.search(rule.when.extract, self.REMARK).group(1) == "3302710287203084298"

    def test_it_does_not_shadow_the_exclusion_rules(self, model) -> None:
        """排除非经营流水那条必须仍在最前面。它排在取号规则后面的话，
        余利宝申购、网商银行调拨会先被抓个订单号出来，排除就永远轮不到。
        """
        rules = self._rules(model)
        excluding = next(i for i, r in enumerate(rules) if r.exclude)
        tradeid = next(
            i for i, r in enumerate(rules)
            if r.when.field == "remark" and "tradeid" in (r.when.extract or "")
        )
        assert excluding < tradeid


class TestTheOrderIdColumnThatHoldsSomethingElse:
    """「业务基础订单号」这一列偶尔放的不是订单号。

    规则链第 2 条原先无条件采用这一格。记账本转账（支付宝转账小额打款给买家）那一格
    写的是打款流水号 FP301_8587437774500402，两家店共 105 行。后果不是挂不上，
    是挂着一个永远挂不上的键，落进「订单号取到了、只是订单不在本期」那一桶——
    一句说得通但不成立的解释，人会照着它去查跨期结算。

    真订单号就在备注里，规则链第 6 条（关联订单号：***）一条全收。人工表算这批钱：
    它自己那张对账表的费项2 列写的是交易赔付，32 行 -214.87。
    """

    FP301 = "FP301_8587437774500402"
    REMARK = "支付宝转账小额打款-关联订单号：3303671307562048393"

    def _rule(self, model):
        return next(
            r for r in model.template("taobao_settlement_alipay_v1").key_rules
            if r.when.field == "base_order_id"
        )

    def test_a_payment_serial_number_is_not_taken_as_an_order_id(self, model) -> None:
        import re
        assert re.search(self._rule(model).when.extract, self.FP301) is None

    def test_a_real_order_id_still_goes_through(self, model) -> None:
        import re
        found = re.search(self._rule(model).when.extract, "3303671307562048393")
        assert found and found.group(1) == "3303671307562048393"

    def test_the_whole_cell_must_be_digits(self, model) -> None:
        """必须整格匹配。只要求「含有一串数字」的话，FP301_8587437774500402 里
        那串数字会被抓出来当订单号，等于什么都没改。
        """
        import re
        assert re.search(self._rule(model).when.extract, "abc3303671307562048393") is None
        assert re.search(self._rule(model).when.extract, "3303671307562048393x") is None

    def test_the_remark_rule_picks_up_what_it_dropped(self, model) -> None:
        import re
        rule = next(
            r for r in model.template("taobao_settlement_alipay_v1").key_rules
            if r.when.field == "remark" and "关联订单号" in (r.when.extract or "")
        )
        assert re.search(rule.when.extract, self.REMARK).group(1) == "3303671307562048393"


class TestTopUpVersusCompensation:
    """保证金充值和保证金赔付在同一个科目下，只有备注分得开。

    对账表公式说明的条件 8 写着「如备注项**为**保证金解冻/天猫保证金-充值（代扣）则
    清楚对应费项单元格内容」。「为」是精确相等：备注后面挂了赔付原因的（-延迟发货、
    -物流轨迹超时、-邮费争议…）是平台按这个原因赔了买家、再从余额把保证金补回来，
    钱真出去了，人工表照样算（喜必顺那份表的费项2 列写着交易赔付，27 行 -150.02）。

    两个方向都会错账，所以两个方向都钉：
      一条都不排 → 天猫皇莉诗 2026-06 多出 -2,090.32 元不存在的赔付；
      按科目全排 → 那 27 行真赔付被一起丢掉，账面上只表现为利润高一点。
    """

    PURE_TOP_UP = "天猫保证金-充值（代扣）"
    WITH_REASON = "天猫保证金-充值（代扣）-延迟发货"

    def _rules(self, model):
        return model.template("taobao_settlement_alipay_v1").classify_rules

    def _top_up_rule(self, model):
        return next(
            r for r in self._rules(model)
            if r.exclude and r.when and self.PURE_TOP_UP in (r.when.equals or ())
        )

    def test_a_pure_top_up_is_excluded(self, model) -> None:
        assert self._top_up_rule(model).exclude

    def test_it_matches_on_equality_not_containment(self, model) -> None:
        rule = self._top_up_rule(model)
        assert rule.when.equals and not rule.when.contains, (
            "写成 contains 会把带赔付原因的那 27 行真赔付一起排掉"
        )
        assert self.WITH_REASON not in (rule.when.equals or ())

    def test_it_runs_before_the_dictionary(self, model) -> None:
        """字典里「保证金-天猫-出账缴存 → 交易赔付」查得到，字典一命中就轮不到排除。"""
        rules = self._rules(model)
        top_up = rules.index(self._top_up_rule(model))
        dictionary = next(i for i, r in enumerate(rules) if r.dictionary)
        assert top_up < dictionary

    def test_the_unfreeze_leg_is_still_excluded(self, model) -> None:
        """充值和解冻是同一件事的两个方向，条件 8 一起写的，不能只剩一条。"""
        assert any(
            r.exclude and r.when and "天猫保证金-解冻" in (r.when.contains or ())
            for r in self._rules(model)
        )


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
