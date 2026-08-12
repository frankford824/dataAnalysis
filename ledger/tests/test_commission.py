"""提成。

这一套测试针对的是一类特殊的错：提成算错不会自己暴露。

损益表错了迟早会被发现——勾稽对不上、覆盖率掉下来、未归属金额冒出来，系统里有
一堆互相牵制的约束。提成没有。少配一个人、比例写错一位、生效日期差一天，算出来
的数完全合法，界面一片正常，只有那个少拿钱的人知道，而他看不到这个界面。

所以下面每一条测的都是「算出来是个数，但那个数是错的」。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ledger import commission
from ledger.engine.runtime import RunResult
from ledger.model.schema import (
    CommissionRule,
    LinkRule,
    Metric,
    Model,
    Platform,
    SourceContract,
    StatementNode,
    Store,
    Template,
    ValueExpr,
)

# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #


def _model(rules: list[CommissionRule] | None = None, **kw) -> Model:
    """一个刚好够算提成的最小模型：一家店、两条指标、毛利 = 收入 + 成本。"""
    return Model(
        id="t",
        name="测试",
        platforms=(Platform(id="taobao", name="淘宝"),),
        stores=(Store(id="s1", name="测试店", platform="taobao"),),
        sources=(
            SourceContract(id="order", name="订单明细", owner_role="shop_owner",
                           cadence="daily", provides=["receipt"]),
            SourceContract(id="cost", name="成本", owner_role="warehouse",
                           cadence="daily", provides=["goods"]),
        ),
        templates=(
            Template(id="t_order", source="order", name="订单",
                     match_columns=["子订单编号"],
                     bindings=[{"role": "sub_order_id", "columns": ["子订单编号"]},
                               {"role": "amount", "columns": ["金额"]}]),
            Template(id="t_cost", source="cost", name="成本",
                     match_columns=["成本"],
                     bindings=[{"role": "sub_order_id", "columns": ["子订单编号"]},
                               {"role": "amount", "columns": ["成本"]}]),
        ),
        metrics=(
            Metric(id="receipt", name="销售收入", source="order",
                   value=ValueExpr(op="sum", of=["amount"]),
                   link=LinkRule(key="sub_order_id", to="order.sub_order_id", grain="order")),
            Metric(id="goods", name="商品成本", source="cost", sign="negate",
                   value=ValueExpr(op="sum", of=["amount"]),
                   link=LinkRule(key="sub_order_id", to="order.sub_order_id", grain="order")),
        ),
        statement=(
            StatementNode(id="n_receipt", name="销售收入", level=2,
                          formula={"op": "add", "of": ["receipt"]}),
            StatementNode(id="n_goods", name="商品成本", level=2,
                          formula={"op": "add", "of": ["goods"]}),
            StatementNode(id="gross", name="毛利", level=1, is_total=True,
                          commission_base=True,
                          formula={"op": "add", "of": ["n_receipt", "n_goods"]}),
            StatementNode(id="margin", name="毛利率", level=1, display="percent",
                          formula={"op": "ratio", "of": ["gross", "n_receipt"]}),
        ),
        commission=tuple(rules or ()),
        **kw,
    )


def _run(orders: list[tuple[str, str, str, float]], store: str = "s1",
         period: str = "2026-05") -> RunResult:
    """造一个跑完的结果。

    orders 每项是（子订单号, 商品id, 下单时间, 毛利）。毛利直接摆进 receipt
    这一条指标——这里测的是分配，不是引擎怎么把毛利算出来的。
    """
    spine = pl.DataFrame(
        {
            "sub_order_id": [o[0] for o in orders],
            "product_id": [o[1] for o in orders],
            "order_time": [o[2] for o in orders],
            "store": [store] * len(orders),
            "period": [period] * len(orders),
        }
    ).with_columns(pl.col("order_time").str.to_datetime("%Y-%m-%d"))

    facts = pl.DataFrame(
        {
            "metric_id": ["receipt"] * len(orders),
            "source_id": ["order"] * len(orders),
            "store": [store] * len(orders),
            "period": [period] * len(orders),
            "link_key": [o[0] for o in orders],
            "amount": [o[3] for o in orders],
            "factor": [1.0] * len(orders),
            "spine_row": list(range(len(orders))),
        }
    ).with_columns(pl.col("spine_row").cast(pl.UInt32))

    return RunResult(
        model=_model(), ingestion=None, facts=facts, notes=[],  # type: ignore[arg-type]
        spine_rows=len(orders), spine_facts=facts, projections={}, spine=spine,
    )


def _rule(day: str, product: str, person: str, share: float, total: float,
          store: str = "s1", name: str = "") -> CommissionRule:
    return CommissionRule(effective_from=day, store=store, product_id=product,
                          product_name=name, person=person, share=share, total_rate=total)


# --------------------------------------------------------------------------- #


class TestTheBaseIsTheStatement:
    """提成基数必须就是损益表上那一行，不能是另算的一个数。"""

    def test_it_adds_up_to_the_gross_profit_line(self):
        run = _run([("a", "p1", "2026-05-02", 100.0), ("b", "p1", "2026-05-03", 50.0)])
        m = _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.base_total == 150.0
        assert c.total == pytest.approx(7.5)

    def test_the_base_follows_the_model_not_the_code(self):
        """把标记挪到别的节点，提成基数跟着变。写死在代码里就做不到这件事。"""
        m = _model()
        assert m.commission_base_node().id == "gross"
        assert set(m.commission_base_metrics()) == {"receipt", "goods"}

    def test_a_ratio_line_cannot_be_the_base(self):
        """毛利率没法拆到子订单再加起来。硬拆出来的数看着像钱，其实没有意义。"""
        with pytest.raises(ValueError, match="不是加法"):
            Model(
                id="t", name="测试",
                platforms=(Platform(id="taobao", name="淘宝"),),
                stores=(Store(id="s1", name="店", platform="taobao"),),
                sources=(SourceContract(id="order", name="订单", owner_role="shop_owner",
                                        cadence="daily"),),
                metrics=(Metric(id="receipt", name="收入", source="order",
                                value=ValueExpr(op="sum", of=["amount"])),),
                statement=(
                    StatementNode(id="n_receipt", name="收入", level=2,
                                  formula={"op": "add", "of": ["receipt"]}),
                    StatementNode(id="margin", name="毛利率", level=1, commission_base=True,
                                  formula={"op": "ratio", "of": ["n_receipt", "n_receipt"]}),
                ),
                commission=(_rule("2026-01-01", "p1", "张三", 0.05, 0.05),),
            )

    def test_only_one_node_can_be_the_base(self):
        with pytest.raises(ValueError, match="提成基数只能标一个节点"):
            Model(
                id="t", name="测试",
                statement=(
                    StatementNode(id="a", name="甲", commission_base=True,
                                  formula={"op": "constant", "of": [], "value": 0.0}),
                    StatementNode(id="b", name="乙", commission_base=True,
                                  formula={"op": "constant", "of": [], "value": 0.0}),
                ),
            )


class TestTheChangeDateDecidesWhichRulesApply:
    """业务原话：变更时间之前的下单时间按老规则，之后按新规则。"""

    def test_orders_before_the_change_keep_the_old_rate(self):
        run = _run([("before", "p1", "2026-05-09", 1000.0),
                    ("after", "p1", "2026-05-11", 1000.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.05, 0.05),
            _rule("2026-05-10", "p1", "张三", 0.08, 0.08),
        ])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == pytest.approx(50.0 + 80.0)

    def test_the_change_day_itself_uses_the_new_rate(self):
        """「创建时间 ≥ 新增时间」——当天算新的。差一天就是差一版比例。"""
        run = _run([("same", "p1", "2026-05-10", 1000.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.05, 0.05),
            _rule("2026-05-10", "p1", "张三", 0.08, 0.08),
        ])
        assert commission.compute(run, m, "s1", "2026-05").total == pytest.approx(80.0)

    def test_orders_before_any_version_get_nothing(self):
        """配置是 5 月才建的，4 月的单不能倒追。"""
        run = _run([("old", "p1", "2026-05-01", 1000.0)])
        m = _model([_rule("2026-05-15", "p1", "张三", 0.05, 0.05)])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == 0.0
        assert c.unassigned_base == 1000.0

    def test_recomputing_does_not_change_history(self):
        """同一批老单，加了新版配置之后重算，结果必须一模一样。

        这是生效制唯一真正要保证的事：只要不动历史行，重算一百遍老单都不变。
        """
        run = _run([("old", "p1", "2026-05-01", 1000.0)])
        before = commission.compute(
            run, _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)]), "s1", "2026-05")
        after = commission.compute(
            run, _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05),
                         _rule("2026-06-01", "p1", "张三", 0.09, 0.09)]), "s1", "2026-05")
        assert before.total == after.total == pytest.approx(50.0)

    def test_an_order_with_no_time_is_not_guessed(self):
        """下单时间缺失不能当成「刚下的单」按最新版算——那会把老单算成新比例。"""
        run = _run([("x", "p1", "2026-05-02", 100.0)])
        run = RunResult(
            model=run.model, ingestion=run.ingestion, facts=run.facts, notes=[],
            spine_rows=1, spine_facts=run.spine_facts, projections={},
            spine=run.spine.with_columns(pl.lit(None, dtype=pl.Datetime).alias("order_time")),
        )
        m = _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == 0.0
        assert c.unassigned_base == 100.0


class TestPeopleComeAndGo:
    """入职、离职、继承——全靠发一版新配置，不给人单独记起止日期。"""

    def test_a_leaver_keeps_the_orders_placed_before_they_left(self):
        run = _run([("before", "p1", "2026-05-05", 1000.0),
                    ("after", "p1", "2026-05-25", 1000.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.06, 0.06),
            _rule("2026-05-20", "p1", "李四", 0.06, 0.06),   # 张三离职，李四接手
        ])
        c = commission.compute(run, m, "s1", "2026-05")
        got = {p.person: p.amount for p in c.people}
        assert got == {"张三": pytest.approx(60.0), "李四": pytest.approx(60.0)}

    def test_a_joiner_splits_the_rate_from_their_start_date(self):
        run = _run([("before", "p1", "2026-05-05", 1000.0),
                    ("after", "p1", "2026-05-25", 1000.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.06, 0.06),
            _rule("2026-05-20", "p1", "张三", 0.04, 0.06),
            _rule("2026-05-20", "p1", "王五", 0.02, 0.06),
        ])
        c = commission.compute(run, m, "s1", "2026-05")
        got = {p.person: p.amount for p in c.people}
        assert got["张三"] == pytest.approx(60.0 + 40.0)
        assert got["王五"] == pytest.approx(20.0)

    def test_the_total_rate_stays_put_while_people_change(self):
        """一个商品给出去的总额是定死的，换几个人分不改变总数。"""
        run = _run([("a", "p1", "2026-05-25", 1000.0)])
        m = _model([
            _rule("2026-05-20", "p1", "张三", 0.04, 0.06),
            _rule("2026-05-20", "p1", "王五", 0.02, 0.06),
        ])
        assert commission.compute(run, m, "s1", "2026-05").total == pytest.approx(60.0)


class TestTheSharesMustAddUp:
    """子提成率之和必须等于总提成率。加载时就拦，不能等算完才发现。"""

    def test_a_missing_person_is_refused_at_load(self):
        with pytest.raises(ValueError, match="加起来是"):
            _model([
                _rule("2026-05-01", "p1", "张三", 0.04, 0.06),
                # 王五那 0.02 忘了配。算出来完全合法，只是他一分钱没有。
            ])

    def test_an_overshoot_is_refused(self):
        with pytest.raises(ValueError, match="加起来是"):
            _model([
                _rule("2026-05-01", "p1", "张三", 0.05, 0.06),
                _rule("2026-05-01", "p1", "王五", 0.05, 0.06),
            ])

    def test_the_error_names_the_version_and_the_people(self):
        """报错要能让人直接去改，而不是只知道「有问题」。"""
        with pytest.raises(ValueError) as e:
            _model([_rule("2026-05-01", "p1", "张三", 0.04, 0.06)])
        msg = str(e.value)
        assert "2026-05-01" in msg and "p1" in msg and "张三" in msg

    def test_two_different_totals_in_one_version_is_refused(self):
        with pytest.raises(ValueError, match="总提成率写了"):
            _model([
                _rule("2026-05-01", "p1", "张三", 0.04, 0.06),
                _rule("2026-05-01", "p1", "王五", 0.02, 0.07),
            ])

    def test_the_same_person_twice_in_one_version_is_refused(self):
        with pytest.raises(ValueError, match="出现了两次"):
            _model([
                _rule("2026-05-01", "p1", "张三", 0.03, 0.06),
                _rule("2026-05-01", "p1", "张三", 0.03, 0.06),
            ])

    def test_an_unregistered_store_is_refused(self):
        with pytest.raises(ValueError, match="没登记的店铺"):
            _model([_rule("2026-05-01", "p1", "张三", 0.05, 0.05, store="不存在")])

    def test_rounding_noise_is_tolerated(self):
        """0.0333+0.0333+0.0334 这种手输的三位小数不该被浮点误差判死。"""
        m = _model([
            _rule("2026-05-01", "p1", "甲", 0.0333, 0.1),
            _rule("2026-05-01", "p1", "乙", 0.0333, 0.1),
            _rule("2026-05-01", "p1", "丙", 0.0334, 0.1),
        ])
        assert len(m.commission) == 3


class TestUnassignedProductsFallToTheStore:
    """没给商品配人的，统一按店铺来。"""

    def test_a_product_without_rules_uses_the_store_rule(self):
        run = _run([("a", "p1", "2026-05-02", 1000.0), ("b", "p9", "2026-05-02", 500.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.05, 0.05),
            _rule("2026-01-01", "", "店长", 0.02, 0.02),
        ])
        c = commission.compute(run, m, "s1", "2026-05")
        got = {p.person: p.amount for p in c.people}
        assert got == {"张三": pytest.approx(50.0), "店长": pytest.approx(10.0)}
        assert c.fallback_base == 500.0

    def test_a_product_with_rules_does_not_also_get_the_store_rule(self):
        """配了人的商品不能再被店铺兜底摊一遍，那是双份。"""
        run = _run([("a", "p1", "2026-05-02", 1000.0)])
        m = _model([
            _rule("2026-01-01", "p1", "张三", 0.05, 0.05),
            _rule("2026-01-01", "", "店长", 0.02, 0.02),
        ])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == pytest.approx(50.0)
        assert [p.person for p in c.people] == ["张三"]

    def test_with_no_store_rule_the_money_is_reported_as_unassigned(self):
        """没人管的毛利要说出来。悄悄算成 0 的话，界面上一切正常。"""
        run = _run([("a", "p1", "2026-05-02", 1000.0), ("b", "p9", "2026-05-02", 500.0)])
        m = _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.unassigned_base == 500.0
        assert any(p.unassigned for p in c.products if p.product_id == "p9")

    def test_no_config_at_all_says_so(self):
        run = _run([("a", "p1", "2026-05-02", 1000.0)])
        c = commission.compute(run, _model(), "s1", "2026-05")
        assert c.total == 0.0
        assert c.unassigned_base == 1000.0
        assert c.notes and "还没有提成配置" in c.notes[0]


class TestNothingLeaksAcrossStoresOrPeriods:
    def test_another_period_is_not_counted(self):
        run = _run([("a", "p1", "2026-05-02", 1000.0)])
        assert commission.compute(run, _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)]),
                                  "s1", "2026-04").base_total == 0.0

    def test_another_stores_rules_do_not_apply(self):
        run = _run([("a", "p1", "2026-05-02", 1000.0)])
        m = Model(
            **{**_model().__dict__,
               "stores": (Store(id="s1", name="甲", platform="taobao"),
                          Store(id="s2", name="乙", platform="taobao")),
               "commission": (_rule("2026-01-01", "p1", "别家的人", 0.05, 0.05, store="s2"),)}
        )
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == 0.0
        assert c.unassigned_base == 1000.0


class TestNegativeGrossProfitIsShownNotHidden:
    def test_a_loss_produces_a_negative_commission_and_is_flagged(self):
        """亏的单算出负提成。对不对是业务决定，但不能让它无声无息。"""
        run = _run([("win", "p1", "2026-05-02", 1000.0), ("lose", "p1", "2026-05-03", -400.0)])
        m = _model([_rule("2026-01-01", "p1", "张三", 0.05, 0.05)])
        c = commission.compute(run, m, "s1", "2026-05")
        assert c.total == pytest.approx(30.0)
        assert c.negative_orders == 1
        assert c.negative_base == -400.0


class TestRatesCanBeWrittenEitherWay:
    def test_percent_and_decimal_mean_the_same(self, tmp_path):
        from ledger.model.loader import _read_commission
        p = tmp_path / "commission.csv"
        p.write_text(
            "effective_from,store,product_id,person,share,total_rate\n"
            "2026-01-01,s1,p1,甲,3%,5%\n"
            "2026-01-01,s1,p1,乙,0.02,0.05\n",
            encoding="utf-8",
        )
        rules = _read_commission(p)
        assert [r.share for r in rules] == [pytest.approx(0.03), pytest.approx(0.02)]
        assert [r.total_rate for r in rules] == [pytest.approx(0.05), pytest.approx(0.05)]

    def test_a_non_number_says_which_line(self, tmp_path):
        from ledger.model.loader import ModelError, _read_commission
        p = tmp_path / "commission.csv"
        p.write_text(
            "effective_from,store,product_id,person,share,total_rate\n"
            "2026-01-01,s1,p1,甲,三成,0.05\n",
            encoding="utf-8",
        )
        with pytest.raises(ModelError, match="第 2 行"):
            _read_commission(p)

    def test_a_bad_date_is_refused(self):
        with pytest.raises(ValueError, match="生效日期"):
            _rule("2026/05/01", "p1", "张三", 0.05, 0.05)


class TestTheUploadedSheetIsReadTheWayPeopleActuallyFillIt:
    """上传这条路上每一处出错都是静默的：商品 ID 少一位、表头写了中文、
    Excel 把数字改成科学计数法——算出来都是一份合法的表，只是有人没提成。"""

    def test_chinese_headers_are_understood(self):
        from ledger.api import commission_rows
        raw = ("生效日期,店铺,商品ID,人员,子提成率,总提成率\n"
               "2026-01-01,s1,p1,张三,3%,3%\n").encode("utf-8")
        rows = commission_rows("提成.csv", raw)
        assert rows == [{"effective_from": "2026-01-01", "store": "s1", "product_id": "p1",
                         "person": "张三", "share": "3%", "total_rate": "3%"}]

    def test_the_excel_text_guard_is_stripped(self):
        """导出时给商品 ID 套了 `="..."` 逼 Excel 当文本读。带着壳传回来匹配不上
        任何订单，而且不报错——只表现为「这个商品怎么没提成」。"""
        from ledger.api import commission_rows
        raw = ('effective_from,store,product_id,person,share,total_rate\n'
               '2026-01-01,s1,"=""1047833336884""",张三,3%,3%\n').encode("utf-8")
        assert commission_rows("x.csv", raw)[0]["product_id"] == "1047833336884"

    def test_a_missing_column_says_which_one_in_chinese(self):
        from ledger.api import commission_rows
        raw = "生效日期,店铺,人员\n2026-01-01,s1,张三\n".encode("utf-8")
        with pytest.raises(ValueError) as e:
            commission_rows("x.csv", raw)
        assert "子提成率" in str(e.value) and "总提成率" in str(e.value)

    def test_extra_columns_are_ignored(self):
        """待配商品表末尾带着本期毛利和子订单数。填完直接传回来就该能用，
        要人先删两列的话，总会有人漏做。"""
        from ledger.api import commission_rows
        raw = ("effective_from,store,product_id,person,share,total_rate,本期毛利,本期子订单数\n"
               "2026-01-01,s1,p1,张三,3%,3%,32078.70,1835\n").encode("utf-8")
        rows = commission_rows("x.csv", raw)
        assert rows[0]["share"] == "3%"
        assert "本期毛利" not in rows[0]


