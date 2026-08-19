"""接一张新表，不能接错。

这是产品里唯一一个「人做的决定直接决定账对不对」的地方。别处出错会报异常或出红字，
这里出错的形态是：列名全对、填充率 100%、试跑一路绿灯，而账里的钱少了一个数量级。
所以这批测试盯的不是「向导能不能跑通」，而是「向导拦不拦得住那几种静默错」。

每一条都对应一次真实踩坑：

  重名列同映    淘宝万相台那张表有两列都叫「推广主体ID」。按列名回传映射，两列
                会一起拿到 product_id，引擎取到几乎全空的那一列，8226 行被当成
                合计行丢掉，只剩 397 行，推广费从 8.85 万变成 3354 元。不报错。
  合计行标记选错 同一件事的另一面：合计行标记的判据是「这个角色为空」，选到一列
                大片是空的，就等于宣布「全表都是合计行」。
  签名是子集    新模板的识别签名如果是老模板列集的子集，老版的表以后会被新模板
                抢走。当期不报错，下一次重算时老账悄悄换了口径。
  透视字段      「求和项:花费」是 Excel 透视表从花费那列汇总出来的。映射它等于把
                同一笔钱记两遍。
  结果列        店长在平台导出上手工加的「净利润」列。映射它是拿别人算过的数当原始
                数据，从此这个数不再可追溯。
"""

from __future__ import annotations

import pytest
from conftest import write_xlsx

from ledger.engine.parse import parse
from ledger.model.loader import ModelError
from ledger.model.propose import propose, shape_of
from ledger.model.schema import (
    ColumnBinding,
    Metric,
    Model,
    Platform,
    SourceContract,
    Template,
    ValueExpr,
)


def _model(*extra: Template) -> Model:
    """一个够小但结构真实的模型：有脉柱、有推广、有平台。"""
    promo = Template(
        id="promo_v1",
        source="promotion",
        name="推广旧版",
        match_columns=("日期", "花费", "主体名称"),
        bindings=(
            ColumnBinding(role="spend_time", columns=("日期",), kind="time"),
            ColumnBinding(role="spend", columns=("花费",), kind="number"),
            ColumnBinding(role="product_name", columns=("主体名称",)),
        ),
        time_slots={"spend_date": "spend_time"},
    )
    return Model(
        id="t",
        name="测试模型",
        platforms=(Platform(id="taobao", name="淘宝天猫", hints=["淘宝"]),),
        sources=(
            SourceContract(
                id="promotion", name="推广", owner_role="operations",
                cadence="monthly", provides=("ad_cost",),
            ),
        ),
        templates=(promo, *extra),
        metrics=(
            Metric(
                id="ad_cost",
                name="推广费用",
                source="promotion",
                value=ValueExpr(op="sum", of=("spend",)),
            ),
        ),
    )


def _wider_model() -> Model:
    """多接了一个平台的模型：同一个 spend 角色在抖音那张表里叫「整体消耗」。

    真实模型就是这样——推广这个数据源下有淘宝和抖音两个模板，两边列名不同。
    词汇表是接表接出来的，所以模型见过的写法越多，认改名的能力越强。
    """
    douyin = Template(
        id="promo_douyin_v1",
        source="promotion",
        name="抖音推广",
        match_columns=("日期", "整体消耗"),
        bindings=(
            ColumnBinding(role="spend_time", columns=("日期",), kind="time"),
            ColumnBinding(role="spend", columns=("整体消耗",), kind="number"),
        ),
        time_slots={"spend_date": "spend_time"},
    )
    return _model(douyin)


