"""店铺注册表与文件归属。

店铺是数据归属和账期结算的单位，认错店就是把一家店的钱记到另一家头上。
这批测试盯的是「认不出来时会怎样」——认不出必须拦下来问人，不能塞进某家店凑数。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MODELS

from ledger.cli import group_by_store
from ledger.model.loader import load_model
from ledger.model.schema import Model, Platform, Store

#: 猜平台的测试要有平台清单才成立——平台是模型数据，不是代码常量。
PLATFORMS = (
    Platform(id="taobao", name="淘宝天猫", hints=("淘宝", "天猫", "tmall", "TB")),
    Platform(id="alibaba1688", name="阿里巴巴 1688", hints=("1688", "阿里巴巴", "阿里")),
    Platform(id="douyin", name="抖音", hints=("抖音", "抖店")),
)


def _model(*stores: Store) -> Model:
    return Model(id="t", name="t", platforms=PLATFORMS, stores=stores)


class TestFileOwnership:
    def test_matches_by_name_in_filename(self):
        """交上来的文件名形如「类别-店铺名.xlsx」，店名就在里面。"""
        m = _model(Store(id="a", name="淘宝喜必顺", platform="taobao"))
        assert m.store_of("聚水潭成本-淘宝喜必顺.xlsx").id == "a"
        assert m.store_of("订单明细-淘宝喜必顺.xlsx").id == "a"

    def test_longest_match_wins(self):
        """短店名会误伤长店名，取最具体的那个。

        「喜必顺」和「淘宝喜必顺」可能同时登记（一家店换过平台或改过名），
        文件名里两个都能匹配上，只有最长的那个是对的。
        """
        m = _model(
            Store(id="short", name="喜必顺", platform="taobao"),
            Store(id="long", name="淘宝喜必顺", platform="taobao"),
        )
        assert m.store_of("运费-淘宝喜必顺.xlsx").id == "long"
        assert m.store_of("运费-喜必顺.xlsx").id == "short"

    def test_alias_also_matches(self):
        """店铺改过名，旧名字的历史文件还得认。"""
        m = _model(Store(id="a", name="淘宝喜必顺", platform="taobao", aliases=("喜必顺旗舰店",)))
        assert m.store_of("对账-喜必顺旗舰店.xlsx").id == "a"

    def test_unknown_store_is_not_guessed(self):
        """认不出的文件绝不塞进某家店。

        塞进去就是把别家的钱记到这家头上，而且没人会发现。宁可拦下来问人。
        """
        m = _model(Store(id="a", name="淘宝喜必顺", platform="taobao"))
        assert m.store_of("运费-某个没登记的店.xlsx") is None

    def test_grouping_separates_orphans(self):
        m = _model(
            Store(id="a", name="淘宝喜必顺", platform="taobao"),
            Store(id="b", name="1688星泽气球派对", platform="alibaba1688"),
        )
        files = [
            Path("运费-淘宝喜必顺.xlsx"),
            Path("对账-1688星泽气球派对.xlsx"),
            Path("推广-没登记的店.xlsx"),
        ]
        grouped, orphans = group_by_store(files, m)
        assert set(grouped) == {"a", "b"}
        assert [f.name for f in orphans] == ["推广-没登记的店.xlsx"]


class TestArchiving:
    def test_archived_excluded_from_active(self):
        """关店不等于删数据：不参与新账期，历史账仍可重算。"""
        m = _model(
            Store(id="a", name="在营店", platform="taobao"),
            Store(id="b", name="关掉的店", platform="taobao", archived=True),
        )
        assert [s.id for s in m.active_stores()] == ["a"]
        # 归档店的文件照样认得出来，否则历史账没法重算。
        assert m.store_of("运费-关掉的店.xlsx").id == "b"


class TestPlatformGuess:
    def test_guesses_from_prefix(self):
        m = _model()
        assert m.guess_platform("淘宝喜必顺") == "taobao"
        assert m.guess_platform("1688星泽气球派对") == "alibaba1688"
        assert m.guess_platform("抖音浅花涧节日装饰") == "douyin"
        assert m.guess_platform("抖店喜品") == "douyin"

    def test_returns_empty_when_unsure(self):
        """猜不出就返回空。猜测只用于给登记提建议，绝不参与计算。

        「朗歌1688」这种平台名在后缀的就猜不出来——猜错平台会让整家店按错误的
        利润口径算账，宁可让人来配。
        """
        m = _model()
        assert m.guess_platform("朗歌1688") == ""
        assert m.guess_platform("某个新店") == ""

    def test_longest_prefix_wins(self):
        """两个平台的线索词都命中时，长的更具体。"""
        m = Model(id="t", name="t", platforms=(
            Platform(id="ali", name="阿里", hints=("阿里",)),
            Platform(id="alibaba1688", name="1688", hints=("阿里巴巴",)),
        ))
        assert m.guess_platform("阿里巴巴星泽") == "alibaba1688"
        assert m.guess_platform("阿里妈妈某店") == "ali"

    def test_no_platforms_declared_means_no_guess(self):
        """没登记平台就别猜。空模型下不该凭空造出一个平台 id。"""
        assert Model(id="t", name="t").guess_platform("淘宝喜必顺") == ""


class TestPlatformRegistry:
    """平台错字是静默扣钱的，加载时必须拦下来。"""

    def test_unknown_store_platform_is_rejected(self):
        with pytest.raises(ValueError, match="没登记"):
            Model(
                id="t", name="t", platforms=PLATFORMS,
                stores=(Store(id="a", name="某店", platform="taobao "),),
            )

    def test_platform_ids_skips_archived(self):
        m = Model(id="t", name="t", platforms=(
            *PLATFORMS, Platform(id="paipai", name="拍拍", archived=True),
        ))
        assert "paipai" not in m.platform_ids()
        assert "taobao" in m.platform_ids()

    def test_no_registry_means_no_check(self):
        """没有 platforms.yaml 的模型照样能加载：这份清单是可选的。"""
        m = Model(id="t", name="t", stores=(Store(id="a", name="某店", platform="随便"),))
        assert m.store("a").platform == "随便"


class TestShippedRegistry:
    """仓库自带的这份注册表本身要是对的。"""

    def test_loads(self):
        m = load_model(MODELS / "cn-ecommerce")
        assert len(m.stores) == 3
        assert {s.platform for s in m.stores} == {"taobao", "alibaba1688", "douyin"}

    def test_two_stores_share_one_entity(self):
        """1688星泽 和 抖音浅花涧 同属义乌星泽天成，这个关系推不出来只能配。

        主体名不是编的：1688 收款明细的「归属主体名称」和抖音对账单的
        「商户主体名称」写的是同一家。
        """
        m = load_model(MODELS / "cn-ecommerce")
        by_entity: dict[str, list[str]] = {}
        for s in m.stores:
            if s.entity:
                by_entity.setdefault(s.entity, []).append(s.id)
        shared = [ids for ids in by_entity.values() if len(ids) > 1]
        assert shared == [["alibaba1688_xingze", "douyin_qianhuajian"]]

    def test_real_filenames_all_resolve(self):
        """交上来的那批真实文件名必须全部认得出归属。"""
        m = load_model(MODELS / "cn-ecommerce")
        for name in [
            "聚水潭成本-淘宝喜必顺.xlsx",
            "订单详情-抖音浅花涧节日装饰.xlsx",
            "对账-1688星泽气球派对.xlsx",
            "小额打款-抖音浅花涧节日装饰.xlsx",
        ]:
            assert m.store_of(name) is not None, name
