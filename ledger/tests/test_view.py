"""展示层：报表顺序、下钻、节点展开。

这一层不算钱，但决定了人能不能读懂算出来的钱。顺序错了、下钻点不开，报表就退化成
一堆没法核对的数字。
"""

from __future__ import annotations

import polars as pl
import pytest

from ledger.model.loader import load_model
from ledger.model.schema import Metric, Model, SourceContract, StatementNode, ValueExpr
from ledger.view import _finding_lines, drill, node_metrics, oneline, statement_order


@pytest.fixture(scope="module")
def real():
    from ledger.cli import DEFAULT_MODEL
    return load_model(DEFAULT_MODEL)


# --------------------------------------------------------------------------- #
# 报表顺序
# --------------------------------------------------------------------------- #


def _tree() -> Model:
    """两个组各带两个明细，外加一个引用了组的合计。"""
    return Model(
        id="t", name="测试",
        sources=(SourceContract(id="s", name="来源", owner_role="shop_owner", cadence="monthly"),),
        metrics=(
            Metric(id=f"m{i}", name=f"指标{i}", source="s",
                   value=ValueExpr(op="sum", of=("a",)))
            for i in range(1, 5)
        ),
        statement=(
            # 故意照真实模型的写法：明细先声明，组后声明。
            StatementNode(id="d1", name="明细一", level=2,
                          formula={"op": "add", "of": ["m1"]}),
            StatementNode(id="d2", name="明细二", level=2,
                          formula={"op": "add", "of": ["m2"]}),
            StatementNode(id="d3", name="明细三", level=2,
                          formula={"op": "add", "of": ["m3"]}),
            StatementNode(id="d4", name="明细四", level=2,
                          formula={"op": "add", "of": ["m4"]}),
            StatementNode(id="g1", name="组一", level=1, children=("d1", "d2")),
            StatementNode(id="g2", name="组二", level=1, children=("d3", "d4")),
            StatementNode(id="total", name="合计", level=1, is_total=True,
                          formula={"op": "add", "of": ["g1", "g2"]}),
        ),
    )


def test_groups_come_before_their_details():
    """YAML 里明细写在前面是为了好写，报表上得先出组再出它的明细。"""
    assert [n.id for n in statement_order(_tree())] == [
        "g1", "d1", "d2", "g2", "d3", "d4", "total",
    ]


def test_totals_do_not_reprint_the_groups():
    """合计引用了两个组。展开 formula 会把整组明细再印一遍。"""
    ids = [n.id for n in statement_order(_tree())]
    assert ids.count("d1") == 1
    assert ids[-1] == "total"


def test_every_node_appears_exactly_once(real):
    order = statement_order(real)
    ids = [n.id for n in order]
    assert len(ids) == len(set(ids))
    assert set(ids) == {n.id for n in real.statement}, "谁都不许从报表上悄悄消失"


def test_snapshot_is_reordered_on_read():
    """快照冻住数字，不冻排版。已结账的账期不能重算，排版要跟着当前模型走。"""
    from ledger.view import reorder_statement
    snap = {"statement": [
        {"id": "d3", "value": 3}, {"id": "g1", "value": 1}, {"id": "d1", "value": 2},
    ]}
    out = reorder_statement(snap, _tree())
    assert [r["id"] for r in out["statement"]] == ["g1", "d1", "d3"]
    assert [r["value"] for r in out["statement"]] == [1, 2, 3], "数字不能被动过"


def test_reorder_keeps_nodes_the_model_forgot():
    """模型里删掉的科目代表当时确实算出来过的钱，不能丢。"""
    from ledger.view import reorder_statement
    out = reorder_statement({"statement": [{"id": "退役科目"}, {"id": "d1"}]}, _tree())
    assert [r["id"] for r in out["statement"]] == ["d1", "退役科目"]


def test_real_model_reads_top_down(real):
    """真实模型：收入组紧跟着销售收入，不该被十几行明细隔开。"""
    ids = [n.id for n in statement_order(real)]
    assert ids.index("g_revenue") < ids.index("n_receipt")
    assert ids.index("n_receipt") < ids.index("g_platform")
    assert ids[-1] == "net_margin"


# --------------------------------------------------------------------------- #
# 节点展开
# --------------------------------------------------------------------------- #


def test_node_expands_to_metrics():
    assert set(node_metrics(_tree(), "g1")) == {"m1", "m2"}


def test_total_expands_through_groups():
    assert set(node_metrics(_tree(), "total")) == {"m1", "m2", "m3", "m4"}


