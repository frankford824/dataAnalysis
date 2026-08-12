"""全局检索。

这个功能存在的理由是：对不上账的时候，人手里只有一个数或者一个订单号。在这之前
答案要靠在几十兆的工作簿里按 Ctrl+F 一张表一张表翻，所以实际上没人查——对不上就
手改一个数把它对上。

所以下面盯的是「人手里那个东西，系统认不认得出来」，以及「找到的东西指得回原文件
哪一行」。指错行比找不到更糟：人会照着错行去改源数据。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
import pytest

from ledger import search
from ledger.model.schema import (
    Metric,
    Model,
    SourceContract,
    Store,
    ValueExpr,
)


@dataclass
class _State:
    store_id: str
    period: str
    run_id: int


def _model() -> Model:
    return Model(
        id="t", name="测试",
        stores=(Store(id="s1", name="甲店", platform="taobao"),
                Store(id="s2", name="乙店", platform="douyin")),
        sources=(SourceContract(id="src", name="对账", owner_role="shop_owner",
                                cadence="monthly"),),
        metrics=(Metric(id="fee", name="平台服务费", source="src",
                        value=ValueExpr(op="sum", of=("a",))),),
    )


ROWS = [
    # 订单号、金额、科目、文件、行号——检索能落到的四种抓手各一条。
    ("5111236008850009225", -88091.88, "推广-直通车", "推广-甲店.xlsx", "5月推广", 61233),
    ("5111236008850009226", -0.11, "天猫返点积分", "对账-甲店.xlsx", "支付宝", 61235),
    ("5111236008850009227", 23.51, "交易收款-交易收款", "对账-甲店.xlsx", "支付宝", 61240),
]


@pytest.fixture
def facts(tmp_path):
    """写两份留档：甲店有那三行，乙店只有一行，用来验证跨店汇总。"""
    def frame(rows):
        return pl.DataFrame(
            {
                "metric_id": ["fee"] * len(rows),
                "link_key": [r[0] for r in rows],
                "amount": [r[1] for r in rows],
                "subject": [r[2] for r in rows],
                "minor": [r[2] for r in rows],
                "linked": [True] * len(rows),
                "file_name": [r[3] for r in rows],
                "sheet": [r[4] for r in rows],
                "row_no": [r[5] for r in rows],
            }
        )

    frame(ROWS).write_parquet(tmp_path / "1.parquet")
    frame(ROWS[:1]).write_parquet(tmp_path / "2.parquet")

    def facts_of(run_id: int):
        path = tmp_path / f"{run_id}.parquet"
        return path if path.exists() else None

    return facts_of


@pytest.fixture
def states():
    return [_State("s1", "2026-05", 1), _State("s2", "2026-05", 2)]


def _find(states, facts, q, **kw):
    return search.search(states, facts, _model(), q, **kw)


# --------------------------------------------------------------------------- #
# 认输入
# --------------------------------------------------------------------------- #

class TestWhatTheInputLooksLike:
    def test_a_long_number_is_an_order_id(self):
        assert "order" in search.classify("5111236008850009225")

    def test_a_number_with_a_decimal_point_is_money(self):
        assert "amount" in search.classify("-88091.88")

    def test_a_bare_integer_is_not_money(self):
        """按金额搜「3」会命中几千行。人打「3」多半是在找别的东西。"""
        assert "amount" not in search.classify("3")

    def test_a_year_is_not_an_order_id(self):
        """2026 不是订单号。门槛放在 8 位是为了这个。"""
        assert "order" not in search.classify("2026")

    def test_everything_is_also_tried_as_text(self):
        """订单号也拿去撞科目名。认错的代价只是多几条结果，认漏的代价是查不到。"""
        assert search.classify("5111236008850009225")[-1] == "text"

    def test_nothing_in_nothing_out(self):
        assert search.classify("   ") == []


# --------------------------------------------------------------------------- #
# 找得到
# --------------------------------------------------------------------------- #

class TestFindingThings:
    def test_finds_an_order_by_its_full_id(self, states, facts):
        res = _find(states, facts, "5111236008850009225")
        assert res.total == 2, "甲店乙店各一行"
        assert {h.store for h in res.hits} == {"甲店", "乙店"}

    def test_points_at_the_exact_cell(self, states, facts):
        """文件、工作表、行号缺一不可。少了行号，人还是得自己翻。"""
        hit = _find(states, facts, "5111236008850009226").hits[0]
        assert (hit.file, hit.sheet, hit.row_no) == ("对账-甲店.xlsx", "支付宝", 61235)

    def test_finds_money_regardless_of_sign(self, states, facts):
        """同一笔钱在订单表里是正的、在对账表里是负的。人手里那个数是从哪张表抄的，
        系统不知道，也不该要求他知道。
        """
        assert _find(states, facts, "88091.88").total == 2
        assert _find(states, facts, "-88091.88").total == 2

    def test_finds_money_written_with_thousands_separators(self, states, facts):
        """人是从界面上复制的，界面上写的是 -88,091.88。"""
        assert _find(states, facts, "-88,091.88").total == 2

    def test_does_not_match_a_different_amount(self, states, facts):
        assert _find(states, facts, "-88091.89").total == 0

    def test_finds_by_subject(self, states, facts):
        res = _find(states, facts, "天猫返点")
        assert res.total == 1
        assert res.hits[0].subject == "天猫返点积分"

    def test_finds_by_file_name(self, states, facts):
        assert _find(states, facts, "推广-甲店").total == 2

    def test_a_subject_with_regex_characters_is_taken_literally(self, states, facts):
        """科目名里带括号、加号的多得是。当成正则会报错或者匹配出一堆无关的。"""
        assert _find(states, facts, "交易收款-交易收款").total == 1


# --------------------------------------------------------------------------- #
# 说清楚找了什么
# --------------------------------------------------------------------------- #

class TestBeingHonestAboutTheSearch:
    def test_says_how_it_read_the_query(self, states, facts):
        """「按订单号找到 3 行」比一份没有出处的结果列表可信得多。"""
        assert _find(states, facts, "5111236008850009225").kinds[0] == "order"

    def test_groups_the_hits_by_store(self, states, facts):
        """这笔钱在几家店都出现过，比多给一百行明细有用。"""
        res = _find(states, facts, "5111236008850009225")
        assert [s["store"] for s in res.by_store] == ["甲店", "乙店"]
        assert all(s["rows"] == 1 for s in res.by_store)

    def test_counts_everything_even_when_it_shows_only_some(self, states, facts):
        """截断的是展示，不是统计。合计说 2 行就是 2 行。"""
        res = _find(states, facts, "5111236008850009225", limit=1)
        assert (res.total, len(res.hits), res.truncated) == (2, 1, True)

    def test_keeps_scanning_other_stores_after_it_has_enough_rows(self, states, facts):
        """够了就不再往结果里塞，但还要继续翻——不然「三家店都有」这句话就丢了。"""
        res = _find(states, facts, "5111236008850009225", limit=1)
        assert len(res.by_store) == 2

    def test_says_when_it_did_not_look_everywhere(self, states, facts, monkeypatch):
        """翻不完要说出来。不说的话，「没找到」和「没翻到」在界面上长得一模一样。"""
        monkeypatch.setattr(search, "MAX_RUNS", 1)
        res = _find(states, facts, "5111236008850009225")
        assert res.exhausted is False
        assert any("没翻" in n for n in res.notes)

    def test_a_missing_archive_is_skipped_not_fatal(self, states, facts):
        """某个店期没留明细，不该让整次检索报错——其他店期的结果照样有用。"""
        res = _find([*states, _State("s1", "2026-04", 99)], facts, "天猫返点")
        assert res.total == 1
        assert res.scanned == 3
