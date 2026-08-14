"""公共表：一份文件里混着全公司几十家店，靠订单号或运单号落到店。

业务把这件事说清楚了：代发成本、刷单（本金佣金）、小额打款、发货运费四张表都是
全公司统一维护的，每个店长交的那一份里都带着别家店的行；兼职费用同样是公共表，
但它一个能匹配到订单的字段都没有，只能摊。

这里盯两处会静悄悄出错的地方：

1. 声明成公司级之后，命中率不再评分。不声明的后果不是算错钱——别家店的订单号
   挂不上，自然不会进这家店的利润——而是界面上刷单这一项写着命中率 3.9%，
   代发 48.5%，看着像整张表九成的数据都坏了。一列常年通红，人就不再看它。
2. 交上来的副本万一圈重了同一批行，直接拼接会让那部分成本翻倍，而翻倍不报错。
   去重键管这件事。
"""

from __future__ import annotations

import pytest
from conftest import MODELS

from ledger.model.loader import load_model

#: 业务点名的四张公共表。兼职费用还没交过来，交了再加进这份名单。
SHARED = {"dropship": "代发", "brushing": "刷单", "small_payment": "小额打款", "freight": "发货运费"}


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


class TestTheFourSharedTablesAreDeclared:
    def test_all_four_are_marked_company_wide(self, model):
        for sid, name in SHARED.items():
            assert model.source(sid).company_wide, (
                f"{name}是全公司一张表，不标出来的话它的命中率会被当成数据质量分打出来"
            )

    def test_all_four_have_a_dedupe_key(self, model):
        """每个店长各交一份，键管的是「两份圈到了同一批行」这一种情况。"""
        for sid, name in SHARED.items():
            assert model.source(sid).dedupe_key, f"{name}缺去重键，重复交会让成本翻倍"

    def test_the_dedupe_key_names_real_roles(self, model):
        """去重键写的角色必须真的绑在模板上，否则去重悄悄不生效。"""
        for sid, name in SHARED.items():
            source = model.source(sid)
            for template in model.templates_of(sid):
                roles = {b.role for b in template.bindings}
                missing = [r for r in source.dedupe_key if r not in roles]
                assert not missing, f"{name}的去重键 {missing} 在模板 {template.id} 上没有绑定"


class TestHitRateIsNotScoredForSharedTables:
    """公司级主表不评命中率，改为不出数。见 view._quality。"""

    def test_brushing_and_dropship_stop_reporting_a_hit_rate(self, model):
        from ledger.view import _quality

        class _Report:
            total_rows, linked_rows, hit_rate = 1202, 47, 0.039
            spine_keys_covered = spine_keys = spine_keys_total = 0
            expect_label, excluded_rows = "", 0

            @property
            def coverage(self):
                return None

        class _Slice:
            link_reports = {"brushing_cost": _Report(), "dropship_cost": _Report()}

        rows = {r["metric"]: r for r in _quality(_Slice(), model)}
        for mid in ("brushing_cost", "dropship_cost"):
            assert rows[mid]["company_wide"] is True
            assert rows[mid]["hit_rate"] is None, (
                "别家店的行挂不上是这张表的常态，不是这家店的数据质量问题"
            )
