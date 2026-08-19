"""抖音改版：对账从「一条净额」改成「拆五项、挂主订单号」。

业务在 2026-08 补了一份抖音规则，把这家店的口径整个换掉了。老口径是一句
销售收入=sumifs(对账表动账金额, 对账表子订单号, 子订单编号)——不拆费项、不分摊，
所以这家店的损益表上「平台费用」整组是空的，看不出它有没有交平台服务费。
新口径拆成交易收款、交易退款、软件服务费、交易赔付、保证金五项，按主订单号加总
再除以 countifs，另加一张保费支出表按子订单直挂。

盯这几处：

1. 拆项之后费项归类要走字典。原先模板上压着一条「凡是有动账场景就算净额」的规则，
   留着它会盖掉字典，五项永远只出一项。
2. 货款直投千川那 1,399 行、-2,640.09 元是把货款划进广告账户，不是消耗。
   当成推广费的话推广费会从 529.95 虚涨五倍；悄悄扔掉的话又有两千多块不知去向。
   口径和万相台一致：单独列出来，不进利润。
3. 挂主订单号比挂子订单号多认领 41.74 元，这是业务改键的原因，不是误差。
4. 保费支出表本期只有表头。空表和没有这张表是两件事：没模板它会被当成认不出来
   的表拦在门外。
"""

from __future__ import annotations

import pytest
from conftest import MODELS, write_xlsx

from ledger.engine.classify import COL_MAJOR
from ledger.engine.recognize import recognize
from ledger.engine.runtime import ingest
from ledger.model.loader import load_model


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


#: 抖店对账单的真实表头，前 39 列。第 40 列往后是人工加的费项和透视，不参与解析。
DY_SETTLE = [
    "动账时间", "动帐流水号", "动账方向", "动账金额", "动账账户", "动账场景",
    "计费类型", "子订单号", "订单号", "售后编号", "下单时间", "商品ID", "商品名称",
    "达人ID", "达人名称", "订单类型", "订单实付应结", "运费实付", "实际平台补贴_运费",
    "实际平台补贴", "其他平台补贴", "以旧换新抵扣", "政府补贴平台垫资", "实际达人补贴",
    "实际抖音支付补贴", "实际抖音月付营销补贴", "银行补贴", "订单退款", "平台服务费",
    "佣金", "服务商佣金", "渠道分成", "招商服务费", "站外推广费", "其他分成",
    "是否免佣", "免佣金额", "商户主体名称", "备注",
]


def _dy_row(scene, amount, sub="", order="", direction="入账"):
    row = [""] * len(DY_SETTLE)
    row[0] = "2026-05-20 10:00:00"
    row[1] = f"流水{scene}{amount}"
    row[2] = direction
    row[3] = amount
    row[5] = scene
    row[7] = sub
    row[8] = order
    row[37] = "义乌星泽天成供应链管理有限公司"
    return row


def _classified(tmp_path, rows, model):
    path = write_xlsx(tmp_path / "对账-抖音浅花涧节日装饰.xlsx", [["名称：对账"], *rows])
    result = ingest([path], model, [s.name for s in model.stores])
    item = next(i for i in result.items if i.frame is not None)
    from ledger.engine.classify import classify

    out, _ = classify(item.frame, model, "douyin", template=item.template)
    return out, item


class TestTheFiveItemsAreSplitApart:
    def test_the_dictionary_decides_the_item_not_a_catch_all_rule(self, tmp_path, model):
        out, _ = _classified(tmp_path, [
            DY_SETTLE,
            _dy_row("货款结算入账", "16.32", sub="123F00", order="123"),
            _dy_row("退款", "-4.57", sub="124F00", order="124", direction="出账"),
            _dy_row("偏远地区物流服务", "-6.80", sub="125F00", order="125", direction="出账"),
            _dy_row("消费者赔付", "-3.00", sub="126F00", order="126", direction="出账"),
        ], model)
        assert out.get_column(COL_MAJOR).to_list() == [
            "trade_receipt", "trade_refund", "software_fee", "trade_compensation",
        ]

    def test_every_item_has_a_metric_and_a_statement_line(self, model):
        """拆出来的五项都要有指标接着，也都要在损益表上有行。

        少一项的后果是那笔钱归类归对了、却没有指标消费它，于是它既不进利润也不
        在未归类里——报表上看不出少了什么。
        """
        douyin = [m for m in model.metrics if m.platform == "douyin"]
        assert {m.major for m in douyin if m.major} >= {
            "trade_receipt", "trade_refund", "software_fee", "trade_compensation",
        }
        fed = {mid for node in model.statement for mid in (node.formula.of if node.formula else ())}
        for m in douyin:
            assert m.id in fed, f"{m.id} 没有任何损益行消费它"

    def test_the_receipt_hangs_on_the_parent_order(self, model):
        """挂主订单号，不是子订单号。这是业务改键的地方。"""
        metric = model.metric("trade_receipt_douyin").for_platform("douyin")
        assert metric is not None and metric.link is not None
        assert metric.link.key == "base_order_id"
        assert metric.allocate is not None and metric.allocate.mode == "even"


