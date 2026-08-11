"""模型进产品的那道接缝。

模型在这套东西里只干一件事：接新表时建议某一列该映成哪个角色。这件事值得让它干——
真实语料上它比纯规则多认对 13 项，而且没有一次发明清单外的角色。

但「它平时表现好」不是能把它放进产品的理由。放进去的理由必须是：**它表现坏的时候，
坏不到账上。** 这批测试盯的就是这一句，逐条把「坏」演一遍：

    没配、断网、超时      向导照常打开，人拿到的是规则那份
    返回一坨不是 JSON     同上，一条异常都不许漏出去
    发明一个不存在的角色   挡掉，留痕
    一个角色塞两列         挡掉后来的
    抢规则已经占了的角色   挡掉，规则那份不动
    跟规则说的不一样       保留规则那份，冲突摆给人看
    一口气提议七十列       整批不采纳

还有一条不在「坏」里但同样要紧：**出站数据的边界**。订单号、手机号、买家昵称、
店名、金额合计一概不许出这台机器——它们对判断「这列是不是钱」毫无帮助，却是最不该
离开的东西。这条单独测，因为它错了不会有任何现象，只是数据已经出去了。
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest
from test_onboard import _model

from ledger import assist, onboard
from ledger.model.propose import propose, role_facts
from ledger.model.schema import ColumnBinding, Template

#: 早一版的推广模板。它在这里只为一件事：把 product_id、subject_type 这两个角色
#: 带进词汇表。角色清单是接表接出来的，清单里没有的角色模型提了也会被挡——
#: 那样测「规则和模型打架」就测不到打架，只测到越界。
_EARLIER = Template(
    id="promo_v0",
    source="promotion",
    name="推广更早的版本",
    match_columns=("日期", "花费", "主体ID"),
    bindings=(
        ColumnBinding(role="spend_time", columns=("日期",), kind="time"),
        ColumnBinding(role="spend", columns=("花费",), kind="number"),
        ColumnBinding(role="product_id", columns=("主体ID",)),
        ColumnBinding(role="subject_type", columns=("主体类型",)),
    ),
    time_slots={"spend_date": "spend_time"},
)


def _m():
    return _model(_EARLIER)


def _draft():
    """一张推广表：规则认得出日期和主体名称，认不出「消耗金额」。

    这个缺口是真的——真实的万相台改版表里，规则就是没认出消耗金额，
    而它是这张表上唯一进账的那一列。
    """
    headers = ["日期", "主体名称", "消耗金额", "点击率"]
    rows = [
        ["2025-05-01", "甲商品", "1200.50", "3.2%"],
        ["2025-05-02", "乙商品", "980.00", "2.8%"],
    ]
    return propose(headers, rows, _m(), source_hint="promotion")


def _vocab():
    return list(role_facts(_m(), "promotion").values())


def _reply(mappings):
    """装成一次 OpenAI 兼容的正常返回。"""
    return {"choices": [{"message": {"content": json.dumps({"mappings": mappings})}}]}


def _wire(monkeypatch, payload):
    """把出站那一下换成给定的返回。整条链路照跑，只是不真的发请求。

    换在 `urlopen` 这一层而不是换掉 `suggest_roles`，是因为要测的东西有一半在
    `_post` 和 `_validate` 里——换掉整个函数就等于把要测的东西一起换掉了。
    """
    sent: dict = {}

    def fake(request, timeout=None):
        sent["body"] = json.loads(request.data.decode("utf-8"))
        sent["url"] = request.full_url
        sent["headers"] = dict(request.headers)
        if isinstance(payload, Exception):
            raise payload
        raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        stream = io.BytesIO(raw.encode("utf-8") if isinstance(raw, str) else raw)
        stream.__enter__ = lambda: stream  # type: ignore[method-assign]
        stream.__exit__ = lambda *a: None  # type: ignore[method-assign]
        return stream

    monkeypatch.setattr(assist.urllib.request, "urlopen", fake)
    return sent


CONFIG = assist.Config(base_url="https://x/v1", model="m", api_key="sk-test-key")


class TestFailureIsJustNoAdvice:
    """模型出任何事，结果都只是「这次没有建议」。

    这一组是能把模型放进产品的直接理由。它们全都不测「模型说得对不对」，
    只测「模型不管怎么坏，向导照样能用、规则那份照样在」。
    """

    def test_no_config_changes_nothing(self):
        draft = _draft()
        before = [(c.index, c.role, c.confidence, c.why) for c in draft.columns]
        a = onboard.advise(draft, _m(), config=assist.Config())
        assert not a.ok
        assert "没有配置模型" in a.note
        assert [(c.index, c.role, c.confidence, c.why) for c in draft.columns] == before

    @pytest.mark.parametrize(
        "boom",
        [
            socket.timeout("timed out"),
            urllib.error.URLError("connection refused"),
            urllib.error.HTTPError("https://x/v1", 500, "boom", {}, None),  # type: ignore[arg-type]
            urllib.error.HTTPError("https://x/v1", 429, "rate limited", {}, None),  # type: ignore[arg-type]
        ],
    )
    def test_network_trouble_changes_nothing(self, monkeypatch, boom):
        _wire(monkeypatch, boom)
        draft = _draft()
        before = [(c.index, c.role, c.confidence) for c in draft.columns]
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert not a.ok, "模型没答上来就是没答上来，不能当成它同意了规则那份"
        assert [(c.index, c.role, c.confidence) for c in draft.columns] == before

    @pytest.mark.parametrize(
        "junk",
        [
            "这不是 JSON",
            {"choices": []},
            {"choices": [{"message": {"content": "{}"}}]},
            {"choices": [{"message": {"content": '{"mappings": "不是列表"}'}}]},
            {"choices": [{"message": {"content": '{"mappings": [42, null]}'}}]},
        ],
    )
    def test_garbage_changes_nothing(self, monkeypatch, junk):
        _wire(monkeypatch, junk)
        draft = _draft()
        before = [(c.index, c.role) for c in draft.columns]
        onboard.advise(draft, _m(), config=CONFIG)
        assert [(c.index, c.role) for c in draft.columns] == before

    def test_the_key_never_shows_up_in_the_error(self, monkeypatch):
        """报错会进日志、进界面、被人贴到聊天里。密钥不能在里面。"""
        _wire(monkeypatch, urllib.error.URLError(f"failed for {CONFIG.api_key}"))
        a = onboard.advise(_draft(), _m(), config=CONFIG)
        assert CONFIG.api_key not in a.note


class TestModelCannotOverrideRules:
    """规则提议过的列，模型改不动。

    这条不是保守，是这套东西可对照性的全部来源：界面上写着「规则提议」的东西一旦
    混进模型的猜测，人就再没有一处能对照——他以为在核对规则，其实在核对一份混合物。
    """

    def test_it_fills_a_hole_the_rules_left(self, monkeypatch):
        _wire(monkeypatch, _reply([{"序号": 2, "role": "spend", "why": "消耗就是花费"}]))
        draft = _draft()
        a = onboard.advise(draft, _m(), config=CONFIG)

        col = draft.columns[2]
        assert col.column == "消耗金额"
        assert col.role == "spend", "规则没认出来的空位，模型填得进去，这就是接它的收益"
        assert col.confidence == "guess", "模型提的不能算有把握，它得留在要人拍板那一档"
        assert "模型提的" in col.why, "依据里必须写明这是谁提的"
        assert a.adopted == ("消耗金额",)

    def test_a_disagreement_keeps_the_rule_and_shows_both(self, monkeypatch):
        _wire(monkeypatch, _reply([{"序号": 1, "role": "product_id", "why": "看着像编号"}]))
        draft = _draft()
        rule_said = draft.columns[1].role
        assert rule_said == "product_name", "前提：规则对这列是有主张的"

        a = onboard.advise(draft, _m(), config=CONFIG)
        assert draft.columns[1].role == "product_name", "打架时留在草案里的必须是规则那份"
        assert draft.columns[1].model_role == "product_id", "模型的说法要留着给人挑"
        assert draft.columns[1].model_why
        assert a.disputed and "product_name" in a.disputed[0] and "product_id" in a.disputed[0]
        assert not a.adopted

    def test_agreement_is_recorded_but_changes_nothing(self, monkeypatch):
        _wire(monkeypatch, _reply([{"序号": 0, "role": "spend_time", "why": "日期"}]))
        draft = _draft()
        before = draft.columns[0].confidence
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert a.agreed == ("日期",)
        assert draft.columns[0].confidence == before, (
            "两边一致是证据，不是理由。把可信度往上调，等于让模型给自己的同伙背书"
        )

    def test_it_cannot_take_a_role_the_rules_already_gave_away(self, monkeypatch):
        """一个角色映两列，引擎会取到其中一列，另一列的钱静默消失。"""
        _wire(monkeypatch, _reply([{"序号": 2, "role": "spend_time", "why": "我觉得是时间"}]))
        draft = _draft()
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert draft.columns[2].role == "", "已经给了「日期」的角色，不能再给「消耗金额」"
        assert draft.columns[2].model_role == "", "挡掉的建议不该留在列上，免得界面又把它提出来"
        assert any("spend_time" in r for r in a.refused)

    def test_it_cannot_map_a_column_the_rules_called_derived(self, monkeypatch):
        """派生列是表里自己算出来的结果。映它等于把同一笔钱记两遍。"""
        headers = ["日期", "花费", "求和项:花费"]
        rows = [["2025-05-01", "10", "10"], ["2025-05-02", "20", "20"]]
        draft = propose(headers, rows, _m(), source_hint="promotion")
        pivot = draft.columns[2]
        assert pivot.derived, "前提：规则认得出这是透视表汇总列"

        _wire(monkeypatch, _reply([{"序号": 2, "role": "spend", "why": "这列也是花费"}]))
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert pivot.role == "", "模型说什么都不能让派生列进账"
        assert a.disputed, "但要摆给人看——万一规则判错了，人得有机会推翻"


    def test_the_warnings_are_recomputed_after_the_merge(self, monkeypatch):
        """警告说的是「照现在这份映射落库会出什么事」。映射改了它必须跟着改。

        不重算的话，屏幕上会同时挂着「spend 没映上」和一行映着 spend 的表格。
        人这时候该信哪个？自相矛盾的警告比没有警告坏——它会让人开始忽略所有警告，
        包括真正拦得住错账的那几条。
        """
        draft = _draft()
        assert any("spend" in w for w in draft.warnings), "前提：规则确实漏了 spend"

        _wire(monkeypatch, _reply([{"序号": 2, "role": "spend", "why": "消耗就是花费"}]))
        onboard.advise(draft, _m(), config=CONFIG)
        assert not any(
            "没映上的角色" in w and "spend" in w for w in draft.warnings
        ), "模型刚把 spend 映上了，还在喊缺 spend 就是在自相矛盾"

    def test_a_notice_unrelated_to_the_mapping_survives(self):
        """重算不能顺手抹掉跟映射无关的提醒。"""
        draft = _draft()
        draft.notices.append("这张表现在已经能被「promo_v1」认出来了，不用再接。")
        from ledger.model.propose import refresh_warnings

        refresh_warnings(draft, _m())
        assert draft.notices, "它说的不是映射的事，映射改了它照样成立"


class TestValidationAtTheSeam:
    """模型说的话变成系统要考虑的东西，就在这一处。这里宁可严。"""

    def test_it_cannot_invent_a_role(self, monkeypatch):
        _wire(monkeypatch, _reply([{"序号": 2, "role": "gmv_total", "why": "编的"}]))
        draft = _draft()
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert draft.columns[2].role == ""
        assert any("gmv_total" in r for r in a.refused)

    def test_it_cannot_point_at_a_column_that_is_not_there(self, monkeypatch):
        _wire(monkeypatch, _reply([{"序号": 99, "role": "spend", "why": "越界"}]))
        a = onboard.advise(_draft(), _m(), config=CONFIG)
        assert any("99" in r for r in a.refused)

    def test_one_role_one_column_even_if_the_model_says_otherwise(self, monkeypatch):
        _wire(monkeypatch, _reply([
            {"序号": 2, "role": "spend", "why": "先来的"},
            {"序号": 3, "role": "spend", "why": "后到的"},
        ]))
        draft = _draft()
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert draft.columns[2].role == "spend"
        assert draft.columns[3].role == "", "点击率不能也当成花费"
        assert any("第 2 列和第 3 列" in r or "spend" in r for r in a.refused)

    def test_a_flood_of_suggestions_is_refused_wholesale(self, monkeypatch):
        """一次提议越过角色总数的量级，说明它在把平台指标往角色上硬套。

        这种错误单条看都挺像回事，所以逐条挡不住；能挡住的判据是总量。
        """
        _wire(monkeypatch, _reply([
            {"序号": i, "role": f"r{i}", "why": "x"} for i in range(40)
        ]))
        draft = _draft()
        before = [(c.index, c.role) for c in draft.columns]
        a = onboard.advise(draft, _m(), config=CONFIG)
        assert [(c.index, c.role) for c in draft.columns] == before
        assert not a.adopted

    def test_temperature_is_zero_and_the_answer_must_be_json(self, monkeypatch):
        """同一张表两次提议给出不同结果，人就没法判断该不该信它。"""
        sent = _wire(monkeypatch, _reply([]))
        onboard.advise(_draft(), _m(), config=CONFIG)
        assert sent["body"]["temperature"] == 0
        assert sent["body"]["response_format"] == {"type": "json_object"}


class TestNothingSensitiveLeaves:
    """出站边界。错了不会有任何现象，只是数据已经出去了。"""

    def test_only_names_shapes_and_masked_samples_go_out(self, monkeypatch):
        sent = _wire(monkeypatch, _reply([]))
        headers = ["订单号", "买家手机", "收货地址", "消耗金额", "主体类型"]
        rows = [
            ["2025051012345678", "13800138000", "浙江省杭州市余杭区文一西路 969 号", "1200.50", "商品"],
            ["2025051012345679", "13900139000", "上海市浦东新区世纪大道 100 号", "980.00", "关键词"],
        ]
        draft = propose(headers, rows, _m(), source_hint="promotion")
        onboard.advise(draft, _m(), config=CONFIG)

        wire = json.dumps(sent["body"], ensure_ascii=False)
        assert "2025051012345678" not in wire, "订单号不出去"
        assert "13800138000" not in wire, "手机号不出去"
        assert "文一西路" not in wire, "收货地址不出去"

    def test_shapes_survive_because_they_are_the_whole_signal(self):
        """脱敏保留的是形状，丢掉的是内容。丢过头就没法判断了。"""
        assert assist.mask("2025051012345678") == "16位数字"
        assert assist.mask("13800138000") == "手机号"
        assert assist.mask(1200.5) == "1200.5"
        assert assist.mask("商品") == "商品", "短枚举值原样带出去：它是判断列用途的强信号"
        assert "字文本" in assist.mask("浙江省杭州市余杭区文一西路 969 号")

    def test_the_column_budget_is_enforced(self, monkeypatch):
        sent = _wire(monkeypatch, _reply([]))
        n = assist.MAX_COLUMNS + 50
        headers = [f"列{i}" for i in range(n)]
        draft = propose(headers, [["1"] * n], _m(), source_hint="promotion")
        onboard.advise(draft, _m(), config=CONFIG)
        payload = json.loads(sent["body"]["messages"][1]["content"].split("\n\n输出")[0])
        assert len(payload["表里的列"]) == assist.MAX_COLUMNS
