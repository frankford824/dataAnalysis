"""证据链：报表上的每个数字，点开要能落到源文件的行上，而且加起来等于那个数字。

这套系统和一张普通报表的唯一区别就在这里。数字点不开，或者点开加起来对不上，
人就只能回去用 Excel 手工核——那套流程本来就是要被替掉的东西。

这个文件盯两件事，它们各自出过一次事，表现完全不同：

归属口径
    源记录原先按「表格自己报的店铺名」归店。聚水潭把这家店写成「喜必顺旗舰店」，
    店铺档案里叫「淘宝喜必顺」，于是这批行在每个账期里都对不上号、一条都不留档。
    表现是商品成本、代发成本、补发成本、客服打款、本金佣金五项报表上有数、
    点开一片空白——十七万的商品成本没有一行证据。

进账标记
    源表里的行不是都进损益表。运费表是全公司的运单，淘宝那家店 29.9 万行里只有
    1.4 万行挂得上自己的订单。不区分的话，点开「发货运费」看到的是 -550,944，
    而报表上写着 -20,294，人只会认为报表算错了。
"""

from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from ledger.engine.calculate import evaluate_metric
from ledger.engine.link import LINK_KEY, LINKED
from ledger.engine.runtime import _mark_counted
from ledger.engine.types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET
from ledger.model.schema import LinkRule, Metric, SourceContract, Template, ValueExpr


def _metric(**kw) -> Metric:
    kw.setdefault("id", "m")
    kw.setdefault("name", "指标")
    kw.setdefault("source", "s")
    kw.setdefault("value", ValueExpr(op="sum", of=("amount",)))
    return Metric(**kw)


def _template() -> Template:
    return Template(id="t", source="s", match_columns=("amount",))


def _frame(rows: list[dict]) -> pl.DataFrame:
    base = {
        "amount": 0.0,
        ANCHOR_SHA: "sha", ANCHOR_FILE: "f.xlsx", ANCHOR_SHEET: "Sheet1", ANCHOR_ROW: 2,
    }
    return pl.DataFrame([{**base, **r} for r in rows])


# --------------------------------------------------------------------------- #
# 一行记录算在谁头上
# --------------------------------------------------------------------------- #


class TestWhoTheRowBelongsTo:
    """钱落在谁的订单上是确定的，表格自己报的店名不是。"""

    def test_the_order_decides_not_the_spreadsheet(self):
        """聚水潭报「喜必顺旗舰店」，店铺档案里叫「淘宝喜必顺」。

        信表格的话这批行在每个账期里都对不上号，一条都不留档——十七万的商品成本
        点开一片空白。
        """
        frame = _frame([{"amount": -10.0, "store_name": "喜必顺旗舰店",
                         "__spine_store__": "淘宝喜必顺"}])
        facts, _ = evaluate_metric(frame, _metric(), _template())
        assert facts.get_column("store").to_list() == ["淘宝喜必顺"]

    def test_falls_back_to_what_the_sheet_says(self):
        """挂不上订单的行没有归属可跟，只能信表格。总比记成「未知店铺」强。"""
        frame = _frame([{"amount": -10.0, "store_name": "喜必顺旗舰店"}])
        facts, _ = evaluate_metric(frame, _metric(), _template())
        assert facts.get_column("store").to_list() == ["喜必顺旗舰店"]

    def test_falls_back_to_the_file_name_last(self):
        """表格连店名列都没有时，用上传时认出来的那家店。"""
        frame = _frame([{"amount": -10.0}])
        facts, _ = evaluate_metric(frame, _metric(), _template(), "淘宝喜必顺")
        assert facts.get_column("store").to_list() == ["淘宝喜必顺"]

    def test_the_order_decides_the_period_too(self):
        """同一个理由：钱算进哪个月，跟着它落在的那张订单走。"""
        frame = _frame([{"amount": -10.0, "pay_date": datetime(2026, 4, 30),
                         "__spine_period__": "2026-05"}])
        facts, _ = evaluate_metric(frame, _metric(time_basis="pay_date"), _template())
        assert facts.get_column("period").to_list() == ["2026-05"]


# --------------------------------------------------------------------------- #
# 一行记录进没进账
# --------------------------------------------------------------------------- #


def _facts(rows: list[dict]) -> pl.DataFrame:
    base = {
        "metric_id": "m", "source_id": "s", "template_id": "t",
        "store": "甲店", "period": "2026-05", "grain": "order",
        "link_key": None, "linked": False, "amount": 0.0,
        "subject": None, "major": None, "minor": None,
        "file_sha": "sha", "file_name": "f.xlsx", "sheet": "Sheet1", "row_no": 2,
    }
    return pl.DataFrame([{**base, **r} for r in rows],
                        schema_overrides={"link_key": pl.Utf8})


def _spine(rows: list[dict]) -> pl.DataFrame:
    base = {"metric_id": "m", "source_id": "s", "store": "甲店",
            "period": "2026-05", "link_key": "", "amount": 0.0,
            "factor": 1.0, "spine_row": 0}
    return pl.DataFrame([{**base, **r} for r in rows],
                        schema_overrides={"spine_row": pl.UInt32})


