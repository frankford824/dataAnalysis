"""快路和慢路必须给出同一个结果。

引擎里有四处「整列算一遍，算不动的再逐行」：日期、金额、归类规则链、取键规则链。
快路的存在理由纯粹是速度——淘宝一家店一个月 184 万行，逐行回 Python 要五十秒，
整列跑完二十秒。它不该带来任何口径上的变化。

这批测试就盯着这一件事：同样的输入，两条路的输出一个字都不能差。回放门盯的是
真实数据上的总额，覆盖不到的是那些真实数据里恰好没出现的写法——括号负数、
百分号、全角标点、Excel 序列号、空串、混进文本的数字列。那些正是快路最容易
悄悄算错的地方，所以在这里逐个钉住。

发现快路和慢路对不上时，改快路，不要改慢路来迁就它：慢路是口径的定义，
快路只是它的一个实现。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

import importlib

# `ledger.engine` 的 __init__ 把 classify 和 link 这两个函数名导出到了包上，
# 正好盖住同名的子模块——`import ledger.engine.link as L` 拿到的是那个函数。
# 这里要的是模块本身（里面的私有函数就是被测对象），所以直接按名字取。
C = importlib.import_module("ledger.engine.classify")
L = importlib.import_module("ledger.engine.link")
from ledger.engine.normalize import _date_expr, _number_expr, to_date, to_number
from ledger.engine.parse import _cell_cleaner
from ledger.engine.rules import (
    EXCLUDED as _EXCLUDED,
    ChainStats,
    compile_classify_rules,
    compile_key_rules,
    resolve_class,
    resolve_key,
)
from ledger.model.schema import (
    Bridge,
    ClassifyRule,
    DictionaryEntry,
    FieldMatch,
    KeyRule,
    Model,
    ParseOptions,
)


# --------------------------------------------------------------------------- #
# 日期
# --------------------------------------------------------------------------- #


class TestDatesParseTheSameBothWays:
    """`_date_expr` 只处理规整写法，剩下的回落到 `to_date`。两边合起来要等于全走
    `to_date`。"""

    SAMPLES = [
        "2026-05-01 13:24:35",
        "2026-05-01 13:24",
        "2026-05-01",
        "2026/05/01 13:24:35",
        "2026/05/01",
        "20260501135959",
        "20260501",
        "2026年5月1日",
        "2026.05.01",
        "2026-05-01T13:24:35",
        "2026-05-01T13:24:35+08:00",
        "  2026-05-01  ",
        "45413",           # Excel 序列号
        "45413.5",
        "",
        "无",
        None,
        "订单已完成",
    ]

    def test_every_written_form_agrees(self):
        frame = pl.DataFrame({"t": [s for s in self.SAMPLES]}, schema={"t": pl.Utf8})
        fast = frame.select(_date_expr("t", pl.Utf8).alias("d")).get_column("d").to_list()
        slow = [to_date(s) for s in self.SAMPLES]

        for written, quick, sure in zip(self.SAMPLES, fast, slow):
            if quick is not None:
                assert quick == sure, f"{written!r} 快路算成 {quick}，逐行算出来是 {sure}"

    def test_the_fast_path_never_invents_a_date(self):
        """快路可以答不上来（留 null 交给慢路），但不能答错。"""
        junk = ["订单号12345678", "-", "N/A", "2026-13-45", "5/1/2026"]
        frame = pl.DataFrame({"t": junk})
        fast = frame.select(_date_expr("t", pl.Utf8).alias("d")).get_column("d").to_list()
        for written, quick in zip(junk, fast):
            assert quick is None or quick == to_date(written), f"{written!r} 被快路错解成 {quick}"

    def test_datetime_columns_skip_parsing_entirely(self):
        frame = pl.DataFrame({"t": [dt.datetime(2026, 5, 1, 13, 0)]})
        got = frame.select(_date_expr("t", frame.schema["t"]).alias("d")).get_column("d")[0]
        assert got == dt.date(2026, 5, 1)


# --------------------------------------------------------------------------- #
# 金额
# --------------------------------------------------------------------------- #


class TestAmountsParseTheSameBothWays:
    SAMPLES = [
        "1234.56", "-1234.56", "+1234.56", "1,234.56", "-1,234.56",
        "0", "0.00", ".5", "12.", "1234", "  1234.56  ",
        "¥1,234.56", "1234.56元", "(123.45)", "（123.45）", "12%",
        "", "-", "无退款申请", None, "1e3",
    ]

    def test_every_written_form_agrees(self):
        frame = pl.DataFrame({"a": self.SAMPLES}, schema={"a": pl.Utf8})
        fast = frame.select(_number_expr("a", pl.Utf8).alias("n")).get_column("n").to_list()
        for written, quick in zip(self.SAMPLES, fast):
            if quick is not None:
                assert quick == pytest.approx(to_number(written)), (
                    f"{written!r} 快路算成 {quick}，逐行算出来是 {to_number(written)}"
                )

    def test_booleans_are_not_numbers(self):
        """逐行版对布尔明确返回 None——`True` 不是 1 块钱。"""
        frame = pl.DataFrame({"a": [True, False]})
        fast = frame.select(_number_expr("a", frame.schema["a"]).alias("n")).get_column("n")
        assert fast.to_list() == [None, None]
        assert to_number(True) is None

    def test_numeric_columns_pass_through(self):
        frame = pl.DataFrame({"a": [1.5, -2.0, None]})
        fast = frame.select(_number_expr("a", frame.schema["a"]).alias("n")).get_column("n")
        assert fast.to_list() == [1.5, -2.0, None]

    @pytest.mark.parametrize(
        "written",
        ["HSC25016", "HZS00888", "887836316460件", "2026-05-22", "已发货", "3-5", "1.2.3"],
    )
    def test_codes_in_a_money_column_are_not_money(self, written):
        """成本价那列里混进商品编码，要留空，不能抠出里面的数字。

        淘宝聚水潭导出里真有三行是这样的。抠数字的写法会把 `HSC25016` 算成两万五
        的成本——一行凭空多出的钱比整张表一天的成本还多，而且不报错。
        """
        assert to_number(written) is None
        frame = pl.DataFrame({"a": [written]}, schema={"a": pl.Utf8})
        assert frame.select(_number_expr("a", pl.Utf8).alias("n")).get_column("n")[0] is None


# --------------------------------------------------------------------------- #
# 单元格清洗
# --------------------------------------------------------------------------- #


class TestCleaningARow:
    def setup_method(self):
        self.clean = _cell_cleaner(ParseOptions())

    def test_strips_both_ends_and_folds_null_tokens(self):
        assert self.clean(["  甲  ", "\t乙\u3000", "-", "N/A", "丙"], 5) == (
            "甲", "乙", "", "", "丙"
        )

    def test_an_all_blank_row_is_dropped(self):
        assert self.clean(["", "  ", None, "-"], 4) is None

    def test_a_row_of_zeros_is_not_blank(self):
        """0 是假值，但一行全是 0 是真数据。用 `any()` 判空会把它整行丢掉。"""
        assert self.clean([0, 0.0, 0], 3) == (0, 0.0, 0)

    def test_short_rows_are_padded_and_long_ones_kept(self):
        assert self.clean(["甲"], 3) == ("甲", None, None)
        assert self.clean(["甲", "乙", "丙", "丁"], 2) == ("甲", "乙", "丙", "丁")

    def test_non_text_values_survive_untouched(self):
        when = dt.datetime(2026, 5, 1)
        assert self.clean([when, 12, "  甲 "], 3) == (when, 12, "甲")


# --------------------------------------------------------------------------- #
# 归类规则链
# --------------------------------------------------------------------------- #


def _model(entries: list[tuple[str, str, str]]) -> Model:
    return Model(
        id="t", name="测试模型",
        dictionary=tuple(
            DictionaryEntry(platform=p, raw=raw, minor=major, major=major)
            for p, raw, major in entries
        )
    )


CLASSIFY_RULES = (
    ClassifyRule(when=FieldMatch(field="remark", contains=["保证金解冻"]), exclude=True),
    ClassifyRule(dictionary=True),
    ClassifyRule(when=FieldMatch(field="remark", contains=["技术服务费", "软件服务费"]),
                 major="software_fee"),
    ClassifyRule(when=FieldMatch(field="remark", matches=r"^订单.*打款$"), major="trade_receipt"),
    ClassifyRule(when=FieldMatch(field="biz_type", equals=["交易分账"]), major="marketing_fee"),
)

CLASSIFY_ROWS = {
    "subject": [
        "交易收款", "  交易收款  ", "交易收款（含税）", "", None,
        "未知科目", "交易收款", "交易收款", "不认识",
    ],
    "remark": [
        "", "", "", "先用后付技术服务费", "订单123打款",
        "保证金解冻 5000", "", "", "什么都不沾",
    ],
    "biz_type": ["", "", "", "", "", "", "交易分账", "", ""],
}


class TestClassifyChainAgrees:
    """整列版和逐行版对同一批行给出同一个结果。

    这批行是照着真实数据里踩过的坑挑的：前后带空格的科目名、全角括号、空科目、
    只能靠备注关键词认出来的行、被显式排除的行、以及谁都不认的行。
    """

    def _frame(self):
        return pl.DataFrame(CLASSIFY_ROWS, schema={k: pl.Utf8 for k in CLASSIFY_ROWS})

    def _slow(self, model, platform="taobao"):
        compiled = compile_classify_rules(CLASSIFY_RULES)
        table = C._dictionary_for(model, platform)
        out = []
        for row in self._frame().to_dicts():
            out.append(resolve_class(row, compiled, lambda r: table.get(_norm_header(r))))
        return out

    def test_same_major_minor_and_exclusion_for_every_row(self):
        model = _model([
            ("taobao", "交易收款", "trade_receipt"),
            ("taobao", "交易收款(含税)", "trade_receipt"),
        ])
        frame = self._frame()
        compiled = compile_classify_rules(CLASSIFY_RULES)
        table = C._dictionary_for(model, "taobao")

        winner = C._chain_winner(frame, compiled, table)
        assert winner is not None, "这条链本该走整列，走不了说明能力检测判错了"

        from ledger.engine.types import ClassifyReport

        fast = C._decide(frame, winner, compiled, table, ChainStats(), ClassifyReport())
        for i, (major, minor, drop) in enumerate(self._slow(model)):
            assert fast.get_column(C.COL_MAJOR)[i] == (major or None), f"第 {i} 行口径项不一致"
            assert fast.get_column(C.COL_MINOR)[i] == (minor or None), f"第 {i} 行小类不一致"
            assert fast.get_column(C.COL_EXCLUDED)[i] is drop, f"第 {i} 行排除标记不一致"

    def test_a_regex_rust_cannot_run_falls_back_instead_of_erroring(self):
        """Rust 的 regex 不支持前后向查看。撞上就该整条回退，不该炸。"""
        rules = compile_classify_rules((
            ClassifyRule(when=FieldMatch(field="remark", matches=r"(?=服务费)"), major="x"),
        ))
        assert C._chain_winner(self._frame(), rules, {}) is None


def _norm_header(raw):
    from ledger.model.schema import normalize_header

    return normalize_header(raw)


# --------------------------------------------------------------------------- #
# 取键规则链
# --------------------------------------------------------------------------- #


KEY_RULES = (
    KeyRule(when=FieldMatch(field="remark", contains=["保证金", "提现"]), exclude=True),
    KeyRule(when=FieldMatch(field="sub_order_id", notnull=True)),
    KeyRule(when=FieldMatch(field="remark", extract=r"交易单号[:：]\s*(\d+)")),
    KeyRule(
        when=FieldMatch(field="tracking_no", notnull=True),
        via=Bridge(source="orders", match="tracking_no", take="order_id"),
    ),
)

KEY_ROWS = {
    "sub_order_id": ["子1", "", "", "", "", " 子2 ", "", "12345.0", ""],
    "remark": ["", "交易单号:998877", "提现到银行卡", "", "什么都没有",
               "", "交易单号：  776655", "", ""],
    "tracking_no": ["", "", "", "SF001", "", "", "", "", "SF999"],
}


class TestKeyChainAgrees:
    def _frame(self):
        return pl.DataFrame(KEY_ROWS, schema={k: pl.Utf8 for k in KEY_ROWS})

    BRIDGES = {"orders": {"SF001": "主订单-9"}}

    def test_same_key_for_every_row(self):
        frame = self._frame()
        compiled = compile_key_rules(KEY_RULES)

        fast = L._keys_vectorized(frame, compiled, self.BRIDGES)
        assert fast is not None, "这条链本该走整列"
        keys, _stats = fast

        for i, row in enumerate(frame.to_dicts()):
            got = resolve_key(row, compiled, self.BRIDGES)
            want = L.EXCLUDED_KEY if got == "__excluded__" or got is _EXCLUDED else (got or None)
            assert keys[i] == want, f"第 {i} 行取到的键不一致：整列 {keys[i]!r}，逐行 {want!r}"

    def test_stats_count_the_same_rules(self):
        """规则链统计是用来回答「这条规则还有没有用」的，两条路必须数出同一个结果。"""
        frame = self._frame()
        compiled = compile_key_rules(KEY_RULES)

        slow = ChainStats()
        for row in frame.to_dicts():
            resolve_key(row, compiled, self.BRIDGES, slow)
        _keys, fast = L._keys_vectorized(frame, compiled, self.BRIDGES)

        assert fast.hits == slow.hits
        assert fast.excluded == slow.excluded
        assert fast.unmatched == slow.unmatched
        assert fast.total == slow.total

    def test_a_bridge_miss_falls_through_to_the_next_rule(self):
        """回中间表查不到，不算这一环命中——要继续往下试，不能停在这里给个空键。"""
        frame = pl.DataFrame({
            "sub_order_id": [""], "remark": ["交易单号:123456"], "tracking_no": ["查不到的单号"],
        })
        compiled = compile_key_rules((
            KeyRule(
                when=FieldMatch(field="tracking_no", notnull=True),
                via=Bridge(source="orders", match="tracking_no", take="order_id"),
            ),
            KeyRule(when=FieldMatch(field="remark", extract=r"交易单号[:：]\s*(\d+)")),
        ))
        keys, stats = L._keys_vectorized(frame, compiled, self.BRIDGES)
        assert keys[0] == "123456"
        assert stats.hits == {1: 1}


