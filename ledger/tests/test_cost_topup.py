"""再导一份聚水潭（K 表）合进来。

业务把第二份聚水潭导出叫 K 表，文件名是「聚水潭成本（补充）-店名.xlsx」，
结构和第一份一模一样。原先以为它是来补空成本价的，2026-08 那批真表到手之后
核实不是：五家店的 K 表补上的空成本价是 0 行，它其实是同一段时间的第二次、
更全的导出（淘宝 05-01~05-13 那段，I 表 18,543 行、K 表 28,972 行）。
两份必须合并，合并的全部风险都在去重键上。

这里盯着四件事，每一件都是「不报错但算错钱」那一类：

1. 两份表里重合的行只能算一次。第二份万一导成了全量而不是增量，直接拼接
   会让商品成本翻倍——账上只表现为利润凭空少一半。
2. 同一条出库记录被重算过成本价，两份表里的成本价差几厘。成本价一旦进了
   去重键，这两行就成了两条记录，同一笔成本算两遍。拿 2026-08 那批真表两种键
   各算一遍，商品成本淘宝多算 23,705.28 元、五家店合计多算 33,916.96 元。
3. 一份表自己内部完全相同的行不能当重复删掉。同一单同一商品分几批出库，
   导出来就是一模一样的两行，那是真发生了两次。删掉的话，商品成本会因为
   「今天多传了一份补充导出」而变小。
4. 有一份只有表头没有数据行时，整批不能崩。
5. 两份表的表头不一定在同一行，按模板写死的行号重解会把成本整份丢掉。
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
        # 空表跳过。整列没有值时类型判成文本，乘不起来——指标那层也是先看空不空
        # 就返回（见 calculate.evaluate_metric），这里跟它保持一致。
        if fr.is_empty():
            continue
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

    def test_a_recosted_row_is_still_the_same_row(self, tmp_path, model):
        """两份表里同一条出库记录，成本价被聚水潭重算过。只能算一次。

        聚水潭的成本价是移动加权，两次导出之间会变：实测同一条记录
        （内部订单号、线上子订单编号、商品编码、数量、状态、快递单号、下单时间
        全都一样）I 表里是 1.6492、K 表里是 1.65。成本价一进去重键，这两行就成了
        两条不同的记录，这笔成本记两遍——淘宝一家店就多算 23,705.28 元。
        """
        first = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER, _row("330001", "HSC001", 1, 1.6492),
        ])
        second = write_xlsx(tmp_path / "聚水潭成本（补充）-淘宝喜必顺.xlsx", [
            TITLE, HEADER, _row("330001", "HSC001", 1, 1.65),
        ])
        frames = _frames([first, second], model)
        assert sum(f.height for f in frames) == 1
        # 留下的是先到那份的成本价。两次导出之间只动了小数点后几位，认哪个都行，
        # 认两遍才是错的。
        assert _cost(frames) == 1.6492

    def test_the_dedupe_key_ignores_the_cost_column(self, model):
        """成本价不在去重键里。这条守的是上面那个 23,705.28。

        单拎出来断言配置本身，是因为「把成本价加回键里」看起来像是让去重更严格、
        更安全的一次修改，而它的实际后果是 K 表整个白做一遍反向的事，
        且不会有任何一条别的测试变红。
        """
        key = model.source("order_cost").dedupe_key
        assert "unit_cost" not in key, "成本价会被重算，它不是这一行的身份"
        assert list(key) == ["internal_order_id", "sku", "sub_order_id", "quantity"]

    def test_a_header_only_export_does_not_break_the_batch(self, tmp_path, model):
        """有一家店交的是只有表头的空表，其余几家的成本照样进得来。

        空表那一列全是空的，类型判成文本；有数据那几份判成数字。跨文件去重拿这两列
        做 join 会直接抛 SchemaError——一个店少交了内容，整批上传报一个看不懂的错，
        另外四家的成本一分都进不来。真实数据里这不是假设：1688 那份代发表、
        京东和抖音那两份刷单表交上来的就是只有表头。
        """
        empty = write_xlsx(tmp_path / "聚水潭成本-抖音浅花涧节日装饰.xlsx", [TITLE, HEADER])
        real = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            TITLE, HEADER, _row("330001", "HSC001", 2, 5.25),
        ])
        for order in ([empty, real], [real, empty]):
            result = ingest(order, model, ["淘宝喜必顺", "抖音浅花涧节日装饰"])
            frames = [i.frame for i in result.items if i.frame is not None]
            assert _cost(frames) == 10.5

    def test_a_sheet_without_the_title_line_still_yields_cost(self, tmp_path, model):
        """K 表是店长另存的，第一行就是表头，没有「名称：聚水潭成本」那句说明。

        模板上写着表头在第二行，识别时是逐行试出来在第一行的；重解那一步要是照模板
        写死的行号来，第一条数据会被当成表头，然后整张表报「模板要求的列没找到」。
        真实数据里淘宝那份就是这样落空了 42,232 行成本，而且不报错——只在
        「认不出来的表」里留一条「实际表头 14792373.0、5113185662925003221…」，
        没人看得懂那是什么。
        """
        path = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [
            HEADER,
            _row("330001", "HSC001", 2, 5.25),
            _row("330002", "HSC002", 4, 3.0),
        ])
        result = ingest([path], model, ["淘宝喜必顺"])
        item = next(i for i in result.items if i.recognition.known)
        assert not item.error, item.error
        assert item.frame is not None and item.frame.height == 2, "表头行判错会整份丢掉"
        assert _cost([item.frame]) == 22.5

    def test_both_header_layouts_merge_into_one_source(self, tmp_path, model):
        """带说明行的 I 表和不带说明行的 K 表是同一个数据源，重合的行仍只算一次。

        表头行是逐份文件试出来的，不是整批一个值——两种排版混在一批里传上来时，
        去重要照样生效，不能因为其中一份多解出一行表头而错位成两条不同的记录。
        """
        rows = [_row("330001", "HSC001", 2, 5.25)]
        with_title = write_xlsx(tmp_path / "聚水潭成本-淘宝喜必顺.xlsx", [TITLE, HEADER, *rows])
        without = write_xlsx(tmp_path / "聚水潭成本（补充）-淘宝喜必顺.xlsx", [
            HEADER, *rows, _row("330003", "HSC003", 1, 7.0),
        ])
        frames = _frames([with_title, without], model)
        assert sum(f.height for f in frames) == 2
        assert _cost(frames) == 10.5 + 7.0

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