def test_unknown_node_expands_to_nothing():
    assert node_metrics(_tree(), "没这个节点") == []


def test_real_model_leaf_nodes_are_all_drillable(real):
    """每个明细行都要能点开。点不开的行等于一个没法核对的数字。"""
    leaves = [n for n in real.statement if n.level == 2]
    assert leaves
    for n in leaves:
        assert node_metrics(real, n.id), f"{n.name} 展不开到指标，界面上就点不动"


# --------------------------------------------------------------------------- #
# 下钻
# --------------------------------------------------------------------------- #


def _facts(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "metric_id": pl.Utf8, "amount": pl.Float64, "subject": pl.Utf8, "minor": pl.Utf8,
        "major": pl.Utf8, "link_key": pl.Utf8, "linked": pl.Boolean, "file_name": pl.Utf8,
        "sheet": pl.Utf8, "row_no": pl.Int64,
    }
    base = {"subject": None, "minor": None, "major": None, "link_key": None, "linked": True,
            "file_name": "x.xlsx", "sheet": "Sheet1", "row_no": 2}
    return pl.DataFrame([{**base, **r} for r in rows], schema=schema)


def _classified() -> Model:
    """两个指标共用一个来源，各自只认自己那个大类。

    这就是真实模型里对账表的样子：五个指标读同一张表，谁认领哪一行由归类结果定。
    """
    return Model(
        id="t", name="测试",
        sources=(SourceContract(id="s", name="对账", owner_role="shop_owner",
                                cadence="monthly"),),
        metrics=(
            Metric(id="fee", name="服务费", source="s", major="fee",
                   value=ValueExpr(op="sum", of=("a",))),
            Metric(id="mkt", name="营销费", source="s", major="mkt",
                   value=ValueExpr(op="sum", of=("a",))),
        ),
        statement=(
            StatementNode(id="n_fee", name="服务费", level=2,
                          formula={"op": "add", "of": ["fee"]}),
            StatementNode(id="n_mkt", name="营销费", level=2,
                          formula={"op": "add", "of": ["mkt"]}),
        ),
    )


#: 引擎产出的事实行长这样：同一张表的每一行，在每个读这张表的指标名下各出现一次。
#: 真正算进哪个指标，由 major 决定。
_CROSS = [
    {"metric_id": "fee", "amount": -10.0, "major": "fee", "row_no": 2},
    {"metric_id": "fee", "amount": -99.0, "major": "mkt", "row_no": 3},
    {"metric_id": "mkt", "amount": -10.0, "major": "fee", "row_no": 2},
    {"metric_id": "mkt", "amount": -99.0, "major": "mkt", "row_no": 3},
]


def test_drill_only_counts_the_rows_the_metric_claimed():
    """事实表里存的是「指标看过的行」，不是「指标算进去的行」。

    不补这一层过滤，五个读同一张对账表的指标会下钻出同一个数。实测淘宝那家店
    「平台服务费」下钻出 -3,258.99、报表上是 -42,236.94，而「平台营销费用」
    下钻出的也是 -3,258.99——三个数没一个对得上，而它们看着都像那么回事。
    """
    fee = drill(_facts(_CROSS), _classified(), "n_fee")
    mkt = drill(_facts(_CROSS), _classified(), "n_mkt")
    assert fee["source_total"] == pytest.approx(-10.0)
    assert mkt["source_total"] == pytest.approx(-99.0)


def test_drill_does_not_show_rows_that_belong_to_a_sibling():
    """行号也不能串。人点开是要去源文件那一行核对的，指错行比不给行更糟。"""
    d = drill(_facts(_CROSS), _classified(), "n_fee")
    assert [r["row_no"] for r in d["sample"]] == [2]


def test_drill_keeps_every_row_when_the_source_has_no_categories():
    """推广扣费、运费这类表源头就不分科目，指标也不声明大类。

    对它们照样要求 major 相等的话，整张表会被筛空——下钻出一个 0，
    而报表上明明写着 -88,091.88。
    """
    model = Model(
        id="t", name="测试",
        sources=(SourceContract(id="s", name="推广", owner_role="shop_owner",
                                cadence="monthly"),),
        metrics=(Metric(id="ad", name="推广费", source="s",
                        value=ValueExpr(op="sum", of=("a",))),),
        statement=(StatementNode(id="n_ad", name="推广费", level=2,
                                 formula={"op": "add", "of": ["ad"]}),),
    )
    d = drill(_facts([{"metric_id": "ad", "amount": -7.0},
                      {"metric_id": "ad", "amount": -3.0}]), model, "n_ad")
    assert d["source_total"] == pytest.approx(-10.0)