class TestDuplicateColumnNames:
    """两列同名时，一个角色只能来自一列。

    这条必须在两处都成立：草案生成模板时拦住，模型加载时也拦住。只拦一处的话，
    绕过向导手写 YAML 就能把坏配置放进来。
    """

    def _draft(self):
        headers = ["日期", "推广主体ID", "主体名称", "花费", "推广主体ID"]
        rows = [
            ["2025-05-01", "A1", "甲", "10.5", ""],
            ["2025-05-02", "A2", "乙", "20.5", ""],
        ]
        return propose(headers, rows, _model())

    def test_second_occurrence_is_not_mapped_by_default(self):
        draft = self._draft()
        dup = [c for c in draft.columns if c.column == "推广主体ID"]
        assert len(dup) == 2
        assert dup[1].role == "", "同名的第二列默认必须留空，否则两列会抢同一个角色"
        assert "第 1 列" in dup[1].why, "得告诉人默认只映第 1 列，以及怎么改"

    def test_index_addresses_the_column_not_the_name(self):
        """按序号寻址，才可能把两列分别设成不同角色。"""
        draft = self._draft()
        dup = [c for c in draft.columns if c.column == "推广主体ID"]
        assert dup[0].index != dup[1].index

        # 只映第 2 列：合法，因为一个角色仍然只来自一列。
        roles = {c.index: "" for c in draft.columns}
        roles[dup[1].index] = "product_id"
        tpl = draft.template("p2", source="promotion", roles=roles)
        binding = next(b for b in tpl.bindings if b.role == "product_id")
        assert binding.occurrence == 1, "映的是第 2 列，取数就得取第 2 个位置"

    def test_mapping_both_columns_to_one_role_is_refused(self):
        draft = self._draft()
        dup = [c for c in draft.columns if c.column == "推广主体ID"]
        roles = {c.index: "" for c in draft.columns}
        roles[dup[0].index] = "product_id"
        roles[dup[1].index] = "product_id"
        with pytest.raises(ModelError, match="product_id"):
            draft.template("p2", source="promotion", roles=roles)

    def test_model_refuses_it_too(self):
        """手写 YAML 也别想放进来。"""
        bad = Template(
            id="p2",
            source="promotion",
            match_columns=("日期",),
            bindings=(
                ColumnBinding(role="product_id", columns=("推广主体ID",)),
                ColumnBinding(role="product_id", columns=("推广主体ID",), occurrence=1),
            ),
        )
        with pytest.raises(Exception, match="product_id"):
            _model(bad)


class TestSignatureDistinguishesRevisions:
    """改版表的签名必须带上老模板没有的列。

    否则新签名是老模板列集的子集，老版的表以后会被新模板抢走——当期一切正常，
    下一次重算时老账悄悄换了口径，这是最难查的一类错。
    """

    def test_new_column_goes_into_the_signature(self):
        headers = ["日期", "主体名称", "消耗金额"]  # 花费改名成消耗金额
        rows = [["2025-05-01", "甲", "10.5"], ["2025-05-02", "乙", "20.5"]]
        draft = propose(headers, rows, _model(), near_misses=[("promo_v1", ("日期", "主体名称"))])

        roles = {c.index: c.role for c in draft.columns}
        roles[next(c.index for c in draft.columns if c.column == "消耗金额")] = "spend"
        tpl = draft.template("promo_v2", source="promotion", roles=roles)

        assert "消耗金额" in tpl.match_columns, (
            "签名里得有老模板没有的列，否则老版的表会被新模板抢走"
        )

    def test_shared_columns_stay_in_the_signature(self):
        """新增列不占满签名：只靠新增列识别，列名完全不同的表也可能凑巧命中。"""
        headers = ["日期", "主体名称", "消耗金额"]
        rows = [["2025-05-01", "甲", "10.5"]]
        draft = propose(headers, rows, _model(), near_misses=[("promo_v1", ("日期", "主体名称"))])
        roles = {c.index: c.role for c in draft.columns}
        roles[next(c.index for c in draft.columns if c.column == "消耗金额")] = "spend"
        tpl = draft.template("promo_v2", source="promotion", roles=roles)
        assert {"日期", "主体名称"} & set(tpl.match_columns)


class TestDerivedColumnsAreNotMapped:
    """加工过的列不能当原始数据接进来。"""

    def test_pivot_prefix_is_recognized(self):
        headers = ["日期", "花费", "求和项:花费"]
        rows = [["2025-05-01", "10.5", "10.5"]]
        draft = propose(headers, rows, _model())
        pivot = next(c for c in draft.columns if c.column == "求和项:花费")
        assert pivot.derived
        assert pivot.role == ""
        assert "透视" in pivot.why, "得说清它是从别的列汇总出来的，而不是只说不映"

    def test_pivot_judgement_is_shared_with_the_engine(self):
        """判据只该有一份。两处各写一套，迟早一处认另一处不认。"""
        from ledger.engine.derivative import PIVOT_PREFIXES

        assert "求和项:" in PIVOT_PREFIXES


