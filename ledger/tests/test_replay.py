"""回放门槛：改动之后，账上的每一个数字都不许在没人看见的地方变。

这条测试和 `test_acceptance.py` 分工不同，缺一条都不行：

    test_acceptance    问「算得对不对」，判据是人工维护的 Excel，护着约十个科目。
    这一条              问「变没变」，判据是上一版基线，护着产品对外的每一个字段。

后者能抓到前者抓不到的一整类回归：净利率变了、某家店从「可结账」翻成「拦截」、
未归类金额涨了、覆盖率掉了、某个科目从报表上消失了。这些改动不会报错，只会让
数字悄悄变成另一个样子——而这套系统存在的全部意义就是那些数字。

它同时是让模型参与改引擎的前提。模型可以写代码、可以补测试，但「这次改动对账上
数字有什么影响」不能由模型自己评价，得由这条回放摆出来给人看。
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import MODELS, PLATFORM_DATA, needs_real_data

from ledger.model.loader import load_model
from ledger.replay import (
    BASELINE,
    TOLERANCE,
    Change,
    compare,
    engine_version,
    load_baseline,
    snapshot,
)


def test_baseline_exists_and_is_readable() -> None:
    """基线是仓库资产，丢了等于门槛没了。

    这条不需要真实语料：它盯的是仓库里那份文件本身。有人 rebase 掉了、
    或者 .gitignore 不小心把它排除了，这里立刻红。
    """
    assert BASELINE.exists(), (
        f"基线文件 {BASELINE} 不见了。没有基线就没有回放门槛，"
        f"引擎改动会变成没人验证的改动。在一个已知算得对的版本上跑 "
        f"`ledger replay --record` 重录。"
    )
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert payload.get("stores"), "基线里一家店都没有"
    assert payload.get("engine_version"), "基线没记引擎版本，将来说不清是哪一版录的"


def test_baseline_covers_every_active_store() -> None:
    """在营的店都要在基线里。

    漏一家等于那家店的数字没人盯着。新登记一家店之后重录基线，这条会提醒你。
    """
    model = load_model(MODELS / "cn-ecommerce")
    active = {s.id for s in model.stores if not s.archived}
    covered = set(load_baseline().get("stores", {}))
    missing = active - covered
    assert not missing, (
        f"这些在营店铺不在回放基线里：{sorted(missing)}。"
        f"它们的数字改坏了不会有人发现。跑 `ledger replay --record` 重录。"
    )


@needs_real_data
@pytest.mark.slow
def test_nothing_moved_since_baseline() -> None:
    """整份回放。任何一个数字变了都在这里红。

    红了不代表改动是错的，代表改动动了账上的数字，需要人看一眼是不是想要的。
    确认想要就重录基线，那份 diff 会跟着提交一起进评审。
    """
    model = load_model(MODELS / "cn-ecommerce")
    current = snapshot(model, PLATFORM_DATA)
    assert current, f"{PLATFORM_DATA} 里没算出任何一家店"
    rp = compare(current, load_baseline())
    assert rp.clean, "\n" + rp.report()


class TestTheGateActuallyCatchesThings:
    """门槛本身要被验证。

    一个从不报警的报警器和没有报警器是一回事，而且更糟——它让人以为有防护。
    所以这里拿造出来的差异喂给比较逻辑，确认每一类变化都抓得到。
    """

    def _pair(self, before: dict, after: dict) -> list[Change]:
        rp = compare({"s": {"2026-05": after}}, {"stores": {"s": {"2026-05": before}}})
        return rp.changes

    def test_catches_a_moved_amount(self) -> None:
        changes = self._pair(
            {"statement": [{"id": "net_profit", "name": "净利润", "value": 100.0}]},
            {"statement": [{"id": "net_profit", "name": "净利润", "value": 100.5}]},
        )
        assert len(changes) == 1
        assert changes[0].delta == pytest.approx(0.5)
        # 报告要能让人一眼认出是哪个科目，不能只给下标。
        assert "净利润" in changes[0].path

    def test_ignores_sub_penny_noise(self) -> None:
        """浮点尾数不算变化，否则门槛天天误报，然后就没人看它了。"""
        assert not self._pair(
            {"v": 100.0}, {"v": 100.0 + TOLERANCE / 2}
        )

    def test_catches_can_close_flipping(self) -> None:
        """从能结账变成不能结账，是比任何金额变化都严重的事。

        bool 是 int 的子类，数值比较会把 True 和 1 判成相等。这条钉住 True→False
        一定会被抓到——一旦哪天有人把比较逻辑改成纯数值比，这里立刻红。
        """
        changes = self._pair({"can_close": True}, {"can_close": False})
        assert len(changes) == 1
        assert changes[0].before is True and changes[0].after is False

    def test_catches_a_line_disappearing(self) -> None:
        """科目从报表上消失，比它的数字变了更严重，也更难被发现。"""
        changes = self._pair(
            {"statement": [{"id": "ad", "name": "推广费", "value": -1.0}]},
            {"statement": []},
        )
        assert [c.kind for c in changes] == ["removed"]
        assert "推广费" in changes[0].path

    def test_catches_findings_changing_verdict(self) -> None:
        """自检结论从通过变成拦截，金额一分没动，但账结不出来了。"""
        changes = self._pair(
            {"findings": [{"id": "cover", "name": "覆盖率", "passed": True}]},
            {"findings": [{"id": "cover", "name": "覆盖率", "passed": False}]},
        )
        assert len(changes) == 1
        assert "覆盖率" in changes[0].path

    def test_catches_statement_reordering(self) -> None:
        """报表顺序也是产品的一部分，顺序变了要报出来。

        列表按下标比而不是按 id 配对，就是为了留住这一类变化。
        """
        a = {"id": "a", "name": "收入", "value": 1.0}
        b = {"id": "b", "name": "成本", "value": 2.0}
        assert self._pair({"statement": [a, b]}, {"statement": [b, a]})

    def test_reports_a_whole_period_going_missing(self) -> None:
        """整个账期算不出来了。这种情况下逐字段比是空的，得单独抓。"""
        rp = compare({}, {"stores": {"s": {"2026-05": {"can_close": True}}}})
        assert rp.vanished == ["s 2026-05"]
        assert not rp.clean

    def test_reports_a_new_period(self) -> None:
        """新账期不一定是坏事（补了数据），但要说出来让人确认。"""
        rp = compare({"s": {"2026-06": {}}}, {"stores": {"s": {}}})
        assert rp.appeared == ["s 2026-06"]
        assert not rp.clean


class TestVersionStamp:
    """引擎版本。回滚要回滚到一个说得清的东西上。"""

    def test_reports_a_version(self) -> None:
        assert engine_version()

    def test_version_has_one_of_three_known_shapes(self) -> None:
        """版本只能是这三种形状：纯提交号、提交号加 -dirty、或者 unknown。

        `-dirty` 这个后缀是要紧的：一份在脏工作区录的基线，对应的代码根本不在
        版本库里，事后无从复现。多出别的形状说明有人往里塞了别的东西，
        那些东西会进基线文件，然后每次回放都对不上。
        """
        assert re.fullmatch(r"unknown|[0-9a-f]{7,40}(-dirty)?", engine_version())
