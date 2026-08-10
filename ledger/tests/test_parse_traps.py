"""解析层踩过的坑。

每条测试对应一个在真实数据上确实出过问题的场景，注释里写清是哪一个——
不然半年后没人知道这条测试在防什么，改坏了也不敢删。
"""

from __future__ import annotations

from pathlib import Path

from ledger.engine.parse import ParseOptions, parse


def _cells(path: Path, *, header_row: int = 0, sheet: str | None = None):
    tables = [t for t in parse(path, ParseOptions(header_row=header_row)) if t.headers]
    if sheet:
        tables = [t for t in tables if t.ref.sheet == sheet]
    table = tables[0]
    return table, [list(r.cells) for r in table.rows]


class TestMergedCells:
    """合并单元格要还原成每行都有值。

    真实场景：1688 的订单明细里一个订单占若干行，订单号和创建时间都是合并的，
    3,086 行里只有 1,516 行有值。不还原的话一半的行会因为订单号为空被当成合计行
    丢掉，剩下的因为没有日期落不进账期。制表的人知道这个坑，手工把订单号那列
    展开填充后另存了一份贴在旁边——这件事该引擎做。
    """

    def test_fills_down_merged_value(self, tmp_xlsx):
        path = tmp_xlsx(
            [
                ["订单号", "商品", "金额"],
                ["A001", "气球", 10],
                [None, "彩带", 20],
                [None, "灯串", 30],
            ],
            merges=["A2:A4"],
        )
        _, rows = _cells(path)
        assert [r[0] for r in rows] == ["A001", "A001", "A001"]
        # 只填合并区域，别的列不受影响。
        assert [r[1] for r in rows] == ["气球", "彩带", "灯串"]

    def test_leaves_genuine_blanks_alone(self, tmp_xlsx):
        """没有合并区域的空格是真的空，不能顺手向下填。

        运费表那 30 万行里也有大量空格，那是真缺值。要是把「某列很多空格」
        直接当成合并处理，就会凭空造出数据来。填什么以 xlsx 里记的合并区域为准。
        """
        path = tmp_xlsx(
            [
                ["订单号", "备注"],
                ["A001", "加急"],
                ["A002", None],
                ["A003", None],
            ]
        )
        _, rows = _cells(path)
        assert [r[0] for r in rows] == ["A001", "A002", "A003"]
        # 断言「仍然是空」而不是空值的具体形式，免得解析层换个表示法就误报。
        assert rows[0][1] == "加急"
        assert all(r[1] in (None, "") for r in rows[1:])

    def test_horizontal_merge(self, tmp_xlsx):
        """横向合并也要铺开。表头上方的说明行常常是横向合并的。"""
        path = tmp_xlsx(
            [
                ["店铺", "平台", "金额"],
                ["星泽", None, 10],
            ],
            merges=["A2:B2"],
        )
        _, rows = _cells(path)
        assert rows[0][:2] == ["星泽", "星泽"]


class TestHeaderLocation:
    """表头不在第一行。

    真实场景：交上来的每张表第一行都是制表人写的说明，形如
    「名称：对账 / 路径：千牛——财务——总览——导出 / 注：以下字段为原始表单无公式」，
    真正的表头在第二行。
    """

    def test_header_row_option(self, tmp_xlsx):
        path = tmp_xlsx(
            [
                ["名称：订单明细\n路径：交易履约——已卖出货品——导出", None, None],
                ["订单号", "商品", "金额"],
                ["A001", "气球", 10],
            ]
        )
        table, rows = _cells(path, header_row=1)
        assert table.headers[:3] == ["订单号", "商品", "金额"]
        assert rows == [["A001", "气球", 10]]


class TestControlTotals:
    """文件尾部自带的控制总数要读出来。

    真实场景：支付宝账务明细的 CSV 尾部写着
        #支出合计：75171笔，共-540182.61元
        #收入合计：18003笔，共536923.62元
    这是文件自己声明的正确答案，解析完拿它对一遍就知道有没有漏读行、
    数值有没有解错。比赌解析库够强靠谱得多，因为它是逐文件的实证。
    """

    def test_extracts_from_footer(self, tmp_csv):
        path = tmp_csv(
            "#支付宝账务明细\n"
            "交易号,业务基础订单号,收入金额,支出金额,备注\n"
            "T1,O1,10.00,,货款\n"
            "T2,O2,,-3.00,服务费\n"
            "#收入合计：1笔，共10.00元\n"
            "#支出合计：1笔，共-3.00元\n"
        )
        table, rows = _cells(path)
        assert len(rows) == 2, "注释行和控制总数行不能当成数据"
        labels = {c.label: (c.count, c.amount) for c in table.controls}
        assert labels == {"收入合计": (1, 10.0), "支出合计": (1, -3.0)}

    def test_no_controls_is_not_a_failure(self, tmp_csv):
        """拿不到控制总数不代表文件有问题，只是这层保障用不上。"""
        path = tmp_csv("订单号,金额\nA001,10\n")
        table, rows = _cells(path)
        assert table.controls == []
        assert len(rows) == 1


class TestDuplicateHeaders:
    """重复列名靠位置区分，不能去重。

    真实场景：1688 订单明细有两列都叫「订单号」，第一列是平台导出的合并单元格，
    第二列是人工展开填充的副本；淘宝的账单汇总表里整组列名出现两遍。
    解析层保留原始顺序和重复，由模板用 occurrence 指定取第几个。
    """

    def test_keeps_both(self, tmp_xlsx):
        path = tmp_xlsx(
            [
                ["订单号", "订单号", "金额"],
                ["A001", "A001", 10],
            ]
        )
        table, rows = _cells(path)
        assert table.headers[:3] == ["订单号", "订单号", "金额"]
        assert len(rows[0]) >= 3