class TestQianchuanTopUpIsNotAdSpend:
    """货款直投千川是充值，不是消耗。"""

    def test_it_is_parked_like_wanxiangtai(self, model):
        entry = next(e for e in model.dictionary
                     if e.platform == "douyin" and e.raw == "货款直投千川")
        assert entry.major == "ad_topup"
        assert entry.naturally_unlinked, "没有订单号是它的常态，不该报成「要查归属」"

    def test_no_metric_eats_it(self, model):
        """没有任何指标消费 ad_topup——那是它不进利润的唯一防线。"""
        assert "ad_topup" not in {m.major for m in model.metrics if m.major}

    def test_it_shows_up_under_its_own_name(self, tmp_path, model):
        """不进利润，但要以自己的名字摆出来。

        两千多块钱无声消失和摆一行「货款直投千川 -2,640.09」是两件事。
        """
        out, _ = _classified(tmp_path, [
            DY_SETTLE,
            _dy_row("货款直投千川", "-1.89", direction="出账"),
        ], model)
        from ledger.engine.classify import COL_MINOR

        assert out.get_column(COL_MINOR).to_list() == ["货款直投千川"]


class TestSettlementHeaderVariants:
    def test_unified_zhang_character_still_matches(self, tmp_path, model):
        """导出把「动帐流水号」写成「动账流水号」时仍应认成对账单。"""
        headers = ["动账流水号" if h == "动帐流水号" else h for h in DY_SETTLE]
        path = write_xlsx(tmp_path / "对账-抖音浅花涧节日装饰.xlsx", [
            headers,
            _dy_row("货款结算入账", "16.32", sub="123F00", order="123"),
        ])
        rec = recognize(path, model)
        assert rec[0].template_id == "douyin_settlement_v1", rec[0].reason
        result = ingest([path], model, [s.name for s in model.stores])
        hit = next(i for i in result.items if i.recognition and i.recognition.template_id)
        assert hit.recognition.template_id == "douyin_settlement_v1"


class TestTheInsuranceTableIsAccepted:
    """保费支出表本期只有表头，模板照留。"""

    HEADER = ["保险单号", "动账流水号", "关联子订单号", "摘要描述", "动账时间", "金额(元)"]

    def test_a_header_only_file_is_recognized_not_rejected(self, tmp_path, model):
        path = write_xlsx(tmp_path / "权益保险-抖音浅花涧.xlsx", [self.HEADER])
        result = ingest([path], model, [s.name for s in model.stores])
        item = result.items[0]
        assert item.recognition is not None, "认不出来的话交表的人要去处理一张空表"
        assert item.recognition.template_id == "insurance_douyin_v1"

    def test_it_hangs_on_the_sub_order_without_splitting(self, model):
        """规则是 sumifs(J表金额, J表关联子订单号, 子订单编号)，没有 countifs。"""
        metric = model.metric("insurance_cost_douyin").for_platform("douyin")
        assert metric is not None and metric.link is not None
        assert metric.link.key == "sub_order_id"
        assert metric.allocate is None, "有 countifs 才摊，这条没有"

    def test_the_row_lands_as_a_cost(self, tmp_path, model):
        path = write_xlsx(tmp_path / "权益保险-抖音浅花涧.xlsx", [
            self.HEADER,
            ["BX001", "LS001", "6953149768301877000A00", "运费险保费",
             "2026-05-20 10:00:00", "1.20"],
        ])
        result = ingest([path], model, [s.name for s in model.stores])
        frame = next(i.frame for i in result.items if i.frame is not None)
        assert frame.get_column("total_cost").to_list() == [1.20]
        assert frame.get_column("sub_order_id").to_list() == ["6953149768301877000A00"]


class TestTheEntityNameIsNotBoundAsAStoreName:
    """商户主体名称不能绑成店名，那一列写的是主体。"""

    def test_the_template_does_not_bind_it(self, model):
        template = model.template("douyin_settlement_v1")
        assert "store_name" not in {b.role for b in template.bindings}, (
            "绑成店名会让挂不上订单的 1,606 行落进一个不存在的店期，"
            "既不进损益表也不进未归属清单"
        )