class TestABadUploadLeavesTheOldConfigUntouched:
    def test_a_broken_sheet_does_not_land(self, tmp_path):
        """整份校验不过就一个字节都不能落盘——存进一份自相矛盾的配置，
        下次加载模型会直接失败，整套系统起不来。"""
        import shutil

        from ledger.model.config import replace_commission
        from ledger.model.loader import ModelError
        src = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
        root = tmp_path / "model"
        shutil.copytree(src, root)
        good = ("effective_from,store,product_id,product_name,person,share,total_rate,note\n"
                "2026-01-01,taobao_xibishun,p1,,张三,0.05,0.05,\n")
        (root / "commission.csv").write_text(good, encoding="utf-8")

        with pytest.raises(ModelError, match="加起来是"):
            replace_commission(root, [
                {"effective_from": "2026-02-01", "store": "taobao_xibishun",
                 "product_id": "p1", "person": "张三", "share": "0.04", "total_rate": "0.06"},
            ])
        assert (root / "commission.csv").read_text(encoding="utf-8") == good

    def test_the_file_keeps_its_line_endings(self, tmp_path):
        """写回不能顺手把整份文件的换行符改掉。

        Windows 上 Python 文本模式会把 \\n 换成 \\r\\n。实测线上就栽在这里：
        在界面上改一个店铺主体，回写之后整份 stores.yaml 逐行都和仓库不同，
        真正改的那一行反而淹在四十八行「差异」里。写回保住注释、不重排的功夫，
        会被这一处全部抵消。
        """
        import shutil

        from ledger.model.config import replace_commission
        src = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
        root = tmp_path / "model"
        shutil.copytree(src, root)
        replace_commission(root, [
            {"effective_from": "2026-01-01", "store": "taobao_xibishun",
             "product_id": "p1", "person": "张三", "share": "0.05", "total_rate": "0.05"},
        ])
        assert b"\r\n" not in (root / "commission.csv").read_bytes()
