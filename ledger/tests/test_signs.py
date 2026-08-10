"""符号约定。

各平台对「支出」用什么符号毫无共识，而且约定往往写在列名的括号里：

    支付宝  支出金额（-元）   带减号，值是负数
    微信    支出金额(元)      不带减号，值是正数
    1688    已付金额(元)      不带减号，值是正数

引擎内部统一成「负数即减利润」，所以正数表示支出的列必须在归一时取反。
这个坑真实发生过：微信那列没取反，营销费用、销售退款、物流运费三项全部符号翻转，
损益表差了几千块，而且方向是错的——费用被当成收入加进了利润。
"""

from __future__ import annotations

from ledger.engine.normalize import normalize
from ledger.engine.types import FileRef, RawRow, RawTable
from ledger.model.schema import ColumnBinding, Template


def _table(headers: list[str], rows: list[list]) -> RawTable:
    return RawTable(
        ref=FileRef(sha256="x", filename="t.xlsx", sheet="s"),
        headers=headers,
        rows=[RawRow(row_no=i + 2, cells=tuple(r)) for i, r in enumerate(rows)],
    )


def _template(bindings: list[ColumnBinding]) -> Template:
    return Template(
        id="t", source="settlement", match_columns=[b.columns[0] for b in bindings],
        bindings=bindings,
    )


class TestNegate:
    def test_positive_outgo_becomes_negative(self):
        """微信把支出写成正数，取反后才和支付宝、和损益表口径一致。"""
        table = _table(["收入金额(元)", "支出金额(元)"], [[0, 3.5], [10, 0]])
        tpl = _template([
            ColumnBinding(role="income", columns=["收入金额(元)"]),
            ColumnBinding(role="outgo", columns=["支出金额(元)"], negate=True),
        ])
        frame, notes = normalize(table, tpl)
        assert frame.get_column("outgo").to_list() == [-3.5, 0.0]
        assert frame.get_column("income").to_list() == [0.0, 10.0]
        assert any("取反" in n for n in notes), "取反是会改变金额的操作，必须留下记录"

    def test_already_negative_stays(self):
        """支付宝那列本来就是负数，不该再取反。"""
        table = _table(["支出金额（-元）"], [[-3.5]])
        tpl = _template([ColumnBinding(role="outgo", columns=["支出金额（-元）"])])
        frame, _ = normalize(table, tpl)
        assert frame.get_column("outgo").to_list() == [-3.5]

    def test_negate_makes_amounts_numeric(self):
        """取反的前提是先认成数字。

        income / outgo 这两个角色名里没有 amount、fee 之类的词，早先没被
        当成数值列，取反就无从下手。
        """
        table = _table(["支出金额(元)"], [["3.50"], ["1,200.00"]])
        tpl = _template([ColumnBinding(role="outgo", columns=["支出金额(元)"], negate=True)])
        frame, _ = normalize(table, tpl)
        assert frame.get_column("outgo").to_list() == [-3.5, -1200.0]


class TestTotalRows:
    """表底的合计行不能当数据。

    真实场景：这些表底部常带一行合计。摄进去的话所有金额正好翻倍——
    实测淘宝的发货运费列合计 5,649.20 就是真实值 2,824.60 的两倍。
    """

    def test_drops_total_row(self):
        table = _table(
            ["订单号", "金额"],
            [["A001", 10.0], ["A002", 20.0], ["合计", 30.0]],
        )
        tpl = Template(
            id="t", source="order_detail", match_columns=["订单号", "金额"],
            bindings=[
                ColumnBinding(role="order_id", columns=["订单号"]),
                ColumnBinding(role="total_cost", columns=["金额"]),
            ],
            total_row_marker="order_id",
        )
        frame, notes = normalize(table, tpl)
        assert frame.get_column("total_cost").to_list() == [10.0, 20.0]
        assert any("合计" in n for n in notes)

    def test_blank_key_row_also_dropped(self):
        """合计行常常连「合计」二字都不写，只是键为空。"""
        table = _table(["订单号", "金额"], [["A001", 10.0], ["", 10.0]])
        tpl = Template(
            id="t", source="order_detail", match_columns=["订单号", "金额"],
            bindings=[
                ColumnBinding(role="order_id", columns=["订单号"]),
                ColumnBinding(role="total_cost", columns=["金额"]),
            ],
            total_row_marker="order_id",
        )
        frame, _ = normalize(table, tpl)
        assert frame.get_column("total_cost").to_list() == [10.0]


class TestOccurrence:
    """重复列名按位置取。

    真实场景：1688 订单明细有两列都叫「订单号」。
    """

    def test_picks_by_position(self):
        table = _table(["订单号", "订单号"], [["合并列", "展开列"]])
        tpl = _template([ColumnBinding(role="order_id", columns=["订单号"], occurrence=1)])
        frame, notes = normalize(table, tpl)
        assert frame.get_column("order_id").to_list() == ["展开列"]
        assert any("重复" in n for n in notes)
