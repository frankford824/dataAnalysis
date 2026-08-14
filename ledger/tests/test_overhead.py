"""兼职费用：一张没有任何键的公共表，只能摊。

业务维护的公共表一共五张。前四张（代发成本、刷单/本金佣金、小额打款、发货运费）
每一行都带订单号或运单号，能落到具体的店、具体的单，走正常的摄取链路，那部分由
test_shared_tables.py 盯着。第五张是兼职费用，它一个能落到订单的字段都没有，
所以只能按占比摊。

2026-08 这批数据里业务把这条规则写进了五个平台的订单明细表：新增一列「兼职费用」，
列头批注写着「总公式：总兼职费用 / 总交易收款 * 订单明细表对应行交易收款单元格」。
口径和这里实现的一致（按交易收款占比），但那一列一格数都没填，月度总额仍然只能
从 overheads.csv 读。

这里盯四件事：摊完加回原数、亏损店不倒拿钱、没交表的时候不假装摊过了、
以及那一列永远不许被当成可以挂订单的数据摄进来。
"""

from __future__ import annotations

import pytest
from conftest import MODELS

from ledger import overhead
from ledger.model.loader import ModelError, load_model, _read_overheads
from ledger.model.schema import Overhead


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


class TestItAddsBackUp:
    """摊出去的必须正好等于总额。这个数要和工资单对上，差一分都得有人解释。"""

    def test_the_shares_sum_to_the_total(self):
        spread = overhead.allocate("2026-05", 443343.25, [
            ("a", 492557.29), ("b", 6250.37), ("c", 21033.11),
        ])
        assert sum(s.amount for s in spread.shares) == pytest.approx(443343.25)

    def test_a_split_that_does_not_divide_evenly_still_adds_up(self):
        """三家店收款相同、总额除不尽的时候，分位差不能各自留在各店。"""
        spread = overhead.allocate("2026-05", 100.0, [("a", 1.0), ("b", 1.0), ("c", 1.0)])
        assert sum(s.amount for s in spread.shares) == pytest.approx(100.0)
        assert sorted(s.amount for s in spread.shares) == [33.33, 33.33, 33.34]

    def test_the_biggest_store_absorbs_the_remainder(self):
        """余额落在占比最大的那家。落在最小那家的话，一个几百块收款的店会背上
        相对自己很大的一笔零头。"""
        spread = overhead.allocate("2026-05", 100.0, [
            ("small", 1.0), ("big", 1000000.0), ("mid", 1.0),
        ])
        assert spread.shares[0].store_id == "big"


class TestAtTheRealScale:
    """拿 2026-05 真账期的五家店销售收入摊一遍。

    合成数据（三家店各 1 元）验的是算法，验不出真实量级下的两件事：
    占比相差一百倍时余额会不会跑偏，以及各店摊到的加起来还等不等于总额。
    收入数字取自 2026-08-14 那批数据算出来的 2026-05，见 /api/overview。
    """

    #: 店铺 → 2026-05 销售收入。合计 649,325.23。
    REVENUE = [
        ("taobao_xibishun", 492557.29),
        ("jd_huanglishi", 65000.93),
        ("alibaba1688_xingze", 53789.78),
        ("pdd_kuailejieqing", 34865.58),
        ("douyin_qianhuajian", 3111.65),
    ]

    def test_the_shares_still_add_up_to_the_total(self):
        spread = overhead.allocate("2026-05", 148802.0, self.REVENUE)
        assert sum(s.amount for s in spread.shares) == pytest.approx(148802.0)
        assert spread.basis_total == pytest.approx(649325.23)

    def test_the_smallest_store_still_gets_its_share(self):
        """抖音只有 3,111.65 元收款，占 0.48%。摊到的不能被四舍五入抹成 0——
        抹成 0 的话它那份人力成本就悄悄转嫁给了别的店。"""
        spread = overhead.allocate("2026-05", 148802.0, self.REVENUE)
        assert spread.of("douyin_qianhuajian") == pytest.approx(713.08)
        assert spread.of("taobao_xibishun") == pytest.approx(112876.42)


