"""京东和拼多多这两个平台的规则。

这两家是第四、第五个平台，接的时候只加了模型数据加一个引擎原语（归类之后的改判）。
这里盯的是那几处「不报错但算错钱」的地方：

1. 京东对账表里，费项是交易收款而收支方向是支出的行要改判成交易退款。不改判的话
   这 257 行、-6,173.83 元会当成负的销售收入，收入和退款两行同时不对。
2. 两个平台的对账表列名和淘宝那张撞了一半，不能互相认错模板。
3. 拼多多推广表表底那行总计必须丢掉，不丢的话推广费正好翻倍。
4. 京东订单明细没有运单号列，商品成本的覆盖率分母不能沿用「已发货」那条。
"""

from __future__ import annotations

import pytest
from conftest import MODELS, write_xlsx

from ledger.engine.classify import COL_MAJOR, COL_MINOR, classify
from ledger.engine.normalize import normalize
from ledger.engine.parse import parse
from ledger.engine.recognize import recognize
from ledger.engine.runtime import ingest
from ledger.model.loader import load_model


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


# 京东对账表的真实表头，一个字都不能改。
JD_SETTLE = [
    "订单编号", "父单号", "订单状态", "订单下单时间", "订单完成时间", "售后服务单号",
    "售后退款时间", "商品编号", "商品名称", "商品数量", "扣点类型", "佣金比例",
    "费用名称", "应结金额", "币种", "收支方向", "结算状态", "预计结算时间",
    "账单生成时间", "到账时间", "商户订单号", "资金动账备注",
]


def _jd_row(order, subject, amount, direction, status="已结算"):
    return [
        order, "3525498018681355", "完成", "2026-05-10 14:41:34", "", "", "",
        "10170858471951", "皇莉诗手持小横幅", "1", "基础扣点", "0.05",
        subject, amount, "CNY", direction, status, "2026-07-08 17:11:38",
        "20260708", "2026-07-09 12:08:53", "202607090000011332601610021", "",
    ]


def _intake(tmp_path, name, rows, model):
    """走真实的摄取流程。表头在第 2 行，第 1 行是说明文字，和真文件一样。

    不直接调 recognize：它只看第一行，而这批文件的表头在第二行——摄取流程会
    逐行试，这里要测的正是那条路走不走得通。
    """
    path = write_xlsx(tmp_path / name, [["名称：说明"], *rows])
    result = ingest([path], model, [s.name for s in model.stores])
    got = [i for i in result.items if i.rows]
    assert len(got) == 1, [i.error for i in result.items]
    return got[0]


def _classified(tmp_path, rows, model, platform):
    """把一张对账表跑到归类之后，返回归一帧。"""
    name = f"对账-{'京东皇莉诗' if platform == 'jd' else 'pdd快乐节庆'}.xlsx"
    item = _intake(tmp_path, name, rows, model)
    assert item.frame is not None, item.error
    out, _ = classify(item.frame, model, platform, template=item.template)
    return out, item.template


class TestJdReceiptPaidOutIsARefund:
    """费项=交易收款 且 收支方向=支出 → 交易退款。京东规则表第二步。"""

    def test_outgoing_receipt_becomes_a_refund(self, tmp_path, model):
        out, _ = _classified(tmp_path, [
            JD_SETTLE,
            _jd_row("3553498017448001", "货款", "36.80", "收入"),
            _jd_row("3553498017448002", "货款", "-13.80", "支出"),
            _jd_row("3553498017448003", "代收配送费", "-6.00", "支出"),
        ], model, "jd")
        assert out.get_column(COL_MAJOR).to_list() == [
            "trade_receipt", "trade_refund", "trade_refund",
        ]

    def test_the_platform_subject_name_survives_the_rewrite(self, tmp_path, model):
        """改判只动大类，细项还是平台自己那个科目名。

        细项是界面上给人看的那一栏。改判时把它一起改成内部代号的话，
        人看到的是自己表里根本不存在的词。
        """
        out, _ = _classified(tmp_path, [
            JD_SETTLE, _jd_row("3553498017448002", "货款", "-13.80", "支出"),
        ], model, "jd")
        assert out.get_column(COL_MINOR).to_list() == ["货款"]

    def test_fees_paid_out_are_not_touched(self, tmp_path, model):
        """支出方向的服务费本来就是服务费，别顺手改判了。"""
        out, _ = _classified(tmp_path, [
            JD_SETTLE,
            _jd_row("3553498017448004", "佣金", "-1.84", "支出"),
            _jd_row("3553498017448005", "运费保险服务费", "-0.86", "支出"),
        ], model, "jd")
        assert set(out.get_column(COL_MAJOR).to_list()) == {"software_fee"}

    def test_the_two_subjects_the_rule_sheet_reassigns(self, tmp_path, model):
        """规则表写「售后卖家赔付费、返利框架费的值填充为平台技术服务费」。"""
        out, _ = _classified(tmp_path, [
            JD_SETTLE,
            _jd_row("3553498017448006", "售后卖家赔付费", "-23.90", "支出"),
            _jd_row("3553498017448007", "返利框架费", "-3.92", "支出"),
        ], model, "jd")
        assert set(out.get_column(COL_MAJOR).to_list()) == {"software_fee"}

    def test_a_rewrite_needs_both_conditions(self):
        """改判规则必须同时给「原来是哪个大类」和「还要满足什么」。

        少一个条件的改判会把一整个大类无条件搬走，而搬走之后两个科目的数
        看着都还像那么回事。
        """
        from pydantic import ValidationError

        from ledger.model.schema import Reclassify

        with pytest.raises(ValidationError):
            Reclassify(when_major="trade_receipt", major="trade_refund")


