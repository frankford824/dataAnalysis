"""提成配置的自动带出：从「谁拿几个点」到逐商品的配置表。

配置提成这件事以前要人对着几百个商品 id 打人名，所以它永远排不上队，提成表
一直是空的。这两个接口把它变成「系统猜、人否决」。

猜错的代价必须是「人改一行」，不能是「钱算错」。所以这里盯三件事：
建议只是建议（不进计算）、展开出来的配置和人心里想的那份等价、
以及重复展开不会把旧的生效版本冲掉。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ledger.api as api
from ledger import ownership

STORE = "taobao_xibishun"


@dataclass
class _State:
    """工作区里一条算完的店期。只带这几个接口会看的字段。"""

    store_id: str
    period: str
    result: dict[str, Any]


class _Workspace:
    def __init__(self, states: list[_State]) -> None:
        self._states = states

    def overview(self) -> list[_State]:
        return self._states


def _product(pid: str, base: float, **kw) -> dict:
    return {"product_id": pid, "product_name": "", "base": base, "sub_orders": 1,
            "amount": 0.0, "total_rate": 0.0, "fallback": False, "unassigned": True,
            **kw}


@pytest.fixture
def model(tmp_path, monkeypatch):
    """模型副本 + 一份小的归属表。

    仓库里那份真的归属表两千四百万字节，每条测试拷一遍太贵；而且拿真数据当断言
    的依据，数据一更新测试就红，红了还看不出是代码错还是数据变了。
    """
    target = tmp_path / "cn-ecommerce"
    shutil.copytree(api.DEFAULT_MODEL, target)
    (target / ownership.FILENAME).write_text(
        "product_id,period,owner,store\n"
        "P1,2026-03,汪学成,喜必顺\n"
        "P2,2026-03,汪学成,喜必顺\n"
        "P3,2026-03,张三,喜必顺\n",
        encoding="utf-8",
    )
    ownership._cache.pop(target, None)
    monkeypatch.setattr(api, "DEFAULT_MODEL", target)
    return target


@pytest.fixture
def client(model, monkeypatch):
    """接口跑在假工作区上：四个商品，三个查得到归属，一个查不到。"""
    products = [_product("P1", 1000.0), _product("P2", 500.0),
                _product("P3", 300.0), _product("P9", 200.0)]
    ws = _Workspace([_State(STORE, "2026-05",
                            {"commission": {"products": products, "base_total": 2000.0}})])
    monkeypatch.setattr(api, "workspace", lambda: ws)
    with TestClient(api.app) as c:
        yield c


# --------------------------------------------------------------------------- #
# 建议
# --------------------------------------------------------------------------- #

class TestWhatTheSystemSuggests:
    def test_fills_in_the_owner_it_knows(self, client) -> None:
        got = client.get(f"/api/commission/products?store_id={STORE}").json()
        by_id = {p["product_id"]: p for p in got["products"]}
        assert by_id["P1"]["suggest_person"] == "汪学成"
        assert by_id["P3"]["suggest_person"] == "张三"

    def test_leaves_the_ones_it_does_not_know_blank(self, client) -> None:
        """猜不到就空着。这一格宁可让人填，也不能填一个看起来像那么回事的名字。"""
        got = client.get(f"/api/commission/products?store_id={STORE}").json()
        by_id = {p["product_id"]: p for p in got["products"]}
        assert by_id["P9"]["suggest_person"] == ""

    def test_says_how_old_the_suggestion_is(self, client) -> None:
        """建议要带出处。归属数据停在三月，账期是五月，界面上得说得出这是沿用的。"""
        got = client.get(f"/api/commission/products?store_id={STORE}").json()
        assert got["ownership_latest"] == "2026-03"
        assert all(p["suggest_since"] == "2026-03"
                   for p in got["products"] if p["suggest_person"])

    def test_sorts_by_money_not_by_id(self, client) -> None:
        """按毛利从大到小。人的时间有限，先看的那几行该是钱最多的那几行。"""
        got = client.get(f"/api/commission/products?store_id={STORE}").json()
        assert [p["product_id"] for p in got["products"]] == ["P1", "P2", "P3", "P9"]

    def test_groups_the_money_by_person(self, client) -> None:
        """按人汇总毛利，让人一眼看出这份建议把多少钱分给了谁。"""
        store = client.get(f"/api/commission/products?store_id={STORE}").json()["stores"][0]
        assert {o["person"]: o["base"] for o in store["owners"]} == {
            "汪学成": 1500.0, "张三": 300.0, "": 200.0}


# --------------------------------------------------------------------------- #
# 展开
# --------------------------------------------------------------------------- #

def _plan(client, *, apply: bool = False, **body) -> dict:
    body.setdefault("store_id", STORE)
    body.setdefault("period", "2026-05")
    r = client.post(f"/api/commission/plan?apply={str(apply).lower()}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestExpandingAPlan:
    def test_one_rate_per_person_becomes_one_row_per_product(self, client) -> None:
        got = _plan(client, rates={"汪学成": 0.03, "张三": 0.05})
        assert got["coverage"] == {"by_product": 3, "by_store": 0, "nobody": 1}
        assert {(r["product_id"], r["person"], r["share"]) for r in got["preview"]} == {
            ("P1", "汪学成", "0.03"), ("P2", "汪学成", "0.03"), ("P3", "张三", "0.05")}

    def test_collapses_products_that_the_store_row_already_covers(self, client) -> None:
        """负责人和兜底同人同率时不单独出行，交给店铺那一行盖住。

        算出来的钱一分不差，但配置从几百行变成一行。淘宝那家店 722 个商品全归
        一个人，不压缩就是 722 行——那张表没人看得懂，也就没人会去审。
        """
        got = _plan(client, rates={"汪学成": 0.03},
                    fallback_person="汪学成", fallback_rate=0.03)
        assert got["generated"] == 1
        assert got["preview"][0]["product_id"] == ""
        assert got["coverage"]["by_store"] == 4

    def test_a_person_on_a_different_rate_still_gets_their_own_row(self, client) -> None:
        """费率和兜底不一样的人必须单独出行，不能被压掉。"""
        got = _plan(client, rates={"汪学成": 0.03, "张三": 0.05},
                    fallback_person="汪学成", fallback_rate=0.03)
        assert [(r["product_id"], r["person"]) for r in got["preview"]] == [
            ("P3", "张三"), ("", "汪学成")]

    def test_no_fallback_means_unowned_products_pay_nobody(self, client) -> None:
        """不设兜底，查不到归属的商品就是不给人——不能悄悄挂到别人名下。"""
        got = _plan(client, rates={"汪学成": 0.03, "张三": 0.05})
        assert got["coverage"]["nobody"] == 1
        assert all(r["product_id"] != "P9" for r in got["preview"])

    def test_a_person_with_no_rate_is_not_paid(self, client) -> None:
        """建议里有这个人，但没给他定率，就不该生成配置。

        这种商品和「压根查不到归属」一起计入 nobody：两者对钱的影响一样，
        都是这份毛利不产生提成，界面上要提醒的也是同一句话。
        """
        got = _plan(client, rates={"汪学成": 0.03})
        assert all(r["person"] != "张三" for r in got["preview"])
        assert got["coverage"]["nobody"] == 2


class TestOverridingWhoOwnsAProduct:
    """归属表认不出新来的人，也认不出这个月刚交接的商品。

    没有这个口子，界面上那一页只对老人有用：新人要拿到提成，只能有人去手改
    commission.csv——而这一页存在的全部理由就是不用去改那个文件。
    """

    def test_a_named_owner_beats_the_suggestion(self, client) -> None:
        got = _plan(client, rates={"李四": 0.04}, owners={"P1": "李四"})
        assert ("P1", "李四", "0.04") in {
            (r["product_id"], r["person"], r["share"]) for r in got["preview"]
        }

    def test_the_override_says_where_it_came_from(self, client) -> None:
        """备注里要看得出这行是人指的还是系统猜的。半年后回头查账靠的就是这一列。"""
        got = _plan(client, rates={"李四": 0.04}, owners={"P1": "李四"})
        row = next(r for r in got["preview"] if r["product_id"] == "P1")
        assert row["note"] == "人工指定"

    def test_a_dash_leaves_the_operator_slot_empty(self, client) -> None:
        """指成「没人管」的商品不单独出行，跟着店铺那一组走。"""
        got = _plan(client, rates={"汪学成": 0.03}, owners={"P1": "-"})
        assert all(r["product_id"] != "P1" for r in got["preview"])
        assert got["coverage"]["nobody"] == 3


class TestSplittingOneRateAmongSeveralPeople:
    """一个商品的总提成率是定死的，再分给几个人——淘宝那家店就是运营 3.5、主管 1.5。

    分法只有两种角色：运营（谁管这个商品谁拿）和固定分成（每个商品都分一份）。
    只支持「兜底一个人」的话，这种分法根本表达不出来，界面把现状显示成一个人，
    人随手一存主管那 1.5 个点就没了——而少发的那个人要到发工资那天才会发现。
    """

    TAOBAO = {"rates": {"汪学成": 0.035}, "fixed": {"李秋雨": 0.015},
              "fallback_owner": "汪学成"}

    def test_the_store_row_is_the_two_of_them(self, client) -> None:
        got = _plan(client, **self.TAOBAO)
        store_rows = [r for r in got["preview"] if not r["product_id"]]
        assert {(r["person"], r["share"]) for r in store_rows} == {
            ("汪学成", "0.035"), ("李秋雨", "0.015")}

    def test_the_group_total_is_what_they_add_up_to(self, client) -> None:
        """同组每条写同一个总提成率，加载时校验组内相加等于它。写各自的点数会加载失败。"""
        got = _plan(client, **self.TAOBAO)
        store_rows = [r for r in got["preview"] if not r["product_id"]]
        assert {r["total_rate"] for r in store_rows} == {"0.05"}

    def test_products_that_follow_the_default_are_not_split_off(self, client) -> None:
        """运营和点数都跟店铺那份一致的商品不单独出行。

        淘宝那家店 627 个商品都是这种情况。不压掉的话，写出去的是 627 行——
        而且一旦漏写了主管那一份，她在这些商品上的 1.5 个点就被抹掉了，
        配置看着还是对的。
        """
        got = _plan(client, **self.TAOBAO)
        assert got["coverage"]["by_store"] == 3
        assert all(r["product_id"] not in ("P1", "P2", "P9") for r in got["preview"])

    def test_a_product_nobody_owns_goes_to_the_fallback_operator(self, client) -> None:
        """归属里查不到的商品落到兜底运营身上——第四步说的就是这件事。

        不这么做的话，那些商品只发固定分成，运营那一格凭空少掉；淘宝那家店
        95 个长尾商品会各写一行「只给主管 1.5」，而人以为它们跟着店铺那一组走。
        """
        got = _plan(client, **self.TAOBAO)
        assert all(r["product_id"] != "P9" for r in got["preview"])

    def test_a_product_whose_owner_has_no_rate_still_needs_its_own_rows(self, client) -> None:
        """运营没定点数的商品必须单独写，不能让它落到店铺那一组。

        落过去的话，这个商品的 3.5 个点会发给店铺默认那个运营——一个不管这个
        商品的人。这是真会多发钱的一种错。
        """
        got = _plan(client, **self.TAOBAO)
        rows = [r for r in got["preview"] if r["product_id"] == "P3"]
        assert [(r["person"], r["share"]) for r in rows] == [("李秋雨", "0.015")]

    def test_handing_one_product_to_someone_else_keeps_the_fixed_share(self, client) -> None:
        """把某个商品交给别人，换的是运营那一格，固定分成那一格照旧。"""
        got = _plan(client, **self.TAOBAO | {"rates": {"汪学成": 0.035, "李四": 0.02},
                                             "owners": {"P1": "李四"}})
        rows = [r for r in got["preview"] if r["product_id"] == "P1"]
        assert {(r["person"], r["share"]) for r in rows} == {
            ("李四", "0.02"), ("李秋雨", "0.015")}
        assert {r["total_rate"] for r in rows} == {"0.035"}

    def test_a_product_with_no_owner_still_pays_the_fixed_share(self, client) -> None:
        """运营那一格空着，固定分成照发——总提成率就少了运营那一块。"""
        got = _plan(client, **self.TAOBAO | {"owners": {"P1": "-"}})
        rows = [r for r in got["preview"] if r["product_id"] == "P1"]
        assert [(r["person"], r["share"], r["total_rate"]) for r in rows] == [
            ("李秋雨", "0.015", "0.015")]

    def test_it_survives_a_round_trip_through_the_config(self, client, model, monkeypatch) -> None:
        monkeypatch.setattr(api.service, "recompute",
                            lambda *a, **k: type("R", (), {"periods": []})())
        _plan(client, **self.TAOBAO, apply=True)
        rules = client.get(f"/api/commission/config?store_id={STORE}").json()["rules"]
        store_level = [r for r in rules if not r["product_id"]]
        assert sorted(r["share"] for r in store_level) == [0.015, 0.035]


class TestPreviewDoesNotTouchAnything:
    def test_preview_writes_no_config(self, client, model) -> None:
        before = (model / "commission.csv").read_bytes()
        _plan(client, rates={"汪学成": 0.03})
        assert (model / "commission.csv").read_bytes() == before

    def test_preview_says_it_did_not_apply(self, client) -> None:
        assert _plan(client, rates={"汪学成": 0.03})["applied"] is False


class TestApplying:
    @pytest.fixture(autouse=True)
    def _no_recompute(self, monkeypatch):
        """落盘要测，重算不测——重算有它自己的测试，在这里跑只是让每条慢二十秒。"""
        monkeypatch.setattr(api.service, "recompute",
                            lambda *a, **k: type("R", (), {"periods": ["2026-05"]})())

    def test_written_rules_come_back_from_the_config(self, client, model) -> None:
        got = _plan(client, rates={"汪学成": 0.03}, apply=True)
        assert got["applied"] is True
        text = (model / "commission.csv").read_text(encoding="utf-8")
        assert "P1" in text and "汪学成" in text

    def test_running_it_twice_replaces_instead_of_doubling(self, client, model) -> None:
        """同一个生效日期重复展开是改主意，不是加一份。"""
        _plan(client, rates={"汪学成": 0.03}, apply=True)
        _plan(client, rates={"汪学成": 0.08}, apply=True)
        text = (model / "commission.csv").read_text(encoding="utf-8")
        assert text.count("P1") == 1
        assert "0.08" in text and "0.03" not in text

    def test_a_different_effective_date_is_kept_as_a_new_version(self, client, model) -> None:
        """提成是生效制的。改配置是往表里加一个新版本，旧版本必须留着——
        不然上个月的账重算一遍会套上这个月的规则，已经发过的钱对不上。
        """
        _plan(client, rates={"汪学成": 0.03}, effective_from="2026-01-01", apply=True)
        _plan(client, rates={"汪学成": 0.08}, effective_from="2026-05-01", apply=True)
        text = (model / "commission.csv").read_text(encoding="utf-8")
        assert "2026-01-01" in text and "2026-05-01" in text
        assert "0.03" in text and "0.08" in text

    def test_the_effective_date_is_required(self, client) -> None:
        """没有生效日期就不知道这份配置从哪天算起，宁可拒绝也不能默认成今天。"""
        r = client.post("/api/commission/plan",
                        json={"store_id": STORE, "rates": {"汪学成": 0.03}})
        assert r.status_code == 400