class TestNobodyGetsPaidForLosingMoney:
    def test_a_store_with_no_revenue_is_not_charged(self):
        spread = overhead.allocate("2026-05", 1000.0, [("a", 5000.0), ("b", 0.0)])
        assert spread.of("b") == 0.0
        assert spread.of("a") == pytest.approx(1000.0)

    def test_a_store_with_negative_revenue_is_not_charged(self):
        """收款是负数（退得比卖的多）时按占比摊会摊出负的兼职，等于倒拿钱。"""
        spread = overhead.allocate("2026-05", 1000.0, [("a", 5000.0), ("b", -300.0)])
        assert spread.of("b") == 0.0
        assert spread.of("a") == pytest.approx(1000.0)

    def test_skipped_stores_are_named(self):
        spread = overhead.allocate("2026-05", 1000.0, [("a", 5000.0), ("b", 0.0)])
        assert any("b" in n for n in spread.notes)


class TestNotYetSubmittedIsNotZero:
    """没交表和这个月兼职是 0 是两件事，混在一起会让提成多发一笔。"""

    def test_no_total_configured_says_so_instead_of_allocating_zero(self):
        spread = overhead.allocate("2026-05", None, [("a", 5000.0)])
        assert not spread.settled
        assert spread.total is None
        assert spread.notes and "还没交" in spread.notes[0]

    def test_nothing_to_allocate_against_is_reported_not_swallowed(self):
        """有总额但一家店都没算出收款：没有摊的依据，得说出来。"""
        spread = overhead.allocate("2026-05", 443343.25, [])
        assert not spread.settled
        assert spread.total == 443343.25
        assert spread.notes


class TestTheMonthlyTotalIsReadFromConfig:
    def test_a_four_digit_month_is_understood(self, tmp_path):
        """历史文件里写的是 2501 那种四位数，系统内部一律用 2026-05。
        只认一种的后果是照旧表抄一遍全落不进任何账期，而且不报错。"""
        path = tmp_path / "overheads.csv"
        path.write_text("period,amount,note\n2501,443343.25,兼职工资\n", encoding="utf-8")
        rows = _read_overheads(path)
        assert rows[0].period == "2025-01"
        assert rows[0].amount == 443343.25

    def test_a_full_period_is_understood(self, tmp_path):
        path = tmp_path / "overheads.csv"
        path.write_text("period,amount\n2026-05,148802\n", encoding="utf-8")
        assert _read_overheads(path)[0].period == "2026-05"

    def test_a_thousands_separator_is_understood(self, tmp_path):
        path = tmp_path / "overheads.csv"
        path.write_text("period,amount\n2026-05,\"443,343.25\"\n", encoding="utf-8")
        assert _read_overheads(path)[0].amount == 443343.25

    def test_a_month_nobody_can_read_is_an_error_not_a_skip(self, tmp_path):
        path = tmp_path / "overheads.csv"
        path.write_text("period,amount\n五月,1000\n", encoding="utf-8")
        with pytest.raises(ModelError, match="period"):
            _read_overheads(path)

    def test_an_expense_written_as_negative_is_still_an_expense(self):
        """支出记正还是记负，业务表格里两种写法都有。"""
        assert Overhead(period="2026-05", amount=-148802.0).amount == 148802.0

    def test_a_period_with_no_row_is_none_not_zero(self):
        model = load_model(MODELS / "cn-ecommerce")
        assert model.overhead("1999-01") is None


