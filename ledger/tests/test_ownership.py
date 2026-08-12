"""运营归属：商品归谁管，从哪个月的记录推出来的。

这一层出的是建议，不是配置。所以测试盯两件事：推得对不对（时效规则），
以及推不出来的时候会不会硬猜——后者更要紧，一条编出来的归属会一路走到发钱。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger import ownership

HEADER = "product_id,period,owner,store\n"


@pytest.fixture
def model_dir(tmp_path: Path):
    """造一个只有归属表的模型目录。返回写表的函数。"""

    def make(rows: str) -> Path:
        (tmp_path / ownership.FILENAME).write_text(HEADER + rows, encoding="utf-8")
        # 换文件要让缓存失效，而缓存认的是 mtime。同一秒内连写两次在某些文件系统上
        # mtime 一模一样，缓存就会把上一份还给你，测试之间互相污染。
        ownership._cache.pop(tmp_path, None)
        return tmp_path

    return make


class TestWhoOwnsIt:
    def test_takes_the_latest_assignment_not_the_first(self, model_dir) -> None:
        """换过人的商品，算的是最近一次安排。"""
        root = model_dir(
            "A1,2025-01,张三,甲店\n"
            "A1,2025-06,李四,甲店\n"
        )
        assert ownership.owners_at(root, "2025-12", ["A1"])["A1"].person == "李四"

    def test_does_not_look_at_assignments_made_later(self, model_dir) -> None:
        """六月换的人，不能拿去算三月的账。

        这是整个模块最容易错的地方，而且错了不报错：拿今天的运营安排去重算历史
        账期，钱会发给一个当时根本不管这个商品的人，报表上什么异常都看不出来。
        """
        root = model_dir(
            "A1,2025-01,张三,甲店\n"
            "A1,2025-06,李四,甲店\n"
        )
        assert ownership.owners_at(root, "2025-03", ["A1"])["A1"].person == "张三"

    def test_carries_the_last_known_assignment_forward(self, model_dir) -> None:
        """归属表停在三月，问五月，答的是三月那一版——并且说出来它是三月的。

        沿用是对的（没换人就是没换人），但必须带出处：归属数据可能已经过期几个月，
        看的人有权知道自己在照多久以前的安排发钱。
        """
        root = model_dir("A1,2026-03,汪学成,喜必顺旗舰店\n")
        got = ownership.owners_at(root, "2026-05", ["A1"])["A1"]
        assert (got.person, got.since) == ("汪学成", "2026-03")

    def test_says_nothing_when_it_knows_nothing(self, model_dir) -> None:
        """问一个归属表里没有的商品，返回空，不返回一个猜的人。"""
        root = model_dir("A1,2025-01,张三,甲店\n")
        assert ownership.owners_at(root, "2025-12", ["B2"]) == {}

    def test_a_product_that_only_appears_later_has_no_owner_yet(self, model_dir) -> None:
        """商品第一次出现在归属表里是六月，问一月就该是没有，不能倒推。"""
        root = model_dir("A1,2025-06,李四,甲店\n")
        assert ownership.owners_at(root, "2025-01", ["A1"]) == {}


class TestWhenThereIsNoTable:
    def test_a_model_without_the_file_still_works(self, tmp_path: Path) -> None:
        """没有归属表是正常状态：新装一套系统本来就没有历史运营安排。

        这里报错的话，任何一个不带这份历史数据的模型目录都开不了提成页。
        """
        assert ownership.table(tmp_path).is_empty()
        assert ownership.owners_at(tmp_path, "2025-01", ["A1"]) == {}

    def test_a_table_missing_columns_is_treated_as_absent(self, tmp_path: Path) -> None:
        """列不对就当没有。半懂不懂地解析出一半，比干脆不认更危险。"""
        (tmp_path / ownership.FILENAME).write_text("foo,bar\n1,2\n", encoding="utf-8")
        ownership._cache.pop(tmp_path, None)
        assert ownership.table(tmp_path).is_empty()


class TestDirtyRows:
    def test_blank_owners_and_ids_are_dropped(self, model_dir) -> None:
        """空人名和空商品 id 直接丢。

        真实的归属表里这两种都有——「无店铺」那 71,662 行里就夹着没填人的。
        留着它们会让覆盖率虚高：界面上说「八成七的商品有归属」，点开一看是空白。
        """
        root = model_dir(
            "A1,2025-01,,甲店\n"
            ",2025-01,张三,甲店\n"
            "A2,2025-01,张三,甲店\n"
        )
        assert set(ownership.owners_at(root, "2025-12", None)) == {"A2"}

    def test_whitespace_around_values_does_not_split_people(self, model_dir) -> None:
        """「张三」和「张三 」是同一个人。不去空白的话发钱时会变成两个人。"""
        root = model_dir(
            "A1,2025-01, 张三 ,甲店\n"
            "A2,2025-01,张三,甲店\n"
        )
        found = ownership.owners_at(root, "2025-12", None)
        assert {o.person for o in found.values()} == {"张三"}


class TestCoverage:
    def test_counts_what_it_can_and_cannot_place(self, model_dir) -> None:
        root = model_dir(
            "A1,2026-03,汪学成,喜必顺旗舰店\n"
            "A2,2026-03,汪学成,喜必顺旗舰店\n"
            "A3,2026-03,张三,喜必顺旗舰店\n"
        )
        got = ownership.coverage(root, "2026-05", ["A1", "A2", "A3", "A4"])
        assert got["products"] == 4
        assert got["matched"] == 3
        assert got["people"] == [{"person": "汪学成", "products": 2},
                                 {"person": "张三", "products": 1}]
        assert got["latest_period"] == "2026-03"

    def test_the_same_product_twice_counts_once(self, model_dir) -> None:
        """脊柱上一个商品有很多子订单，传进来会重复。覆盖率的分母是商品，不是行。"""
        root = model_dir("A1,2026-03,汪学成,甲店\n")
        assert ownership.coverage(root, "2026-05", ["A1", "A1", "A1"])["products"] == 1
