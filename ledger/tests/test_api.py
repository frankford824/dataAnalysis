"""上传接口。

最要紧的一条是文件名必须原样保留。店铺归属和数据源识别全靠文件名——交上来的
文件叫「聚水潭成本-淘宝喜必顺.xlsx」，破折号前是类别、后面是店铺。换成随机名存盘，
这两件事立刻全瞎，而且不会报错，只会算出一张空账。
"""

from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from ledger.api import app


@pytest.fixture
def client():
    return TestClient(app)


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, *named: tuple[str, bytes]):
    files = [("files", (name, io.BytesIO(data),
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
             for name, data in named]
    return client.post("/api/run", files=files)


class TestStoresEndpoint:
    def test_lists_registered_stores(self, client):
        res = client.get("/api/stores")
        assert res.status_code == 200
        names = {s["name"] for s in res.json()["stores"]}
        assert "淘宝喜必顺" in names

    def test_exposes_entity_so_ui_can_flag_missing(self, client):
        """主体是否配了要能看出来，界面才好提示补配。"""
        stores = client.get("/api/stores").json()["stores"]
        assert all("entity" in s for s in stores)


class TestPage:
    def test_serves_the_page(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "把文件拖到这里" in res.text


class TestUpload:
    def test_filename_decides_the_store(self, client):
        """认哪家店只看文件名。这里的表内容是空的，重点是它被归给了对的店。"""
        data = _xlsx_bytes([["订单号", "金额"], ["A001", 1]])
        res = _upload(client, ("运费-淘宝喜必顺.xlsx", data))
        assert res.status_code == 200
        body = res.json()
        assert body["orphans"] == [], "文件名带着店名，不该认不出"

    def test_unknown_store_becomes_orphan_with_a_suggestion(self, client):
        """认不出的文件要列出来，还要给个能直接照着登记的建议。

        绝不塞进某家店凑数——那会把一家店的钱记到另一家头上，而且没人会发现。
        """
        data = _xlsx_bytes([["订单号", "金额"], ["A001", 1]])
        res = _upload(client, ("运费-拼多多某个新店.xlsx", data))
        body = res.json()
        assert len(body["orphans"]) == 1
        orphan = body["orphans"][0]
        assert orphan["file"] == "运费-拼多多某个新店.xlsx"
        assert orphan["suggest"]["store"] == "拼多多某个新店"
        assert orphan["suggest"]["platform"] == "pdd", "平台前缀认得出来就该提示"

    def test_unsupported_format_is_skipped_not_ignored(self, client):
        """不认识的格式要明说跳过了，不能悄悄丢掉让人以为已经算进去了。"""
        files = [("files", ("说明.docx", io.BytesIO(b"x"), "application/octet-stream"))]
        body = client.post("/api/run", files=files).json()
        assert body["skipped"] == ["说明.docx"]
        assert body["slices"] == []

    def test_path_in_filename_cannot_escape(self, client):
        """上传名里带路径的一律只取文件名，不许写到别处去。"""
        data = _xlsx_bytes([["订单号"], ["A001"]])
        res = _upload(client, ("../../etc/运费-淘宝喜必顺.xlsx", data))
        assert res.status_code == 200
        assert res.json()["orphans"] == []

    def test_nothing_uploaded_says_so(self, client):
        files = [("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))]
        body = client.post("/api/run", files=files).json()
        assert body["slices"] == []
        assert "没有能解析的文件" in body["message"]