class TestSuggestionsStayHonest:
    """没依据的时候不能装作有依据。

    万相台那张表 78 列，而推广这个数据源只有 spend 一个数字角色。逐列都写
    「最像的是 spend」的话，这句话不含信息，还会诱导人把「展现量」映成花费。
    """

    def test_shape_alone_does_not_claim_a_best_guess(self):
        headers = ["日期", "花费", "展现量", "点击率", "收藏宝贝数"]
        rows = [["2025-05-01", "10.5", "1234", "0.05", "7"]]
        draft = propose(headers, rows, _model())

        noise = [c for c in draft.columns if c.column in {"展现量", "点击率", "收藏宝贝数"}]
        assert noise, "样本列得都在"
        for col in noise:
            assert "最像的是" not in col.why, (
                f"「{col.column}」只是形态像数字，不该说它最像 spend：{col.why}"
            )

    def _guess(self, column: str):
        draft = propose(
            ["日期", column, "主体名称"], [["2025-05-01", "10.5", "甲"]], _model(),
            near_misses=[("promo_v1", ("日期", "主体名称"))],
        )
        return next(c for c in draft.columns if c.column == column)

    def test_a_near_identical_name_gets_the_role_prefilled(self):
        """名字真像的时候要敢填。不然人得在几十个角色里自己找。"""
        guess = self._guess("总花费")
        assert guess.role == "spend"
        assert guess.confidence == "guess", "字面像不是确证，不能默认勾上"
        assert "花费" in guess.why, "得说清是跟哪个列名对上的"

    def test_a_renamed_column_is_offered_but_not_prefilled(self):
        """改名幅度大到不敢直接填的，要排在候选第一位。

        「花费」改成「花费金额」是平台常见的改法，但字面相似度已经不足以当依据。
        这一档的正确行为是「摆在最顺手的位置让人点」，不是「替人决定」。
        """
        guess = self._guess("花费金额")
        assert guess.role == "", "不够像就不能替人填，填错是静默少算钱"
        assert guess.alternatives[:1] == ("spend",), "但得摆在候选第一位"
        assert "最像的是 spend" in guess.why

    def test_shape_only_match_says_so_plainly(self):
        """连名字都不像、只是形态相同的，必须说清「只是形态相同」。"""
        guess = self._guess("消耗金额")
        assert guess.role == ""
        assert "形态上" in guess.why
        assert "最像的是" not in guess.why, "只有形态相同就说「最像」，是把巧合说成依据"
        assert guess.no_name_match, "界面要靠这个标记把这一档收拢，不然同一句话重复几十遍"


class TestWideTablesStayReadable:
    """几十列的表要能扫得动。

    万相台那张表 78 列，其中 71 列是平台自带的展现量、转化率、投产比。平铺出来
    每一行都占着和「消耗金额」一样的视觉分量，真正要拍板的那几列就被埋掉了。
    界面靠 no_name_match 分两段，所以这个标记必须只落在该收拢的那一档上。
    """

    HEADERS = [
        "日期", "主体名称", "消耗金额",          # 要拍板：一个改名的金额列
        "求和项:花费",                          # 要拍板：透视字段，得说清为什么不映
        "展现量", "点击率", "投入产出比", "收藏宝贝数",  # 该收拢
    ]
    ROW = [["2025-05-01", "甲", "10.5", "10.5", "1234", "0.05", "3.2", "7"]]

    def _folded(self, model):
        draft = propose(
            self.HEADERS, self.ROW, model,
            near_misses=[("promo_v1", ("日期", "主体名称"))],
        )
        return {c.column for c in draft.columns if c.no_name_match}

    def test_platform_own_metric_columns_are_folded(self):
        folded = self._folded(_wider_model())
        assert folded == {"展现量", "点击率", "投入产出比", "收藏宝贝数"}, (
            "收拢的只该是列名对不上、纯靠形态凑的那些"
        )

    def test_the_columns_that_matter_stay_in_front(self):
        folded = self._folded(_wider_model())
        assert "消耗金额" not in folded, "改了名的金额列必须留在前面让人拍板"
        assert "求和项:花费" not in folded, "透视字段要当面说清为什么不映，不能混进去"
        assert "日期" not in folded and "主体名称" not in folded

    def test_vocabulary_grows_with_every_table_onboarded(self):
        """接的表越多，改名越认得出来。这不是巧合，是这个设计的要点。

        「消耗金额」跟「花费」字面上毫无关系，所以只见过淘宝那张表的模型认不出它，
        只能把它收拢起来等人自己发现。而抖音那张表把同一个角色叫「整体消耗」——
        有了这个写法，「消耗金额」就跟它对上了，于是被提到前面。

        换句话说，词汇表不是维护出来的，是接表接出来的。所以这里不能硬编码同义词表：
        那样每接一个平台都要改代码，而漏改的表现是「这一列没人提醒」。
        """
        assert "消耗金额" in self._folded(_model()), (
            "只见过「花费」的模型认不出「消耗金额」，只能收拢——这是诚实的"
        )
        assert "消耗金额" not in self._folded(_wider_model()), (
            "见过「整体消耗」之后就该认出来"
        )