class TestTheTwoSettlementTablesDoNotGetConfused:
    """拼多多对账表和淘宝支付宝账务明细的列名撞了一半。"""

    def test_pdd_settlement_is_not_read_as_alipay(self, tmp_path, model):
        item = _intake(tmp_path, "对账-pdd快乐节庆.xlsx", [
            ["商户订单号", "发生时间", "收入金额（+元）", "支出金额（-元）",
             "账务类型", "备注", "业务描述", "费项", "金额"],
            ["260531-662941284701219", "2026-05-31 23:54:45", "0.09", "0",
             "技术服务费", "基础技术服务费返还", "0030002|技术服务费-基础技术服务费",
             "软件服务费", "0.09"],
        ], model)
        assert item.recognition.template_id == "pdd_settlement_v1"

    def test_alipay_settlement_is_still_read_as_alipay(self, tmp_path, model):
        """反过来也要成立：给拼多多加模板不能把淘宝那张抢走。"""
        item = _intake(tmp_path, "对账-淘宝喜必顺.xlsx", [
            ["账务流水号", "业务流水号", "发生时间", "收入金额（+元）", "支出金额（-元）",
             "账户余额（元）", "业务类型", "业务描述", "业务基础订单号", "商户订单号", "备注"],
            ["20260531001", "T200P123456789012", "2026-05-31 23:54:45", "146.92", "0",
             "1000.00", "交易分账", "订单收入", "3553498017448001",
             "T200P123456789012", ""],
        ], model)
        assert item.recognition.template_id == "taobao_settlement_alipay_v1"

    def test_pdd_subject_codes_hit_the_dictionary(self, tmp_path, model):
        """业务描述是带编号的全称，字典里存的就是全称。"""
        out, _ = _classified(tmp_path, [
            ["商户订单号", "发生时间", "收入金额（+元）", "支出金额（-元）",
             "账务类型", "备注", "业务描述"],
            ["260531-662941284701219", "2026-05-31 23:54:45", "146.92", "0",
             "交易收入", "", "0010002|交易收入-订单收入"],
            ["260531-662941284701220", "2026-05-31 23:55:00", "0", "-27.50",
             "交易退款", "", "0020002|交易退款-订单退款"],
            ["260531-662941284701221", "2026-05-31 23:56:00", "0", "-8.14",
             "其他支出", "", "0140028|其他支出-合作费追回"],
        ], model, "pdd")
        assert out.get_column(COL_MAJOR).to_list() == [
            "trade_receipt", "trade_refund", "trade_compensation",
        ]

    def test_withdrawals_are_classified_but_never_reach_profit(self, model):
        """提现和给广告账户转钱不是经营损益。

        本期这两项合计 -99,600 元，是拼多多对账表流水的大头。混进利润的话
        这家店会从赚一万四变成亏八万五。防线是：字典给它们独立的大类，
        而没有任何指标消费那两个大类。
        """
        parked = {"withdrawal", "ad_topup", "deposit", "misc_payment"}
        eaten = {m.major for m in model.metrics if m.major}
        assert not (parked & eaten), "有指标开始吃这些大类了，钱会串进利润"
        assert parked <= {e.major for e in model.dictionary}, "字典里没这几个大类了"


class TestPddPromotionTotalRow:
    """表底那行总计不丢，推广费正好翻倍。"""

    HEADER = ["日期", "商品ID", "商品名称", "推广场景", "推广名称", "出价方式",
              "分组", "是否已删除", "成交花费(元)", "交易额(元)", "实际投产比",
              "总花费(元)", "曝光量", "点击量"]

    def _sheet(self, tmp_path, model):
        path = write_xlsx(tmp_path / "推广-pdd快乐节庆.xlsx", [
            ["名称：推广"],
            self.HEADER,
            ["2026-05-31", "754299529443", "接考横幅", "稳定成本推广", "接考横幅",
             "目标投产比：5.80", "", "", "102.12", "701.07", "6.87", "102.12",
             "7677", "624"],
            ["2026-05-30", "754299529443", "接考横幅", "稳定成本推广", "接考横幅",
             "目标投产比：5.80", "", "", "80.00", "500.00", "6.25", "80.00",
             "6000", "500"],
            ["总计", "-", "-", "-", "-", "-", "-", "-", "182.12", "1201.07",
             "6.59", "182.12", "13677", "1124"],
        ])
        return ingest([path], model, [s.name for s in model.stores])

    def test_the_total_row_is_dropped(self, tmp_path, model):
        result = self._sheet(tmp_path, model)
        frames = [i.frame for i in result.items if i.frame is not None]
        assert len(frames) == 1
        assert frames[0].height == 2
        assert frames[0].get_column("spend").sum() == pytest.approx(182.12)

    def test_a_total_that_disagrees_with_the_rows_is_reported(self, tmp_path, model):
        """总计大于明细之和是拼多多的全店托管，平台不给单个商品的花费。

        眼下不摊，但差额必须有人看得见——无声扔掉是最坏的选择。
        """
        path = write_xlsx(tmp_path / "推广-pdd快乐节庆.xlsx", [
            ["名称：推广"],
            self.HEADER,
            ["2026-05-31", "754299529443", "接考横幅", "稳定成本推广", "接考横幅",
             "目标投产比：5.80", "", "", "102.12", "701.07", "6.87", "102.12",
             "7677", "624"],
            ["总计", "-", "-", "-", "-", "-", "-", "-", "500.00", "1201.07",
             "6.59", "500.00", "13677", "1124"],
        ])
        result = ingest([path], model, [s.name for s in model.stores])
        notes = " ".join(n for i in result.items for n in i.notes)
        assert "合计行的 spend 说 500.00" in notes
        assert "397.88" in notes


