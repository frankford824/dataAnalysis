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

    def test_tells_ui_what_is_editable(self, client):
        """哪些字段能改由后端说，界面别自己猜——猜错了就会渲染出一个改不动的输入框。"""
        body = client.get("/api/stores").json()
        assert "entity" in body["editable"]
        assert "id" not in body["editable"] and "name" not in body["editable"]
        assert "taobao" in body["platforms"]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把模型复制到临时目录再改。

    改配置的接口是真的写文件，直接冲仓库里那份等于让测试改坏项目自己的模型。
    """
    import shutil

    import ledger.api as api

    target = tmp_path / "cn-ecommerce"
    shutil.copytree(api.DEFAULT_MODEL, target)
    monkeypatch.setattr(api, "DEFAULT_MODEL", target)
    return target


class TestEditStore:
    """法人主体这类东西数据里读不出来，只能由人配——那就必须能从界面配。

    支付宝和微信账单都不带主体信息，引擎读不到也不该猜。要人去改 YAML 才能配一家店，
    这就不是产品而是脚手架了。
    """

    def test_sets_entity(self, client, sandbox):
        res = client.patch("/api/stores/taobao_xibishun",
                           json={"entity": "某某电子商务有限公司"})
        assert res.status_code == 200
        assert res.json()["store"]["entity"] == "某某电子商务有限公司"
        again = client.get("/api/stores").json()["stores"]
        assert next(s for s in again if s["id"] == "taobao_xibishun")["entity"] \
            == "某某电子商务有限公司"

    def test_writes_through_to_the_model_file(self, client, sandbox):
        client.patch("/api/stores/taobao_xibishun", json={"entity": "某某电子商务有限公司"})
        text = (sandbox / "stores.yaml").read_text(encoding="utf-8")
        assert "某某电子商务有限公司" in text
        assert "# 店铺注册表。" in text, "注释是取证记录，不能被写回冲掉"

    def test_can_clear_it_again(self, client, sandbox):
        client.patch("/api/stores/taobao_xibishun", json={"entity": "填错了"})
        res = client.patch("/api/stores/taobao_xibishun", json={"entity": ""})
        assert res.json()["store"]["entity"] == ""

    def test_rejects_empty_patch(self, client, sandbox):
        assert client.patch("/api/stores/taobao_xibishun", json={}).status_code == 400

    def test_rejects_unknown_store(self, client, sandbox):
        res = client.patch("/api/stores/没这家店", json={"entity": "x"})
        assert res.status_code == 400

    def test_cannot_rename(self, client, sandbox):
        """name 是认文件的依据，改了以前交过的文件立刻认不出。"""
        res = client.patch("/api/stores/taobao_xibishun", json={"name": "新名字"})
        assert res.status_code == 400
        assert client.get("/api/stores").json()["stores"][0]["name"] == "淘宝喜必顺"

    def test_adds_a_store(self, client, sandbox):
        res = client.post("/api/stores", json={
            "id": "pdd_new", "name": "拼多多新店", "platform": "pdd",
        })
        assert res.status_code == 200
        ids = {s["id"] for s in client.get("/api/stores").json()["stores"]}
        assert "pdd_new" in ids

    def test_refuses_duplicate_name(self, client, sandbox):
        res = client.post("/api/stores", json={
            "id": "taobao_other", "name": "淘宝喜必顺", "platform": "taobao",
        })
        assert res.status_code == 400


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