def test_drill_reports_the_statement_number_next_to_the_source_total():
    """原始行合计不等于报表数字——中间隔着分摊、账期归属和孤儿行。

    只给一个对不上的数，人会以为报表算错了。两个数一起给，差额才有出处。
    """
    d = drill(_facts([{"metric_id": "fee", "amount": -10.0, "major": "fee"}]),
              _classified(), "n_fee", value=-8.0)
    assert (d["source_total"], d["value"]) == (pytest.approx(-10.0), -8.0)


def test_drill_says_it_does_not_know_the_statement_number():
    """给不出报表数字时给 None，不能拿原始行合计冒充。"""
    assert drill(_facts([{"metric_id": "fee", "amount": -10.0, "major": "fee"}]),
                 _classified(), "n_fee")["value"] is None


def test_drill_carries_row_level_evidence():
    """只报总数不给行号，对不上账时没人查得动。"""
    d = drill(_facts([
        {"metric_id": "m1", "amount": -100.0, "row_no": 7, "link_key": "A1"},
        {"metric_id": "m1", "amount": -50.0, "row_no": 8, "link_key": "A2"},
    ]), _tree(), "d1")
    assert d["rows"] == 2
    assert d["total"] == pytest.approx(-150.0)
    assert d["sample"][0]["row_no"] == 7, "金额大的排前面"
    assert d["sample"][0]["file_name"] == "x.xlsx"


def test_drill_ignores_other_metrics():
    d = drill(_facts([
        {"metric_id": "m1", "amount": -100.0},
        {"metric_id": "m3", "amount": -999.0},
    ]), _tree(), "d1")
    assert d["total"] == pytest.approx(-100.0)


def test_drill_skips_subject_grouping_when_there_is_no_subject_column():
    """推广扣费那张表没有科目列，硬分出来是一行「未分类 6,324 行」——
    看着像 6,324 行漏了归类，实际是这项本来就不分科目。"""
    d = drill(_facts([{"metric_id": "m1", "amount": -1.0}]), _tree(), "d1")
    assert d["by_subject"] == []


def test_drill_groups_by_subject_when_present():
    d = drill(_facts([
        {"metric_id": "m1", "amount": -1.0, "minor": "快递费"},
        {"metric_id": "m1", "amount": -2.0, "minor": "快递费"},
        {"metric_id": "m1", "amount": -9.0, "minor": "赔付"},
    ]), _tree(), "d1")
    assert [x["subject"] for x in d["by_subject"]] == ["赔付", "快递费"]
    assert d["by_subject"][0]["count"] == 1


def test_drill_merges_the_same_fee_under_two_raw_names():
    """字典收过的和原始科目本身就叫这个名字的，界面上是同一行。

    天猫皇莉诗平台服务费下钻曾经并排出两行「类目软件服务费」：33,017 行的原始
    科目带着 0030003|，另外 323 行原始科目就叫「类目软件服务费」。按两列分
    它们是两组，显示名却一样，按费项对账对不上。
    """
    d = drill(_facts([
        {"metric_id": "m1", "amount": -42.0, "minor": "类目软件服务费",
         "subject": "0030003|软件服务费-类目软件服务费（原天猫佣金）"},
        {"metric_id": "m1", "amount": -0.2, "minor": "类目软件服务费",
         "subject": "类目软件服务费"},
    ]), _tree(), "d1")
    assert [x["subject"] for x in d["by_subject"]] == ["类目软件服务费"]
    assert d["by_subject"][0]["count"] == 2
    assert d["by_subject"][0]["amount"] == pytest.approx(-42.2)


def test_drill_says_when_it_truncated():
    rows = [{"metric_id": "m1", "amount": float(-i), "row_no": i} for i in range(1, 12)]
    d = drill(_facts(rows), _tree(), "d1", limit=5)
    assert len(d["sample"]) == 5
    assert d["truncated"] is True
    assert d["rows"] == 11, "截断的是展示，不是统计"


def test_drill_on_empty_facts_is_not_an_error():
    d = drill(pl.DataFrame(), _tree(), "d1")
    assert d["rows"] == 0 and d["total"] == 0.0


# --------------------------------------------------------------------------- #
# 下钻：翻页与筛选
#
# 淘宝那家店一个月的推广扣费六千多行。只给头 200 行、不给筛选，等于没给：
# 人要找的那一行大概率不在这 200 行里。
# --------------------------------------------------------------------------- #