class TestShapeReading:
    """形态是从值看出来的，不是从列名猜的。看错形态会一路错到类型声明。"""

    @pytest.mark.parametrize(
        ("values", "want"),
        [
            (["10.5", "-3", "1,234.00"], "number"),
            (["2025-05-01", "2025-05-02"], "time"),
            (["JD0012345678", "SF0099"], "id"),
            (["甲店", "乙店"], "text"),
            ([], "empty"),
        ],
    )
    def test_reads_shape_from_values(self, values, want):
        shape, _ = shape_of(values, ())
        assert shape == want

    def test_null_tokens_do_not_turn_numbers_into_text(self):
        """导出文件里空值常写成「-」。把它当数据看，整列就被判成文本，
        于是类型声明写不出 number，金额列按文本处理，求和直接报错。"""
        shape, _ = shape_of(["10.5", "-", "20.5"], ("-",))
        assert shape == "number"

    def test_long_digits_are_ids_not_numbers(self):
        """订单号是 19 位数字。判成 number 会走浮点，末几位精度丢掉，
        然后商品成本静默挂不上订单。"""
        shape, _ = shape_of(["1234567890123456789", "2234567890123456789"], ())
        assert shape == "id"


class TestDryRunCatchesTotalRowTraps:
    """合计行相关的两种错，都是纸面上看不出来的。"""

    def _table(self, tmp_path, rows):
        path = write_xlsx(tmp_path / "t.xlsx", rows, sheet="推广")
        tables = parse(path)
        return next(t for t in tables if t.headers)

    def test_unmarked_total_row_doubles_every_amount(self, tmp_path):
        from ledger.onboard import DryRun, _check_total_row

        rows = [["订单号", "金额"]]
        rows += [[f"O{i}", 10.0] for i in range(1, 11)]
        rows += [["", 100.0]]  # 表底合计
        table = self._table(tmp_path, rows)

        tpl = Template(
            id="x", source="promotion", match_columns=("订单号", "金额"),
            bindings=(
                ColumnBinding(role="order_id", columns=("订单号",)),
                ColumnBinding(role="amount", columns=("金额",), kind="number"),
            ),
        )
        out = DryRun()
        _check_total_row(table, tpl, out)
        assert out.errors, "合计行没被拦住的话，每一列金额都会翻倍"
        assert "合计行" in out.errors[0]

    def test_marker_that_eats_the_data_is_refused(self, tmp_path):
        """合计行标记选到一列大片是空的，等于宣布「全表都是合计行」。"""
        from ledger.onboard import DryRun, _check_drop_rate

        rows = [["订单号", "备注", "金额"]]
        rows += [[f"O{i}", "", 10.0] for i in range(1, 20)]
        rows += [["O20", "有备注", 10.0]]
        table = self._table(tmp_path, rows)

        tpl = Template(
            id="x", source="promotion", match_columns=("订单号", "金额"),
            bindings=(
                ColumnBinding(role="order_id", columns=("订单号",)),
                ColumnBinding(role="note", columns=("备注",)),
                ColumnBinding(role="amount", columns=("金额",), kind="number"),
            ),
            total_row_marker="note",
        )
        out = DryRun()
        _check_drop_rate(table, tpl, out)
        assert out.errors, "丢掉 95% 的行必须拦住，否则账里只剩零头"
        assert "%" in out.errors[0], "得把丢掉的比例说出来，光说「有问题」没法查"


class TestPddLikePromotionOpensTheWizard:
    """拼多多推广表同时有「总花费」和「成交花费」，向导不能因此打不开。"""

    def test_only_one_spend_column_is_mapped(self):
        from conftest import MODELS
        from ledger.model.loader import load_model

        model = load_model(MODELS / "cn-ecommerce")
        headers = [
            "日期", "商品ID", "商品名称", "推广场景", "成交花费(元)",
            "总花费(元)", "曝光量", "点击量",
        ]
        draft = propose(
            headers,
            [["2026-05-01", "1", "接考横幅", "稳定成本推广", "10", "12", "100", "8"]],
            model,
            source_hint="promotion",
        )
        spends = [c.column for c in draft.columns if c.role == "spend"]
        assert spends == ["总花费(元)"], spends
        draft.template("tmp_id", source="promotion")