class TestItIsNeverLinkedToOrders:
    """兼职费用只能摊，不能挂单。

    诱惑是明摆着的：2026-08 这批订单明细表里已经有一列就叫「兼职费用」，
    绑一下角色、加个指标，看起来就「支持」了。但那一列的值本身是摊出来的结果
    （总兼职费用 × 本行交易收款 / 总交易收款），不是原始凭证。把它摄进来有两个后果：
    摊销开始依赖自己的输出，形成循环；以及哪个月业务真填了数，同一笔钱会既按行
    进了利润、又按月摊进提成基数，扣两遍。

    这条测试守的就是「有人看见那一列，顺手把它接进摄取链路」这一步。
    """

    def test_no_template_binds_the_column(self, model):
        bound = [
            (t.id, b.role, c)
            for t in model.templates
            for b in t.bindings
            for c in b.columns
            if "兼职" in c
        ]
        assert not bound, f"兼职费用被绑成了可摄取的列：{bound}"

    def test_no_source_or_metric_claims_it(self, model):
        """也不能换个名字从数据源那头进来。"""
        assert not [s.id for s in model.sources if "兼职" in s.name]
        assert not [m.id for m in model.metrics if "兼职" in m.name]

    def test_the_statement_does_not_carry_it_as_a_line(self, model):
        """损益表上也不该有这一行。

        店铺利润是「这家店自己赚的」，摊销是发提成时才做的一步——见 api.commission。
        写进损益表的话，同一笔钱会在报表和提成基数里各扣一次。
        """
        assert not [n.id for n in model.statement if "兼职" in n.name]

    def test_the_monthly_total_still_comes_from_the_config(self, model):
        """总额的唯一入口是 overheads.csv。它不在，摊销就该说「还没交」而不是摊 0。"""
        assert model.overhead("2025-01") is not None
        assert model.overhead("2026-05") is None, (
            "2026-08 这批数据没给兼职费用总额（订单明细里那一列全空），"
            "填进 overheads.csv 之前 2026-05 就该是「还没交」"
        )


class TestItReachesThePayout:
    """摊出来的数必须真的从发放金额里扣掉，否则这一整套只是报表上的装饰。"""

    @pytest.fixture
    def paid(self, tmp_path, monkeypatch):
        import ledger.api as api
        from fastapi.testclient import TestClient
        from ledger.workspace import PeriodState

        def snap(store_id, revenue, base, person, amount):
            return PeriodState(
                store_id=store_id, period="2026-05", run_id=1,
                result={
                    "statement": [{"id": "n_receipt", "value": revenue, "available": True}],
                    "commission": {
                        "base_name": "利润", "base_total": base, "total": amount,
                        "configured": True,
                        "people": [{"person": person, "amount": amount, "base": base,
                                    "rate": 0.05, "products": 3}],
                    },
                },
            )

        class _WS:
            def overview(self):
                return [snap("taobao_xibishun", 900.0, 100.0, "张三", 5.0),
                        snap("alibaba1688_xingze", 100.0, 100.0, "李四", 5.0)]

            def state(self, store_id, period):
                return None

        monkeypatch.setattr(api, "WORKSPACE_ROOT", tmp_path / "space")
        monkeypatch.setattr(api, "_ws", None)
        monkeypatch.setattr(api, "workspace", lambda: _WS())
        model = load_model(MODELS / "cn-ecommerce")
        with_overhead = model.model_copy(update={
            "overheads": (Overhead(period="2026-05", amount=20.0),),
        })
        monkeypatch.setattr(api, "_model", lambda: with_overhead)
        with TestClient(api.app) as c:
            yield c

    def test_the_total_is_split_by_revenue_share(self, paid):
        body = paid.get("/api/commission").json()
        got = {s["store_id"]: s["overhead"] for s in body["stores"]}
        assert got["taobao_xibishun"] == pytest.approx(18.0)
        assert got["alibaba1688_xingze"] == pytest.approx(2.0)

    def test_the_base_and_the_payout_both_come_down(self, paid):
        body = paid.get("/api/commission").json()
        big = next(s for s in body["stores"] if s["store_id"] == "taobao_xibishun")
        assert big["base_after"] == pytest.approx(82.0)
        # 基数从 100 降到 82，发放跟着按同一比例降：5 × 0.82。
        assert big["total"] == pytest.approx(4.1)
        assert big["total_before"] == pytest.approx(5.0)

    def test_the_page_says_how_it_was_split(self, paid):
        """会让每个人到手变少的一步，页面上必须有出处。"""
        head = paid.get("/api/commission").json()["overhead"]
        assert head["settled"] and head["total"] == 20.0
        assert head["basis_total"] == pytest.approx(1000.0)
        assert {s["store_id"] for s in head["shares"]} == {
            "taobao_xibishun", "alibaba1688_xingze",
        }

    def test_each_person_amount_matches_the_store_total(self, paid):
        """页面上按人汇总的合计必须等于按店汇总的合计。两处不一致就没人敢照着发钱。"""
        body = paid.get("/api/commission").json()
        assert body["total"] == pytest.approx(sum(s["total"] for s in body["stores"]))