def _many() -> pl.DataFrame:
    """六行明细，两个科目、两个来源文件。"""
    return _facts([
        {"metric_id": "m1", "amount": -60.0, "minor": "快递费", "row_no": 2,
         "file_name": "运费.xlsx", "link_key": "A1"},
        {"metric_id": "m1", "amount": -50.0, "minor": "快递费", "row_no": 3,
         "file_name": "运费.xlsx", "link_key": "A2"},
        {"metric_id": "m1", "amount": -40.0, "minor": "赔付", "row_no": 4,
         "file_name": "运费.xlsx", "link_key": "A3"},
        {"metric_id": "m1", "amount": -30.0, "minor": "赔付", "row_no": 5,
         "file_name": "对账.xlsx", "link_key": "B1"},
        {"metric_id": "m1", "amount": -20.0, "minor": "快递费", "row_no": 6,
         "file_name": "对账.xlsx", "link_key": "B2"},
        {"metric_id": "m1", "amount": -10.0, "minor": "赔付", "row_no": 7,
         "file_name": "对账.xlsx", "link_key": "B3"},
    ])


def test_drill_pages_through_the_rows():
    first = drill(_many(), _tree(), "d1", limit=2)
    second = drill(_many(), _tree(), "d1", limit=2, offset=2)
    assert [r["row_no"] for r in first["sample"]] == [2, 3]
    assert [r["row_no"] for r in second["sample"]] == [4, 5]


def test_paging_does_not_skip_rows_that_tie():
    """并列的行必须有稳定次序。次序不稳，两次请求之间同一行会换页——
    翻页时漏掉的那行不会有任何提示，人只会以为它不存在。
    """
    same = _facts([
        {"metric_id": "m1", "amount": -5.0, "row_no": i, "file_name": "a.xlsx"}
        for i in range(1, 8)
    ])
    seen = []
    for off in range(0, 7, 2):
        seen += [r["row_no"] for r in
                 drill(same, _tree(), "d1", limit=2, offset=off)["sample"]]
    assert seen == [1, 2, 3, 4, 5, 6, 7]


def test_row_order_follows_the_source_file():
    """按金额排是为了找异常，按行号排是为了对着源文件逐行核。两件事都要做。"""
    d = drill(_many(), _tree(), "d1", order="row")
    assert [(r["file_name"], r["row_no"]) for r in d["sample"]][:2] == [
        ("对账.xlsx", 5), ("对账.xlsx", 6)
    ]


def test_filter_by_subject_narrows_the_rows():
    d = drill(_many(), _tree(), "d1", subject="赔付")
    assert {r["minor"] for r in d["sample"]} == {"赔付"}
    assert d["selection"]["rows"] == 3
    assert d["selection"]["amount"] == pytest.approx(-80.0)


def test_filter_by_file_narrows_the_rows():
    d = drill(_many(), _tree(), "d1", file="对账.xlsx")
    assert {r["file_name"] for r in d["sample"]} == {"对账.xlsx"}
    assert d["selection"]["rows"] == 3


def test_filters_stack():
    d = drill(_many(), _tree(), "d1", subject="赔付", file="对账.xlsx")
    assert [r["row_no"] for r in d["sample"]] == [5, 7]


def test_keyword_searches_the_order_id():
    """人手里往往只有一个订单号，而它不在这一页上。"""
    d = drill(_many(), _tree(), "d1", q="B2")
    assert [r["row_no"] for r in d["sample"]] == [6]


def test_promotion_drill_labels_the_key_as_product_id(real):
    """拼多多商品分天推广挂的是商品 ID。写成订单号的话人会对着订单库去查。"""
    d = drill(_facts([{"metric_id": "ad_cost", "amount": -7.47}]), real, "n_ad")
    assert d["key_label"] == "商品ID"


def test_fee_drill_still_says_order_id(real):
    d = drill(_facts([{"metric_id": "software_fee", "amount": -1.0, "major": "software_fee"}]),
              real, "n_software")
    assert d["key_label"] == "订单号"


def test_keyword_is_taken_literally():
    """科目名里带括号、加号的多得是。当成正则不是报错就是撞出一堆无关的行。"""
    d = drill(_facts([
        {"metric_id": "m1", "amount": -1.0, "minor": "保证金-天猫-扣除转移"},
        {"metric_id": "m1", "amount": -2.0, "minor": "保证金X天猫X扣除转移"},
    ]), _tree(), "d1", q="保证金-天猫")
    assert d["selection"]["rows"] == 1


