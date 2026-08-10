"""展示层：报表顺序、下钻、节点展开。

这一层不算钱，但决定了人能不能读懂算出来的钱。顺序错了、下钻点不开，报表就退化成
一堆没法核对的数字。
"""

from __future__ import annotations

import polars as pl
import pytest

from ledger.model.loader import load_model
from ledger.model.schema import Metric, Model, SourceContract, StatementNode, ValueExpr
from ledger.view import drill, node_metrics, oneline, statement_order


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
        "link_key": pl.Utf8, "linked": pl.Boolean, "file_name": pl.Utf8,
        "sheet": pl.Utf8, "row_no": pl.Int64,
    }
    base = {"subject": None, "minor": None, "link_key": None, "linked": True,
            "file_name": "x.xlsx", "sheet": "Sheet1", "row_no": 2}
    return pl.DataFrame([{**base, **r} for r in rows], schema=schema)


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
# 文案
# --------------------------------------------------------------------------- #


def test_oneline_does_not_leave_gaps_after_chinese_punctuation():
    """模型里的提示语用 YAML 折叠写法，换行变空格，中文标点后会留下夹缝。"""
    assert oneline("还没同步，\n或者导出时选窄了") == "还没同步，或者导出时选窄了"