class TestWhetherTheRowMadeItIntoTheStatement:
    def test_a_row_that_landed_on_an_order_is_counted(self):
        out = _mark_counted(_facts([{"link_key": "A1", "amount": -10.0}]),
                            _spine([{"link_key": "A1", "amount": -10.0}]))
        assert out.get_column("counted").to_list() == [True]
        assert out.get_column("contribution").to_list() == [pytest.approx(-10.0)]

    def test_a_row_with_no_order_number_is_not_counted(self):
        """挂不上任何订单的钱不进损益表。它照样留档，只是标出来。"""
        out = _mark_counted(_facts([{"amount": -10.0}]),
                            _spine([{"link_key": "A1", "amount": -1.0}]))
        assert out.get_column("counted").to_list() == [False]
        assert out.get_column("contribution").to_list() == [0.0]

    def test_an_order_belonging_to_another_store_is_not_counted(self):
        """运费表是全公司的运单。这家店 29.9 万行里只认领得了 1.4 万行，
        剩下的属于别的店铺——它们不进这家店的账。"""
        out = _mark_counted(_facts([{"link_key": "别家的单", "amount": -530.0}]),
                            _spine([{"link_key": "A1", "amount": -1.0}]))
        assert out.get_column("counted").to_list() == [False]

    def test_the_amount_is_scaled_by_how_it_was_split(self):
        """一笔主订单级的钱按比例摊到子订单上。原始金额不等于进账金额，
        差的就是这个比例——不折算的话下钻永远比报表多一截。"""
        out = _mark_counted(
            _facts([{"link_key": "A1", "amount": -100.0}]),
            _spine([{"link_key": "A1", "factor": 0.3}, {"link_key": "A1", "factor": 0.45}]),
        )
        assert out.get_column("contribution").to_list() == [pytest.approx(-75.0)]

    def test_rows_add_up_to_the_statement(self):
        """这是这两列存在的全部意义：逐行加起来等于报表上那个数，不是差不多。"""
        facts = _facts([
            {"link_key": "A1", "amount": -100.0},
            {"link_key": "A2", "amount": -60.0},
            {"link_key": "别家的单", "amount": -530.0},
            {"amount": -7.0},
        ])
        spine = _spine([
            {"link_key": "A1", "factor": 0.5}, {"link_key": "A1", "factor": 0.5},
            {"link_key": "A2", "factor": 1.0},
        ])
        out = _mark_counted(facts, spine)
        assert out.get_column("contribution").sum() == pytest.approx(-160.0)

    def test_the_same_key_in_another_period_does_not_count(self):
        """账期也要对上，否则上个月的钱会算进这个月。"""
        out = _mark_counted(_facts([{"link_key": "A1", "amount": -10.0}]),
                            _spine([{"link_key": "A1", "period": "2026-04"}]))
        assert out.get_column("counted").to_list() == [False]

    def test_the_same_key_in_another_store_does_not_count(self):
        out = _mark_counted(_facts([{"link_key": "A1", "amount": -10.0}]),
                            _spine([{"link_key": "A1", "store": "乙店"}]))
        assert out.get_column("counted").to_list() == [False]

    def test_another_metric_does_not_lend_its_keys(self):
        """五个指标读同一张对账表。按键匹配而不按指标匹配的话，
        被别的指标认领的行会跟着一起标成进账。"""
        out = _mark_counted(_facts([{"metric_id": "别的指标", "link_key": "A1",
                                     "amount": -10.0}]),
                            _spine([{"link_key": "A1"}]))
        assert out.get_column("counted").to_list() == [False]

    def test_nothing_landed_on_the_spine_at_all(self):
        """一条都没挂上时也要有这两列，否则下钻得为它写一套分支。"""
        out = _mark_counted(_facts([{"amount": -10.0}]), _spine([]).clear())
        assert out.get_column("counted").to_list() == [False]
        assert "contribution" in out.columns

    def test_marking_twice_does_not_stack(self):
        once = _mark_counted(_facts([{"link_key": "A1", "amount": -10.0}]),
                             _spine([{"link_key": "A1"}]))
        twice = _mark_counted(once, _spine([{"link_key": "A1"}]))
        assert twice.columns == once.columns


# --------------------------------------------------------------------------- #
# 端到端：留档的行能不能还原报表数字
# --------------------------------------------------------------------------- #


def test_the_archived_rows_reproduce_the_number(tmp_path):
    """留档一趟再读回来，进账金额还是要加得出报表数字。

    parquet 存的是 f64，Decimal 那一路的精度到这里就断了。所以口径是「分」，
    不是「差不多」。
    """
    facts = _mark_counted(
        _facts([{"link_key": "A1", "amount": -33.33},
                {"link_key": "A2", "amount": -66.67},
                {"link_key": "别家的单", "amount": -530.0}]),
        _spine([{"link_key": "A1", "factor": 1.0}, {"link_key": "A2", "factor": 1.0}]),
    )
    path = tmp_path / "facts.parquet"
    facts.write_parquet(path)
    back = pl.read_parquet(path)
    assert round(back.filter(pl.col("counted")).get_column("contribution").sum(), 2) == -100.0


def test_link_is_declared_on_the_metric_for_projection():
    """`_mark_counted` 只认脊柱事实里出现过的键，而脊柱事实只有声明了挂钩的指标才有。

    没声明挂钩的指标不产出脊柱事实，它的钱在报表上也是 0——两边一致，
    所以这里不该给它标进账。
    """
    metric = _metric(link=LinkRule(to="order.order_id", key="order_id", grain="order"))
    assert metric.link is not None and metric.link.to


def test_source_contract_is_not_involved():
    """归属和进账都由数据本身决定，不需要在契约上多配一个字段。

    多一个字段就多一处会配错的地方，而配错的表现是静默少算钱。
    """
    contract = SourceContract(id="s", name="对账", owner_role="shop_owner",
                              cadence="monthly")
    assert not hasattr(contract, "counted")