class TestPddOrdersWithNoTimeAtAll:
    """成交时间整列空着的那 127 行。账期得从订单号里兜底，否则它们从损益表上消失。"""

    HEADER = ["订单成交时间", "商品", "订单号", "订单状态", "商品总价(元)", "邮费(元)",
              "店铺优惠折扣(元)", "平台优惠折扣(元)", "多多支付立减金额(元)",
              "用户实付金额(元)", "商家实收金额(元)", "商品数量(件)", "发货时间",
              "确认收货时间", "商品id", "商品规格", "样式ID", "商家编码-规格维度",
              "商家编码-商品维度", "商家备注", "售后状态", "快递单号", "快递公司"]

    def _row(self, when, order, tracking=""):
        return [when, "接考横幅", order, "已收货", "17.8", "0", "0", "0", "0",
                "17.8", "17.8", "1", "", "", "754299529443", "无规格", "", "",
                "", "", "无售后或售后取消", tracking, "申通快递"]

    def _dates(self, tmp_path, model, rows):
        item = _intake(tmp_path, "订单明细-pdd快乐节庆.xlsx", [self.HEADER, *rows], model)
        assert item.frame is not None, item.error
        return [str(d) for d in item.frame.get_column("order_date").to_list()]

    def test_a_blank_time_falls_back_to_the_order_number(self, tmp_path, model):
        got = self._dates(tmp_path, model, [
            self._row("", "260517-607198974342268"),
            self._row("", "260501-607198974342269"),
        ])
        assert got == ["2026-05-17", "2026-05-01"]

    def test_a_real_time_wins_over_the_order_number(self, tmp_path, model):
        """原列有值就用原列。兜底的依据比原列弱，拿它去覆盖等于用推断替换事实。

        真实数据里有 3 行两者不一致——临近午夜下的单，订单号是一天、成交时间是下一天。
        """
        got = self._dates(tmp_path, model, [
            self._row("2026-06-01 00:03:00", "260531-607198974342268"),
        ])
        assert got == ["2026-06-01"]

    def test_the_fallback_is_reported(self, tmp_path, model):
        """兜底了要说一声。悄悄补上的日期，等哪天订单号规则变了没人会发现。"""
        item = _intake(tmp_path, "订单明细-pdd快乐节庆.xlsx", [
            self.HEADER, self._row("", "260517-607198974342268"),
        ], model)
        assert any("日期从 order_id 里取" in n for n in item.notes), item.notes

    def test_an_order_number_without_a_date_stays_blank(self, tmp_path, model):
        """抠不出日期就还是空的，不能瞎填一个。"""
        got = self._dates(tmp_path, model, [self._row("", "A607198974342268")])
        assert got == ["None"]


class TestJdSpineHasNoWaybillColumn:
    """京东订单明细没有运单号那一列，两处口径要跟着变。"""

    def test_goods_cost_coverage_is_measured_by_order_state(self, model):
        """缺省那条「已发货才要求有成本」在京东恒不成立，分母会变成全部订单。

        后果不是报错，是覆盖率显示 79.9%、结账被拦住，而缺的那六百单
        压根没出过库——一条永远亮的红灯，看久了就没人看了。
        """
        jd = next(m for m in model.metrics if m.id == "goods_cost").for_platform("jd")
        assert jd is not None
        fields = {p.field for p in jd.expect}
        assert fields == {"order_state"}, "京东的覆盖率分母还在看运单号"
        assert jd.expect_label == "已出库"

    def test_the_spine_really_has_no_waybill(self, model):
        template = model.template("jd_order_detail_v1")
        roles = {b.role for b in template.bindings}
        assert "tracking_no" not in roles
        # 所以运费只能靠聚水潭那一级回查。规则链里那一条必须还在。
        freight = model.template("freight_v1")
        assert any(
            r.via and r.via.source == "order_cost" for r in freight.key_rules
        ), "聚水潭回查那条没了，京东的发货运费会整块挂不上"