def test_the_headline_numbers_do_not_move_when_you_filter():
    """人下钻就是为了拿这两个数跟报表核对。核对基准跟着筛选变，这事就没法做了。"""
    whole = drill(_many(), _tree(), "d1", value=-200.0)
    part = drill(_many(), _tree(), "d1", value=-200.0, subject="赔付")
    assert part["source_total"] == whole["source_total"] == pytest.approx(-210.0)
    assert part["rows"] == whole["rows"] == 6
    assert part["value"] == -200.0


def test_the_summaries_stay_whole_so_you_can_switch_filters():
    """按科目汇总是导航入口。点了「赔付」就把它自己筛成一行，人就回不去了。"""
    d = drill(_many(), _tree(), "d1", subject="赔付")
    assert {x["subject"] for x in d["by_subject"]} == {"赔付", "快递费"}
    assert {x["file"] for x in d["by_file"]} == {"运费.xlsx", "对账.xlsx"}


def test_selection_says_whether_there_is_a_next_page():
    d = drill(_many(), _tree(), "d1", limit=2)
    assert (d["selection"]["has_more"], d["truncated"]) == (True, True)
    last = drill(_many(), _tree(), "d1", limit=2, offset=4)
    assert (last["selection"]["has_more"], last["truncated"]) == (False, False)


def test_selection_repeats_the_filters_back():
    """界面照着这个渲染筛选状态，不用自己记——记岔了会出现「看着筛了、其实没筛」。"""
    sel = drill(_many(), _tree(), "d1", subject="赔付", q=" B1 ")["selection"]
    assert (sel["subject"], sel["q"], sel["filtered"]) == ("赔付", "B1", True)
    assert drill(_many(), _tree(), "d1")["selection"]["filtered"] is False


def test_a_filter_that_matches_nothing_is_not_an_error():
    d = drill(_many(), _tree(), "d1", q="根本没有这个词")
    assert (d["sample"], d["selection"]["rows"]) == ([], 0)
    assert d["rows"] == 6, "筛没了不代表这个节点没数"


def test_empty_drill_still_describes_the_page():
    """空结果也要带 selection，界面才不用为「有没有这个字段」写两套分支。"""
    assert drill(pl.DataFrame(), _tree(), "d1")["selection"]["rows"] == 0


# --------------------------------------------------------------------------- #
# 下钻：进账的和没进账的
#
# 源表里的行不是都进损益表。运费表是全公司的运单，淘宝那家店 29.9 万行里只有
# 1.4 万行挂得上自己的订单，其余五十三万块钱属于别的店铺。全摆出来的话，点开
# 「发货运费」看到的是 -550,944，而报表上写着 -20,294。
# --------------------------------------------------------------------------- #


def _graded(rows: list[dict]) -> pl.DataFrame:
    """带进账标记的事实行。counted 是进没进账，contribution 是实际算进去多少。"""
    out = _facts([{k: v for k, v in r.items()
                   if k not in ("counted", "contribution")} for r in rows])
    return out.with_columns(
        pl.Series("counted", [bool(r.get("counted", True)) for r in rows]),
        pl.Series("contribution", [
            float(r.get("contribution", r["amount"] if r.get("counted", True) else 0.0))
            for r in rows
        ]),
    )


_MIXED = [
    {"metric_id": "m1", "amount": -20.0, "row_no": 2, "counted": True},
    {"metric_id": "m1", "amount": -30.0, "row_no": 3, "counted": True},
    {"metric_id": "m1", "amount": -530.0, "row_no": 4, "counted": False},
]


def test_drill_shows_the_money_that_actually_landed():
    """默认只给进了账的部分，加起来正好是报表数字。"""
    d = drill(_graded(_MIXED), _tree(), "d1", value=-50.0)
    assert d["source_total"] == pytest.approx(-50.0) == pytest.approx(d["value"])
    assert [r["row_no"] for r in d["sample"]] == [3, 2], "金额大的排前面"


def test_drill_reports_the_money_that_did_not_land():
    """不能悄悄丢掉。「这笔钱去哪了」每个月都会被问到。"""
    u = drill(_graded(_MIXED), _tree(), "d1")["uncounted"]
    assert (u["rows"], u["amount"]) == (1, pytest.approx(-530.0))


def test_drill_can_go_look_at_what_did_not_land():
    d = drill(_graded(_MIXED), _tree(), "d1", only="uncounted")
    assert [r["row_no"] for r in d["sample"]] == [4]
    assert d["source_total"] == pytest.approx(-530.0)


