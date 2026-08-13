"""补一份聚水潭把空成本填上。

淘宝店的聚水潭导出里有 385 行成本价是空的：补差价商品，以及被合并、被拆分之后
成本落到另一张单子上的行。业务的做法是再导一份（他们叫 K 表）交上来，结构完全一样。

这条测试盯着两件事，两件都是「不报错但算错钱」那一类：

1. 两份表里重合的行只能算一次。第二份万一导成了全量而不是增量，直接拼接
   会让商品成本翻倍——账上只表现为利润凭空少一半。
2. 一份表自己内部完全相同的行不能当重复删掉。同一单同一商品分几批出库，
   导出来就是一模一样的两行，那是真发生了两次。删掉的话，商品成本会因为
   「今天多传了一份补充导出」而变小。
"""

from __future__ import annotations

import polars as pl
import pytest
from conftest import MODELS, write_xlsx

from ledger.engine.runtime import ingest
from ledger.model.loader import load_model

HEADER = [
    "内部订单号", "线上订单号", "店铺名称", "下单时间", "状态", "订单类型",
    "线上子订单编号", "原始线上订单号", "商品编码", "商品名称", "数量", "成本价", "总成本",
]
TITLE = ["名称：聚水潭成本"]


def _row(sub: str, sku: str, qty, cost, *, total=None, state="已发货", kind="普通订单"):
    return [
        "14792373", sub, "淘宝喜必顺", "2026-05-08 10:00:00", state, kind,
        sub, sub, sku, "喜字贴", qty, cost, total if total is not None else "",
    ]


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


def _cost(frames: list[pl.DataFrame]) -> float:
    """把各份表的成本价×数量加起来。指标真正算的就是这个式子。"""
    total = 0.0
    for fr in frames:
        got = fr.select((pl.col("unit_cost") * pl.col("quantity")).sum()).item()
        total += got or 0.0
    return round(total, 4)


def _frames(paths, model) -> list[pl.DataFrame]:
    result = ingest(paths, model, ["淘宝喜必顺"])
    out = [i.frame for i in result.items if i.frame is not None]
    assert len(out) == len(paths), "有文件没被认出来是聚水潭成本表"
    return out


class TestSecondExportFillsTheBlanks:
    def test_blank_cost_row_contributes_nothing(self, tmp_path, model):
        """成本价空着的行算 0，不能从别的列里凑一个数出来。"""
        path = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER,
            _row("330001", "HSC001", 2, 5.25),
            _row("330002", "HSC002", 1, ""),
        ])
        assert _cost(_frames([path], model)) == 10.5

    def test_a_code_in_the_cost_column_is_not_a_number(self, tmp_path, model):
        """成本价那格填成了商品编码。真实数据里有三行是这样。

        从 `HSC25016` 里抠出 25016 当成本价，一行就是两万五，比整张表一天的成本
        还多。留空、说一声，等 K 表来补。
        """
        path = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER,
            _row("330001", "HSC001", 2, 5.25),
            _row("330002", "HSC25016", 18, "HSC25016"),
        ])
        result = ingest([path], model, ["淘宝喜必顺"])
        item = next(i for i in result.items if i.frame is not None)
        assert _cost([item.frame]) == 10.5
        assert any("不是数" in n for n in item.notes), "填错的格子要留痕，不能悄悄当空"

    def test_topup_export_adds_the_missing_cost(self, tmp_path, model):
        """K 表补上空成本那单，两份加起来是完整成本。"""
        first = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER,
            _row("330001", "HSC001", 2, 5.25),
            _row("330002", "HSC002", 1, ""),
        ])
        second = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺-补.xlsx", [
            TITLE, HEADER,
            _row("330002", "HSC002", 3, 4.0),
        ])
        assert _cost(_frames([first, second], model)) == 10.5 + 12.0

    def test_a_full_reexport_does_not_double_the_cost(self, tmp_path, model):
        """第二份导成了全量。重合的行只算一次，只有新增的那单加进来。"""
        rows = [_row("330001", "HSC001", 2, 5.25), _row("330002", "HSC002", 1, "")]
        first = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [TITLE, HEADER, *rows])
        second = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺-全量.xlsx", [
            TITLE, HEADER, *rows,
            _row("330002", "HSC002", 3, 4.0),
        ])
        frames = _frames([first, second], model)
        assert _cost(frames) == 10.5 + 12.0
        assert sum(f.height for f in frames) == 3

    def test_order_of_files_does_not_matter(self, tmp_path, model):
        rows = [_row("330001", "HSC001", 2, 5.25)]
        a = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [TITLE, HEADER, *rows])
        b = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺-补.xlsx", [
            TITLE, HEADER, *rows, _row("330003", "HSC003", 1, 7.0),
        ])
        assert _cost(_frames([a, b], model)) == _cost(_frames([b, a], model)) == 17.5

    def test_identical_rows_inside_one_file_both_count(self, tmp_path, model):
        """同一单同一商品分两批出库，导出来是一模一样的两行。两行都要算。

        实测淘宝那份里有 11 组共 25 行是这样。跨文件去重不能连它们一起吞掉——
        否则「多传了一份补充导出」这个动作会把原来那份的成本改小。
        """
        dup = _row("330001", "HSC001", 1, 5.25)
        first = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [TITLE, HEADER, dup, dup])
        alone = _cost(_frames([first], model))
        assert alone == 10.5

        second = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺-补.xlsx", [
            TITLE, HEADER, _row("330009", "HSC009", 1, 1.0),
        ])
        assert _cost(_frames([first, second], model)) == alone + 1.0

    def test_cancelled_orders_have_no_cost(self, tmp_path, model):
        """规则表写着「订单状态已取消的需删除对应行数据」。

        当前三家店的导出里一行都没有，写进来是为了哪天真出现时不用等人发现。
        """
        path = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER,
            _row("330001", "HSC001", 2, 5.25),
            _row("330002", "HSC002", 4, 3.0, state="已取消"),
        ])
        fr = _frames([path], model)[0]
        kept = fr.filter(pl.col("order_state") != "已取消")
        assert _cost([kept]) == 10.5
        assert _cost([fr]) == 22.5, "过滤是指标那层做的，归一化不该丢行"
