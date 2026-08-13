"""工作区：留档、快照、账期状态机。

这里锁的每一条都直接关系到钱：同名新版本必须顶掉旧版（否则成本双份）、已结账的
数字必须冻住（否则报出去的账会自己变）、结不了的账不许结。
"""

from __future__ import annotations

import io

import pytest

from ledger.service import _keep_facts
from ledger.workspace import CLOSED, OPEN, Workspace, WorkspaceError


@pytest.fixture()
def ws(tmp_path):
    w = Workspace(tmp_path / "space")
    yield w
    w.close()


def _blob(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# 留档
# --------------------------------------------------------------------------- #


def test_same_content_stored_once(ws):
    a = ws.keep("成本-淘宝喜必顺.xlsx", _blob("x"), "taobao_xibishun")
    b = ws.keep("运费-淘宝喜必顺.xlsx", _blob("x"), "taobao_xibishun")
    assert a.sha == b.sha
    assert len(list((ws.root / "files").rglob("*")) ) >= 1
    # 两个槽位，一份内容。
    assert len(ws.submissions("taobao_xibishun")) == 2


def test_reupload_identical_is_reported_as_unchanged(ws):
    ws.keep("成本.xlsx", _blob("x"), "s1")
    again = ws.keep("成本.xlsx", _blob("x"), "s1")
    assert again.unchanged
    assert not again.replaced


def test_same_name_new_content_replaces(ws):
    """店长改了数据重新导出，文件名不变。两版都参与计算就是双份成本。"""
    first = ws.keep("成本.xlsx", _blob("旧"), "s1")
    second = ws.keep("成本.xlsx", _blob("新"), "s1")
    assert second.replaced == first.sha
    assert not second.unchanged
    active = ws.active_files("s1")
    assert [p.name for p in active] == ["成本.xlsx"]
    assert active[0].read_text("utf-8") == "新"


def test_old_version_is_kept_in_history(ws):
    ws.keep("成本.xlsx", _blob("旧"), "s1", by="张三")
    ws.keep("成本.xlsx", _blob("新"), "s1", by="李四")
    rows = ws.submissions("s1")
    assert rows[0]["versions"] == 2
    assert rows[0]["by"] == "李四"


def test_active_files_are_per_store(ws):
    ws.keep("成本.xlsx", _blob("a"), "s1")
    ws.keep("成本.xlsx", _blob("b"), "s2")
    assert ws.active_files("s1")[0].read_text("utf-8") == "a"
    assert ws.active_files("s2")[0].read_text("utf-8") == "b"


def test_forget_removes_from_calculation_but_keeps_bytes(ws):
    kept = ws.keep("成本.xlsx", _blob("a"), "s1")
    ws.forget("s1", "成本.xlsx")
    assert ws.active_files("s1") == []
    assert ws.path_of(kept.sha).exists()


def test_empty_name_refused(ws):
    with pytest.raises(WorkspaceError):
        ws.keep("", _blob("a"), "s1")


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #


def _result(can_close: bool = True, **extra):
    return {"can_close": can_close, "findings": [], "missing_sources": [], **extra}


def test_record_creates_open_period(ws):
    ws.record("s1", "2025-05", _result(), ["sha1"])
    state = ws.state("s1", "2025-05")
    assert state is not None
    assert state.state == OPEN
    assert state.result is not None


def test_every_run_says_which_engine_computed_it(ws):
    """没有这个印，「回滚」是句空话。

    改坏了不一定报错，常见的形态是某家店某个月的数字悄悄变了。发现的时候第一个
    问题是「这个数是哪一版算的」——快照可以是几周前的，光看提交时间只能靠猜。
    """
    from ledger.version import engine_version

    ws.record("s1", "2025-05", _result(), ["a"])
    assert ws.state("s1", "2025-05").engine == engine_version()


def test_the_engine_stamp_stays_out_of_the_numbers(ws):
    """版本印不许进 result。

    result 是回放逐字段比对的那份东西。版本进去之后，每提交一次基线就整份变，
    于是人开始习惯性地重录基线——门槛当天就废了。
    """
    ws.record("s1", "2025-05", _result(), ["a"])
    st = ws.state("s1", "2025-05")
    assert st.engine
    assert "engine" not in st.result


def test_latest_snapshot_wins_while_open(ws):
    ws.record("s1", "2025-05", _result(profit=1), ["a"])
    ws.record("s1", "2025-05", _result(profit=2), ["a"])
    assert ws.state("s1", "2025-05").result["profit"] == 2


def test_open_period_points_at_the_run_it_shows(ws):
    """进行中的账期也要给出 run_id，否则下钻取不到明细。"""
    first = ws.record("s1", "2025-05", _result(), ["a"])
    assert ws.state("s1", "2025-05").run_id == first
    second = ws.record("s1", "2025-05", _result(), ["a"])
    assert ws.state("s1", "2025-05").run_id == second


def test_closed_period_points_at_the_frozen_run(ws):
    frozen = ws.record("s1", "2025-05", _result(), ["a"])
    ws.close_period("s1", "2025-05")
    ws.record("s1", "2025-05", _result(), ["a"])
    assert ws.state("s1", "2025-05").run_id == frozen


def test_history_lists_every_run(ws):
    ws.record("s1", "2025-05", _result(), ["a"])
    ws.record("s1", "2025-05", _result(False), ["a"])
    assert len(ws.history("s1", "2025-05")) == 2


def test_evidence_failure_is_visible_and_blocks_close(ws):
    run_id = ws.record("s1", "2025-05", _result(), ["a"], evidence_ready=False)
    ws.mark_evidence(run_id, ready=False, error="磁盘已满")
    state = ws.state("s1", "2025-05")
    assert state.result["can_close"] is False
    assert any(f["id"] == "evidence_archive" for f in state.result["findings"])
    with pytest.raises(WorkspaceError, match="磁盘已满"):
        ws.close_period("s1", "2025-05")


def test_pending_evidence_blocks_close(ws):
    ws.record("s1", "2025-05", _result(), ["a"], evidence_ready=False)
    with pytest.raises(WorkspaceError, match="证据"):
        ws.close_period("s1", "2025-05")


def test_parquet_write_error_is_recorded_not_swallowed(ws):
    class BrokenFacts:
        def is_empty(self):
            return False

        def write_parquet(self, _path):
            raise OSError("只读文件系统")

    run_id = ws.record("s1", "2025-05", _result(), ["a"], evidence_ready=False)
    _keep_facts(ws, run_id, type("SliceStub", (), {"facts": BrokenFacts()})())
    run = ws.latest_run("s1", "2025-05")
    assert run["evidence_ready"] == 0
    assert "只读文件系统" in run["evidence_error"]


# --------------------------------------------------------------------------- #
# 账期
# --------------------------------------------------------------------------- #


def test_cannot_close_what_engine_refuses(ws):
    ws.record("s1", "2025-05", _result(
        False, findings=[{"blocking": True, "passed": False, "message": "商品成本只覆盖 62%"}]
    ), ["a"])
    with pytest.raises(WorkspaceError, match="覆盖 62%"):
        ws.close_period("s1", "2025-05")


def test_missing_sources_explain_refusal(ws):
    ws.record("s1", "2025-05", _result(False, missing_sources=["运费表"]), ["a"])
    with pytest.raises(WorkspaceError, match="运费表"):
        ws.close_period("s1", "2025-05")


def test_cannot_close_before_any_run(ws):
    with pytest.raises(WorkspaceError, match="还没算过账"):
        ws.close_period("s1", "2025-05")


def test_close_freezes_the_numbers(ws):
    """结账后模型改了、字典补了，已结的账不能跟着变。"""
    ws.record("s1", "2025-05", _result(profit=100), ["a"])
    ws.close_period("s1", "2025-05", by="王五")
    ws.record("s1", "2025-05", _result(profit=999), ["a"])
    state = ws.state("s1", "2025-05")
    assert state.closed
    assert state.result["profit"] == 100


def test_reopen_needs_a_reason(ws):
    ws.record("s1", "2025-05", _result(), ["a"])
    ws.close_period("s1", "2025-05")
    with pytest.raises(WorkspaceError, match="原因"):
        ws.reopen_period("s1", "2025-05", note="  ")


def test_reopen_returns_to_latest(ws):
    ws.record("s1", "2025-05", _result(profit=100), ["a"])
    ws.close_period("s1", "2025-05")
    ws.record("s1", "2025-05", _result(profit=999), ["a"])
    ws.reopen_period("s1", "2025-05", by="王五", note="退款漏了")
    state = ws.state("s1", "2025-05")
    assert state.state == OPEN
    assert state.result["profit"] == 999
    assert state.note == "退款漏了"


def test_reopen_what_is_not_closed_is_refused(ws):
    ws.record("s1", "2025-05", _result(), ["a"])
    with pytest.raises(WorkspaceError, match="本来就没结账"):
        ws.reopen_period("s1", "2025-05", note="随便")


def test_new_data_after_close_is_flagged_not_applied(ws):
    """账已经报出去了，系统不能自己重算。挂旗子让人决定要不要反结账。"""
    ws.keep("成本.xlsx", _blob("旧"), "s1")
    ws.record("s1", "2025-05", _result(), ["a"])
    ws.close_period("s1", "2025-05")
    assert not ws.state("s1", "2025-05").stale
    ws.keep("成本.xlsx", _blob("新"), "s1")
    assert ws.state("s1", "2025-05").stale


def test_overview_covers_every_store_period(ws):
    ws.record("s1", "2025-05", _result(), ["a"])
    ws.record("s1", "2025-06", _result(False), ["a"])
    ws.record("s2", "2025-06", _result(), ["a"])
    grid = ws.overview()
    assert {(g.store_id, g.period) for g in grid} == {
        ("s1", "2025-05"), ("s1", "2025-06"), ("s2", "2025-06")
    }
    assert grid[0].period == "2025-06"  # 最近的账期排在前面


def test_state_of_unknown_period_is_none(ws):
    assert ws.state("s1", "2099-01") is None


def test_reopen_then_close_again_uses_new_numbers(ws):
    ws.record("s1", "2025-05", _result(profit=100), ["a"])
    ws.close_period("s1", "2025-05")
    ws.reopen_period("s1", "2025-05", note="补数据")
    ws.record("s1", "2025-05", _result(profit=200), ["a"])
    ws.close_period("s1", "2025-05")
    assert ws.state("s1", "2025-05").result["profit"] == 200


def test_several_requests_can_read_at_once(ws):
    """界面一进页面就并发拉三份数据，它们落在不同的线程上。

    一条 sqlite 连接给多个线程共用会撞出 `bad parameter or other API misuse`——
    不是「数据库忙」那种能重试的错，是直接 500。老界面一个请求接一个请求发，
    把这个坑盖了好几个月。
    """
    import threading

    for i in range(6):
        ws.record(f"s{i}", "2025-05", _result(), ["a"])

    seen: list[int] = []
    boom: list[Exception] = []

    def read() -> None:
        try:
            for _ in range(20):
                seen.append(len(ws.overview()))
        except Exception as exc:  # noqa: BLE001 - 要的就是把它捞出来看
            boom.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not boom, f"并发读崩了：{boom[0]!r}"
    assert set(seen) == {6}