def test_drill_can_show_both_at_once():
    d = drill(_graded(_MIXED), _tree(), "d1", only="all")
    assert d["rows"] == 3
    assert d["source_total"] == pytest.approx(-580.0), "两边一起看时对不上报表，正常"


def test_the_uncounted_summary_is_there_whichever_view_you_are_in():
    """切到「没进账」那一档时，这个数不能跟着变成 0——它是导航回来的路标。"""
    for only in ("counted", "uncounted", "all"):
        d = drill(_graded(_MIXED), _tree(), "d1", only=only)
        assert d["uncounted"]["rows"] == 1


def test_counted_rows_use_the_allocated_amount_not_the_raw_one():
    """一笔主订单级的钱按比例摊到子订单上。报原始金额的话，
    下钻永远比报表多一截，而多出来的那截没有出处。"""
    d = drill(_graded([{"metric_id": "m1", "amount": -100.0, "counted": True,
                        "contribution": -75.0}]), _tree(), "d1")
    assert d["source_total"] == pytest.approx(-75.0)
    assert d["sample"][0]["amount"] == pytest.approx(-100.0), "原始金额照样给，核对源文件要用"


def test_the_summaries_follow_the_same_number():
    """按科目、按文件的汇总必须和顶上那个合计同口径，否则两处数加起来对不上。"""
    d = drill(_graded([{"metric_id": "m1", "amount": -100.0, "minor": "快递费",
                        "counted": True, "contribution": -75.0}]), _tree(), "d1")
    assert d["by_subject"][0]["amount"] == pytest.approx(-75.0)
    assert d["by_file"][0]["amount"] == pytest.approx(-75.0)


def test_an_old_archive_without_the_mark_still_opens():
    """进账标记是后加的。老快照没有这两列，退回全部显示并标出来，
    界面照着提示重算一次——总比点开一片空白强。"""
    d = drill(_facts([{"metric_id": "m1", "amount": -10.0}]), _tree(), "d1")
    assert d["graded"] is False
    assert (d["only"], d["rows"]) == ("all", 1)


def test_a_fresh_archive_says_it_is_graded():
    assert drill(_graded(_MIXED), _tree(), "d1")["graded"] is True


def test_nothing_landed_at_all():
    """整项都没进账时给 0 和一句说明，不是一个空壳。"""
    d = drill(_graded([{"metric_id": "m1", "amount": -530.0, "counted": False}]),
              _tree(), "d1")
    assert (d["source_total"], d["sample"]) == (0.0, [])
    assert d["uncounted"]["amount"] == pytest.approx(-530.0)


def test_a_penny_of_float_noise_does_not_show_up_as_a_difference():
    """留档里的金额是 f64，四万行加起来会攒出半分钱的误差。

    报表写 -172,082.78、下钻写 -172,082.79，界面就会标一句「差 -0.01」——
    那一分钱不存在，而人会去查一笔根本没有的账。
    """
    rows = [{"metric_id": "m1", "amount": 0.1, "counted": True} for _ in range(30)]
    d = drill(_graded(rows), _tree(), "d1", value=3.0)
    assert d["source_total"] == 3.0


def test_an_unknown_view_falls_back_to_the_safe_one():
    """界面传错值时给进了账的那部分。多给一屏别家店铺的行是误导，少给不是。"""
    assert drill(_graded(_MIXED), _tree(), "d1", only="乱写")["only"] == "counted"


# --------------------------------------------------------------------------- #
# 文案
# --------------------------------------------------------------------------- #


def test_oneline_does_not_leave_gaps_after_chinese_punctuation():
    """模型里的提示语用 YAML 折叠写法，换行变空格，中文标点后会留下夹缝。"""
    assert oneline("还没同步，\n或者导出时选窄了") == "还没同步，或者导出时选窄了"


def test_a_checks_bullet_list_survives_being_flattened():
    """检查结论里逐条列举的那几行，要在压成一行之前先拆出来。

    `oneline` 把整段压成一行是对的（YAML 折叠写法会带进假换行），但压完之后
    「缺三份表」和「缺一份表」在界面上一样长——三条明细连成了一堵墙。
    """
    lines = _finding_lines("还差 2 份数据：\n  · 运费 —— 物流\n  · 对账 —— 财务")
    assert lines == ["运费 —— 物流", "对账 —— 财务"]


def test_a_one_sentence_conclusion_has_no_bullets_to_pull_out():
    assert _finding_lines("所有科目都认识") == []
