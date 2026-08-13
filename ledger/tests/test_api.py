"""HTTP 接口。

最要紧的一条是文件名必须原样保留。店铺归属和数据源识别全靠文件名——交上来的
文件叫「聚水潭成本-淘宝喜必顺.xlsx」，破折号前是类别、后面是店铺。换成随机名存盘，
这两件事立刻全瞎，而且不会报错，只会算出一张空账。

所有测试都跑在临时工作区里。接口现在真的会留档，冲默认目录等于让测试往用户的
账本里写垃圾。
"""

from __future__ import annotations

import io
import shutil

import openpyxl
import pytest
from fastapi.testclient import TestClient

import ledger.api as api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "WORKSPACE_ROOT", tmp_path / "space")
    monkeypatch.setattr(api, "_ws", None)
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把模型复制到临时目录再改。

    改配置的接口是真的写文件，直接冲仓库里那份等于让测试改坏项目自己的模型。
    """
    target = tmp_path / "cn-ecommerce"
    shutil.copytree(api.DEFAULT_MODEL, target)
    monkeypatch.setattr(api, "DEFAULT_MODEL", target)
    return target


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
    return client.post("/api/upload", files=files)


# --------------------------------------------------------------------------- #
# 启动信息
# --------------------------------------------------------------------------- #


class TestBootstrap:
    def test_gives_the_ui_everything_it_needs_at_once(self, client):
        """分开取的话，中间有人改了配置，界面会拿半新半旧的结构去渲染。"""
        body = client.get("/api/bootstrap").json()
        assert {"stores", "platforms", "editable", "statement", "sources", "accepts"} <= set(body)
        assert any(s["name"] == "淘宝喜必顺" for s in body["stores"])
        assert any(n["headline"] == "profit" for n in body["statement"]), \
            "总览上放哪个数由模型说，界面不该写死节点 id"

    def test_says_what_files_it_accepts(self, client):
        """界面上的「支持哪些格式」不该由前端写死。"""
        assert ".xlsx" in client.get("/api/bootstrap").json()["accepts"]


# --------------------------------------------------------------------------- #
# 交表
# --------------------------------------------------------------------------- #


class TestUpload:
    def test_filename_decides_the_store(self, client):
        data = _xlsx_bytes([["订单号", "金额"], ["A001", 1]])
        body = _upload(client, ("运费-淘宝喜必顺.xlsx", data)).json()
        assert body["rejected"] == [], "文件名带着店名，不该认不出"
        assert body["kept"][0]["store_id"] == "taobao_xibishun"

    def test_unknown_store_is_refused_with_a_suggestion(self, client):
        """绝不塞进某家店凑数——那会把一家店的钱记到另一家头上，而且没人会发现。"""
        data = _xlsx_bytes([["订单号", "金额"], ["A001", 1]])
        body = _upload(client, ("运费-拼多多某个新店.xlsx", data)).json()
        assert len(body["rejected"]) == 1
        bad = body["rejected"][0]
        assert bad["file"] == "运费-拼多多某个新店.xlsx"
        assert bad["suggest"]["store"] == "拼多多某个新店"
        assert bad["suggest"]["platform"] == "pdd", "平台前缀认得出来就该提示"

    def test_unsupported_format_is_refused_not_ignored(self, client):
        """不认识的格式要明说，不能悄悄丢掉让人以为已经算进去了。"""
        files = [("files", ("说明.docx", io.BytesIO(b"x"), "application/octet-stream"))]
        body = client.post("/api/upload", files=files).json()
        assert body["rejected"][0]["file"] == "说明.docx"
        assert "解析" in body["rejected"][0]["why"]
        assert body["kept"] == []

    def test_path_in_filename_cannot_escape(self, client):
        data = _xlsx_bytes([["订单号"], ["A001"]])
        body = _upload(client, ("../../etc/运费-淘宝喜必顺.xlsx", data)).json()
        assert body["rejected"] == []
        assert body["kept"][0]["file"] == "运费-淘宝喜必顺.xlsx"

    def test_no_files_at_all_is_an_error(self, client):
        assert client.post("/api/upload", files=[]).status_code == 422

    def test_reupload_says_nothing_changed(self, client):
        """重复交同一份表是常事。说「和上次一样」比说「已上传」有用。"""
        data = _xlsx_bytes([["订单号"], ["A001"]])
        _upload(client, ("运费-淘宝喜必顺.xlsx", data))
        body = _upload(client, ("运费-淘宝喜必顺.xlsx", data)).json()
        assert body["kept"][0]["unchanged"] is True

    def test_same_name_new_content_replaces(self, client):
        """店长改数重导出，文件名不变。两版都算就是双份成本。"""
        _upload(client, ("运费-淘宝喜必顺.xlsx", _xlsx_bytes([["订单号"], ["A001"]])))
        body = _upload(client, ("运费-淘宝喜必顺.xlsx", _xlsx_bytes([["订单号"], ["A002"]]))).json()
        assert body["kept"][0]["replaced"] is True
        files = client.get("/api/stores/taobao_xibishun").json()["files"]
        assert len(files) == 1 and files[0]["versions"] == 2

    def test_summary_is_a_human_sentence(self, client):
        data = _xlsx_bytes([["订单号"], ["A001"]])
        body = _upload(client, ("运费-淘宝喜必顺.xlsx", data)).json()
        assert "收下" in body["summary"]


class TestDropFile:
    def test_撤表_after_upload(self, client):
        data = _xlsx_bytes([["订单号"], ["A001"]])
        _upload(client, ("运费-淘宝喜必顺.xlsx", data))
        res = client.delete("/api/stores/taobao_xibishun/files",
                            params={"name": "运费-淘宝喜必顺.xlsx"})
        assert res.status_code == 200
        assert client.get("/api/stores/taobao_xibishun").json()["files"] == []

    def test_unknown_store_is_404(self, client):
        res = client.delete("/api/stores/没这家店/files", params={"name": "x.xlsx"})
        assert res.status_code == 404


# --------------------------------------------------------------------------- #
# 看账
# --------------------------------------------------------------------------- #


class TestOverview:
    def test_empty_workspace_is_not_an_error(self, client):
        """一家店都还没交表时，首页要能正常打开并告诉人下一步做什么。"""
        body = client.get("/api/overview").json()
        assert body["cells"] == []
        assert body["stores"], "还没数据也要把已登记的店列出来"

    def test_lists_stores_and_periods(self, client):
        data = _xlsx_bytes([["订单号"], ["A001"]])
        _upload(client, ("运费-淘宝喜必顺.xlsx", data))
        body = client.get("/api/overview").json()
        # 这份表算不出账期也没关系，重点是矩阵结构成立。
        assert isinstance(body["periods"], list)
        assert isinstance(body["totals"], list)


class TestStoreDetail:
    def test_unknown_store_is_404(self, client):
        assert client.get("/api/stores/没这家店").status_code == 404

    def test_lists_files_and_periods(self, client):
        data = _xlsx_bytes([["订单号"], ["A001"]])
        _upload(client, ("运费-淘宝喜必顺.xlsx", data))
        body = client.get("/api/stores/taobao_xibishun").json()
        assert body["store"]["name"] == "淘宝喜必顺"
        assert [f["name"] for f in body["files"]] == ["运费-淘宝喜必顺.xlsx"]

    def test_period_never_computed_is_404(self, client):
        assert client.get("/api/stores/taobao_xibishun/periods/2099-01").status_code == 404


class TestPeriodActions:
    def test_cannot_close_a_period_that_was_never_computed(self, client):
        res = client.post("/api/stores/taobao_xibishun/periods/2099-01/close", json={})
        assert res.status_code == 409
        assert "还没算过账" in res.json()["detail"]

    def test_reopen_needs_a_reason(self, client):
        res = client.post("/api/stores/taobao_xibishun/periods/2099-01/reopen", json={})
        assert res.status_code == 409


class TestDrill:
    def test_missing_facts_says_recompute(self, client):
        res = client.get("/api/runs/9999/drill/profit")
        assert res.status_code == 404
        assert "重算" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestStoresEndpoint:
    def test_lists_registered_stores(self, client):
        res = client.get("/api/stores")
        assert res.status_code == 200
        assert "淘宝喜必顺" in {s["name"] for s in res.json()["stores"]}

    def test_exposes_entity_so_ui_can_flag_missing(self, client):
        stores = client.get("/api/stores").json()["stores"]
        assert all("entity" in s for s in stores)

    def test_tells_ui_what_is_editable(self, client):
        """哪些字段能改由后端说，界面别自己猜——猜错了就会渲染出一个改不动的输入框。"""
        body = client.get("/api/stores").json()
        assert "entity" in body["editable"]
        assert "id" not in body["editable"] and "name" not in body["editable"]
        # 平台带上中文名：下拉框里显示 `alibaba1688` 没人认得那是哪个平台。
        options = {p["id"]: p["name"] for p in body["platforms"]}
        assert options["taobao"] == "淘宝天猫"


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


class TestOnboardAssist:
    """模型建议走单独一个端点，而且它坏掉不能影响向导。

    这两条是同一件事的两面：分开是为了人先看到确定性那份、看得出模型动了哪里；
    坏掉不影响，是因为向导本来就不靠它——模型是加分项，不是依赖。
    """

    def _unknown(self, client):
        """交一张谁也认不出的表，拿它的内容哈希。"""
        data = _xlsx_bytes([
            ["莫名其妙的列", "另一列", "第三列"],
            ["a", "1", "2025-05-01"],
            ["b", "2", "2025-05-02"],
        ])
        res = _upload(client, ("推广-淘宝喜必顺.xlsx", data))
        tables = res.json()["unknown_tables"]
        assert tables, "前提：这张表确实没被认出来"
        return tables[0]["sha"]

    def test_the_draft_endpoint_does_not_wait_for_the_model(self, client, monkeypatch):
        """规则草案这一条路上，一个出站请求都不许有。

        向导要在零点几秒内打开。模型是可以关掉、可以超时、可以答十几秒的东西，
        让它挡在向导前面，等于把「能不能接表」交给一个不归自己管的服务。
        """
        from ledger import assist

        def boom(*a, **kw):
            raise AssertionError("规则草案这条路不该碰模型")

        monkeypatch.setattr(assist.urllib.request, "urlopen", boom)
        res = client.get(f"/api/onboard/{self._unknown(client)}")
        assert res.status_code == 200
        assert res.json()["columns"]

    def test_no_model_configured_is_a_normal_answer(self, client, monkeypatch):
        """没配模型不是错误。返回 200，界面上安静地什么都不显示。"""
        from ledger import assist

        monkeypatch.setattr(assist, "load_config", lambda root=None: assist.Config())
        res = client.get(f"/api/onboard/{self._unknown(client)}/assist")
        assert res.status_code == 200
        body = res.json()
        assert body["assist"]["ok"] is False
        assert body["columns"], "模型没说话，规则那份照样给"

    def test_a_broken_model_still_returns_the_rule_draft(self, client, monkeypatch):
        from ledger import assist

        monkeypatch.setattr(
            assist, "load_config",
            lambda root=None: assist.Config(base_url="https://x/v1", model="m", api_key="k"),
        )

        def boom(*a, **kw):
            raise TimeoutError("模型没理我")

        monkeypatch.setattr(assist.urllib.request, "urlopen", boom)
        res = client.get(f"/api/onboard/{self._unknown(client)}/assist")
        assert res.status_code == 200, "模型超时不是 500——向导没坏，只是这次没建议"
        assert res.json()["assist"]["ok"] is False
        assert res.json()["columns"]


class TestPage:
    def test_serves_the_page(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert 'id="app"' in res.text

    def test_serves_the_assets(self, client):
        """页面引用的每个文件都得真的能取到，少一个就是白屏。"""
        import re

        refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', client.get("/").text)
        assert refs, "页面一个资源都没引用"
        for ref in refs:
            assert client.get(ref).status_code == 200, f"{ref} 取不到"
