"""未归类科目的行数和金额必须是真的。

这两个数字是用户判断"要不要管"的唯一依据，报错了比不报更糟。实测两处都错：

  金额  只取了取值表达式的第一个角色。支付宝把一笔钱拆成收入、支出两栏，
        余利宝申购那 88 行的钱全在支出栏，报出来是 0 元——看着像不用管的
        零头，实际是 81.30 万的资金划转。
  行数  一张对账表被七个科目各归类一遍，同一行数了七次，88 行报成 528 行。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.classify import classify
from ledger.engine.types import ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, ClassifyReport
from ledger.model.schema import Model


def _model() -> Model:
    return Model(id="t", name="测试模型")


def _rows(n: int) -> dict:
    return {
        ANCHOR_SHA: ["sha"] * n,
        ANCHOR_SHEET: ["Sheet1"] * n,
        ANCHOR_ROW: [str(i) for i in range(n)],
    }


class TestAmountUsesWholeExpression:
    def test_amount_column_can_be_a_computed_net(self):
        """运行时先按取值表达式算出每行净额，再交给归类，所以两栏的钱都在。"""
        frame = pl.DataFrame({
            "subject": ["余利宝-基金申购"] * 2,
            "income": [0.0, 0.0],
            "outgo": [-800000.0, -13000.0],
            **_rows(2),
        }).with_columns((pl.col("income") + pl.col("outgo")).alias("__row_amount__"))
        _, report = classify(frame, _model(), "taobao", "__row_amount__")
        count, amount = report.unmatched["余利宝-基金申购"]
        assert count == 2
        assert amount == -813000.0

    def test_first_role_alone_would_have_reported_zero(self):
        """钉住修之前的错法：只看收入栏，81 万变成 0 元。"""
        frame = pl.DataFrame({
            "subject": ["余利宝-基金申购"],
            "income": [0.0],
            "outgo": [-800000.0],
            **_rows(1),
        })
        _, report = classify(frame, _model(), "taobao", "income")
        assert report.unmatched["余利宝-基金申购"][1] == 0.0


class TestOneRowCountedOnce:
    def test_same_physical_row_across_metrics(self):
        """七个科目从同一张对账表出数，同一行只能算一次。"""
        frame = pl.DataFrame({
            "subject": ["不认识的费项"] * 3,
            "__row_amount__": [-100.0, -200.0, -300.0],
            **_rows(3),
        })
        reports = []
        for _ in range(7):
            _, r = classify(frame, _model(), "taobao", "__row_amount__")
            reports.append(r)
        merged = ClassifyReport()
        for r in reports:
            for label, rows in r.unmatched_rows.items():
                merged.unmatched_rows.setdefault(label, {}).update(rows)
        count, amount = merged.unmatched["不认识的费项"]
        assert count == 3, "同一行被七个科目各报一次，不去重就是 21 行"
        assert amount == -600.0

    def test_different_rows_still_add_up(self):
        """去重不能把不同的行也吞掉。"""
        a = pl.DataFrame({
            "subject": ["不认识的费项"],
            "__row_amount__": [-100.0],
            ANCHOR_SHA: ["sha1"], ANCHOR_SHEET: ["S"], ANCHOR_ROW: ["1"],
        })
        b = pl.DataFrame({
            "subject": ["不认识的费项"],
            "__row_amount__": [-50.0],
            ANCHOR_SHA: ["sha2"], ANCHOR_SHEET: ["S"], ANCHOR_ROW: ["1"],
        })
        merged = ClassifyReport()
        for frame in (a, b):
            _, r = classify(frame, _model(), "taobao", "__row_amount__")
            for label, rows in r.unmatched_rows.items():
                merged.unmatched_rows.setdefault(label, {}).update(rows)
        assert merged.unmatched["不认识的费项"] == (2, -150.0)

    def test_falls_back_to_row_position_without_anchors(self):
        """没有锚点列时按行序号认。同一张表跑多遍，行顺序是一样的。"""
        frame = pl.DataFrame({"subject": ["不认识的费项"] * 2, "__row_amount__": [-1.0, -2.0]})
        merged = ClassifyReport()
        for _ in range(3):
            _, r = classify(frame, _model(), "taobao", "__row_amount__")
            for label, rows in r.unmatched_rows.items():
                merged.unmatched_rows.setdefault(label, {}).update(rows)
        assert merged.unmatched["不认识的费项"] == (2, -3.0)
