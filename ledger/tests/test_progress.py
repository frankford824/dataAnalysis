"""交表进度。

界面上那个转圈只能证明「还没返回」，证明不了「还在干活」。人分不出这两件事的
时候会去刷新，而刷新之后这次交表的结果就再也看不见了——这就是这一层存在的
全部理由，所以它要保证的是：任何时候问它，它都说得出现在在干哪一步。
"""

from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

import ledger.api as api
from ledger import progress


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "WORKSPACE_ROOT", tmp_path / "space")
    monkeypatch.setattr(api, "_ws", None)
    with TestClient(api.app) as c:
        yield c


def _xlsx(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestTheLane:
    def test_an_unknown_token_reads_as_nothing(self) -> None:
        assert progress.read("没这个号") is None

    def test_it_says_which_step_and_how_far(self) -> None:
        progress.open("t1")
        progress.step("t1", "留档", 2, 5)
        got = progress.read("t1")
        assert (got["phase"], got["done"], got["total"]) == ("留档", 2, 5)
        progress.forget("t1")

    def test_the_same_step_reported_twice_stays_one_line(self) -> None:
        """一份份报留档不该在流水账里刷出五行「留档」。"""
        progress.open("t2")
        for i in range(1, 6):
            progress.step("t2", "留档", i, 5)
        progress.step("t2", "读表")
        assert progress.read("t2")["trail"] == ["正在收表", "留档"]
        progress.forget("t2")

    def test_closing_marks_it_finished(self) -> None:
        progress.open("t3")
        progress.step("t3", "读表")
        progress.close("t3")
        got = progress.read("t3")
        assert got["finished"] and got["trail"] == ["正在收表", "读表"]
        progress.forget("t3")

    def test_reporting_after_close_changes_nothing(self) -> None:
        """请求已经返回了，界面最后一次轮询不该把状态倒退回「正在读表」。"""
        progress.open("t4")
        progress.close("t4")
        progress.step("t4", "读表")
        assert progress.read("t4")["phase"] == "算完了"
        progress.forget("t4")

    def test_the_silent_one_swallows_everything(self) -> None:
        """命令行和测试不带号调用，这一层不能因此炸掉。"""
        progress.SILENT("读表", 1, 2)
        progress.step("", "读表")
        assert progress.read("") is None


class TestWhileTakingFiles:
    ROWS = [["订单号", "商品ID", "金额"], ["A1", "P1", "100"]]

    def test_it_walks_through_the_real_steps(self, client) -> None:
        """交完表去问那个号，能看到它走过留档、读表、核算、存账期。"""
        client.post(
            "/api/upload?token=live",
            files=[("files", ("订单明细-淘宝喜必顺-2026-05.xlsx", io.BytesIO(_xlsx(self.ROWS)),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        )
        got = client.get("/api/upload/progress/live").json()
        assert got["finished"]
        walked = " ".join([*got["trail"], got["phase"]])
        assert "留档" in walked
        assert "读表" in walked

    def test_asking_about_a_token_nobody_used_is_not_an_error(self, client) -> None:
        """界面轮询可能比请求先到，也可能服务重启过。这种时候不该弹错误。"""
        got = client.get("/api/upload/progress/野号").json()
        assert got["finished"] and got["unknown"]

    def test_it_names_the_store_being_recomputed(self, client) -> None:
        """交一批表牵动几家店时，人要知道现在算的是哪家。"""
        client.post(
            "/api/upload?token=named",
            files=[("files", ("订单明细-淘宝喜必顺-2026-05.xlsx", io.BytesIO(_xlsx(self.ROWS)),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        )
        walked = " ".join(client.get("/api/upload/progress/named").json()["trail"])
        assert "淘宝喜必顺" in walked
