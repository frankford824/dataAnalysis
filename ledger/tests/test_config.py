"""配置要能改，而且不能改坏。

法人主体这类东西数据里读不出来——支付宝和微信账单都不带主体信息，只能由人告诉
引擎。要人去改 YAML 才能配一家店，那是脚手架不是产品，所以界面和命令行都得能改，
写回同一份文件。

写回的三条要求各有一个失败模式，都比"改不了"更坏：

  丢注释    stores.yaml 里每条主体都记着它是从哪张表哪一列读出来的。那些取证说明
            比字段本身值钱，冲掉了以后没人知道这个值凭什么是这个值。
  写坏模型  模型加载不了，整个系统起不来。所以写完立刻校验，不过就回滚。
  改了关联键 id 是关联键、name 是认文件的依据，改了历史账全部对不上。所以不让改。
"""

from __future__ import annotations

import pytest

from ledger.model.config import (
    EDITABLE,
    add_store,
    add_template,
    drop_template,
    update_store,
)
from ledger.model.loader import ModelError, load_model
from ledger.model.schema import (
    ColumnBinding,
    ParseOptions,
    SourceContract,
    Store,
    Template,
)

HEAD = """\
# 店铺注册表。
#
# 主体名是从数据里读出来的，读不到的只能配。

"""

BODY = """\
- id: taobao_a
  name: 淘宝甲店
  platform: taobao
  entity: ""
  note: 主体待配，支付宝账单不带主体信息。

- id: douyin_b
  name: 抖音乙店
  platform: douyin
  entity: 某某供应链有限公司
  # 这一行是行内注释，也得留着
  entity_tax_id: "91330782MACX8XB45T"
"""


@pytest.fixture
def model_dir(tmp_path):
    """一个能加载的最小模型。只有店铺注册表，其余为空。"""
    root = tmp_path / "m"
    root.mkdir()
    (root / "model.yaml").write_text("id: t\nname: 测试模型\n", encoding="utf-8")
    (root / "stores.yaml").write_text(HEAD + BODY, encoding="utf-8")
    return root


class TestUpdate:
    def test_sets_entity(self, model_dir):
        store = update_store(model_dir, "taobao_a", {"entity": "甲主体有限公司"})
        assert store.entity == "甲主体有限公司"
        assert load_model(model_dir).store("taobao_a").entity == "甲主体有限公司"

    def test_keeps_comments(self, model_dir):
        update_store(model_dir, "taobao_a", {"entity": "甲主体有限公司"})
        text = (model_dir / "stores.yaml").read_text(encoding="utf-8")
        assert "# 店铺注册表。" in text
        assert "主体名是从数据里读出来的" in text
        assert "这一行是行内注释" in text

    def test_keeps_other_stores_untouched(self, model_dir):
        update_store(model_dir, "taobao_a", {"entity": "甲主体有限公司"})
        other = load_model(model_dir).store("douyin_b")
        assert other.entity == "某某供应链有限公司"
        assert other.entity_tax_id == "91330782MACX8XB45T"

    def test_clearing_entity_is_allowed(self, model_dir):
        """配错了要能改回未配置，不能只进不出。"""
        update_store(model_dir, "taobao_a", {"entity": "填错了"})
        store = update_store(model_dir, "taobao_a", {"entity": ""})
        assert store.entity == ""

    def test_none_removes_the_key(self, model_dir):
        update_store(model_dir, "douyin_b", {"entity_tax_id": None})
        assert load_model(model_dir).store("douyin_b").entity_tax_id == ""
        assert "91330782" not in (model_dir / "stores.yaml").read_text(encoding="utf-8")

    def test_aliases_round_trip(self, model_dir):
        store = update_store(model_dir, "taobao_a", {"aliases": ["喜必顺旧名", "甲店"]})
        assert store.aliases == ("喜必顺旧名", "甲店")
        assert store.owns("聚水潭成本-喜必顺旧名.xlsx")

    def test_archive(self, model_dir):
        assert update_store(model_dir, "taobao_a", {"archived": True}).archived
        assert not update_store(model_dir, "taobao_a", {"archived": False}).archived

    def test_survives_repeated_writes(self, model_dir):
        """反复改不能让文件慢慢烂掉。"""
        for i in range(5):
            update_store(model_dir, "taobao_a", {"entity": f"主体{i}"})
        text = (model_dir / "stores.yaml").read_text(encoding="utf-8")
        assert "# 店铺注册表。" in text
        assert load_model(model_dir).store("taobao_a").entity == "主体4"
        assert len(load_model(model_dir).stores) == 2


class TestRefusals:
    def test_id_and_name_are_not_editable(self):
        assert "id" not in EDITABLE
        assert "name" not in EDITABLE

    @pytest.mark.parametrize("field", ["id", "name", "platform"])
    def test_refuses_key_fields(self, model_dir, field):
        with pytest.raises(ModelError) as exc:
            update_store(model_dir, "taobao_a", {field: "随便"})
        assert field in str(exc.value)

    def test_unknown_store(self, model_dir):
        with pytest.raises(ModelError):
            update_store(model_dir, "没这家店", {"entity": "x"})

    def test_rolls_back_when_result_would_not_load(self, model_dir):
        """写出来的东西加载不了就还原。宁可拒绝这次修改，也不能留个坏模型。"""
        before = (model_dir / "stores.yaml").read_text(encoding="utf-8")
        with pytest.raises((ModelError, Exception)):
            # aliases 给个不能转成字符串元组的东西
            update_store(model_dir, "taobao_a", {"aliases": [{"nested": "dict"}]})
        assert (model_dir / "stores.yaml").read_text(encoding="utf-8") == before
        assert load_model(model_dir).store("taobao_a").entity == ""


class TestAdd:
    def test_adds_store(self, model_dir):
        store = add_store(model_dir, Store(
            id="pdd_c", name="拼多多丙店", platform="pdd", entity="丙主体有限公司",
        ))
        assert store.id == "pdd_c"
        model = load_model(model_dir)
        assert len(model.stores) == 3
        assert model.store_of("聚水潭成本-拼多多丙店.xlsx").id == "pdd_c"

    def test_keeps_comments(self, model_dir):
        add_store(model_dir, Store(id="pdd_c", name="拼多多丙店", platform="pdd"))
        assert "# 店铺注册表。" in (model_dir / "stores.yaml").read_text(encoding="utf-8")

    def test_refuses_duplicate_id(self, model_dir):
        with pytest.raises(ModelError):
            add_store(model_dir, Store(id="taobao_a", name="另一个名", platform="taobao"))

    def test_refuses_duplicate_name(self, model_dir):
        """同名会让文件认不清归谁——文件名里带的就是店名。"""
        with pytest.raises(ModelError) as exc:
            add_store(model_dir, Store(id="taobao_z", name="淘宝甲店", platform="taobao"))
        assert "淘宝甲店" in str(exc.value)

    def test_omits_empty_fields(self, model_dir):
        """没填的东西不要写成一堆空串，文件是给人看的。"""
        add_store(model_dir, Store(id="pdd_c", name="拼多多丙店", platform="pdd"))
        text = (model_dir / "stores.yaml").read_text(encoding="utf-8")
        tail = text[text.index("pdd_c"):]
        assert "entity:" not in tail
        assert "aliases:" not in tail


TEMPLATES = """\
# 模板：把某一种表头长相绑定到字段角色上。
#
# 全部照实测的表头写，列名一个字都不能改——实测过一次别人把「线上子订单编号」
# 写成「线上子订单号」，少一个「编」字，结果订单级成本覆盖率直接掉到 0。

# ======================================================================== #
# 脊柱
# ======================================================================== #
- id: order_v1
  source: order_detail
  name: 订单明细
  match_columns: [子订单编号, 主订单编号]
  parse: {header_row: 1}
  bindings:
    - {role: sub_order_id, columns: [子订单编号]}
    - {role: order_id, columns: [主订单编号]}
"""

SOURCES = """\
# 数据源：一份数据的契约。谁交、多久交一次、供给哪些指标。

- id: order_detail
  name: 订单明细
  owner_role: shop_owner
  cadence: daily
  is_spine: true
"""


@pytest.fixture
def template_dir(tmp_path):
    """一个带模板和数据源的模型。注释和缩进都照真实文件的样子写。"""
    root = tmp_path / "m"
    root.mkdir()
    (root / "model.yaml").write_text("id: t\nname: 测试模型\n", encoding="utf-8")
    (root / "stores.yaml").write_text(HEAD + BODY, encoding="utf-8")
    (root / "templates.yaml").write_text(TEMPLATES, encoding="utf-8")
    (root / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    return root


def _promo() -> Template:
    return Template(
        id="promo_v1",
        source="order_detail",
        name="推广花费",
        match_columns=("日期", "花费"),
        bindings=(
            ColumnBinding(role="spend_time", columns=("日期",), kind="time"),
            ColumnBinding(role="spend", columns=("花费",), kind="number"),
        ),
    )


class TestAddTemplate:
    """接表向导靠这个写模型。它写的是人手写的文件，所以只许追加，不许重排。

    这里的失败模式跟店铺不一样，也更隐蔽：模板文件回写一遍，语义一个字节没变，
    但嵌套序列的缩进从 4 格变成 2 格，git 上是 600 行改动。改了什么审不出来，
    于是这种写回没人敢用，向导也就白做了。
    """

    def test_adds_the_template(self, template_dir):
        saved = add_template(template_dir, _promo(), by="张三")
        assert saved.id == "promo_v1"
        model = load_model(template_dir)
        assert len(model.templates) == 2
        assert {b.role for b in model.template("promo_v1").bindings} == {"spend_time", "spend"}

    def test_writes_back_everything_it_was_given(self, template_dir):
        """读回来的必须和确认的那份一模一样。

        逐字段列举写了什么是靠不住的：漏掉一个字段不会报错，只会静默算错。实测漏过
        `total_row_marker`——试跑时拿它验过「合计行会被丢掉」，落库却没写进去，于是
        表底那行合计混进数据，每一列金额刚好翻倍。所以这里整个对象比。
        """
        tpl = Template(
            id="promo_v1",
            source="order_detail",
            name="推广花费",
            match_columns=("日期", "花费"),
            parse=ParseOptions(header_row=1),
            bindings=(
                ColumnBinding(role="spend_time", columns=("日期",), kind="time"),
                ColumnBinding(role="spend", columns=("花费",), kind="number"),
                ColumnBinding(role="order_id", columns=("订单号",), occurrence=1, required=False),
            ),
            time_slots={"spend_date": "spend_time"},
            total_row_marker="spend_time",
            note="接表向导登记的测试模板",
        )
        add_template(template_dir, tpl)
        assert load_model(template_dir).template("promo_v1") == tpl

    def test_touches_nothing_that_was_already_there(self, template_dir):
        """已有内容必须一个字节都不动。"""
        before = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        add_template(template_dir, _promo(), by="张三")
        after = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        assert after.startswith(before), (
            "新记录只该追加在末尾。前面的部分变了，说明整个文档被重写了一遍"
        )

    def test_keeps_the_file_header_and_section_comments(self, template_dir):
        add_template(template_dir, _promo(), by="张三")
        text = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        assert text.startswith("# 模板：")
        assert "少一个「编」字" in text, "那段取证比字段本身值钱"
        assert "# 脊柱" in text

    def test_keeps_the_existing_indent_style(self, template_dir):
        """新记录的缩进要跟文件里现有的一致，否则同一个文件两种风格。"""
        add_template(template_dir, _promo(), by="张三")
        text = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        tail = text[text.index("promo_v1"):]
        assert "\n    - {role: spend_time" in tail, (
            f"嵌套序列的横杠该在第 4 列，跟 order_v1 一样。实际写成了：\n{tail}"
        )

    def test_records_who_did_it(self, template_dir):
        add_template(template_dir, _promo(), by="张三")
        text = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        assert "张三" in text, "模板是人确认的，得记下是谁——出错时要找得到人问"

    def test_registers_a_new_source_together(self, template_dir):
        """新数据源和模板要一起写，不能留悬空引用。"""
        tpl = _promo().model_copy(update={"source": "promotion"})
        add_template(template_dir, tpl, source=SourceContract(
            id="promotion", name="推广", owner_role="operations", cadence="monthly",
        ))
        model = load_model(template_dir)
        assert model.template("promo_v1").source == "promotion"
        assert any(s.id == "promotion" for s in model.sources)
        assert (template_dir / "sources.yaml").read_text(encoding="utf-8").startswith("# 数据源：")

    def test_refuses_a_dangling_source(self, template_dir):
        tpl = _promo().model_copy(update={"source": "没这个数据源"})
        with pytest.raises(ModelError, match="没这个数据源"):
            add_template(template_dir, tpl)

    def test_rolls_back_both_files_when_the_result_would_not_load(self, template_dir):
        """一半写成了比全没写更坏：数据源进去了模板没进去，模型照样加载不了。"""
        before = {
            name: (template_dir / name).read_text(encoding="utf-8")
            for name in ("templates.yaml", "sources.yaml")
        }
        # 角色绑到两列上，模型校验会拒绝——用它来触发写完之后的校验失败。
        bad = _promo().model_copy(update={"bindings": (
            ColumnBinding(role="spend", columns=("花费",)),
            ColumnBinding(role="spend", columns=("花费",), occurrence=1),
        )})
        with pytest.raises(Exception):
            add_template(template_dir, bad, source=SourceContract(
                id="promotion", name="推广", owner_role="operations", cadence="monthly",
            ))
        for name, text in before.items():
            assert (template_dir / name).read_text(encoding="utf-8") == text, (
                f"{name} 没还原干净"
            )
        assert len(load_model(template_dir).templates) == 1

    def test_survives_repeated_adds(self, template_dir):
        """接一张又一张，文件不能慢慢烂掉。"""
        for i in range(4):
            add_template(template_dir, _promo().model_copy(update={
                "id": f"promo_v{i}",
                "match_columns": (f"日期{i}", "花费"),
            }))
        text = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        assert text.startswith("# 模板：")
        assert "# 脊柱" in text
        assert len(load_model(template_dir).templates) == 5


class TestDropTemplate:
    """人主动撤模板。撤掉一条不能赔掉整份文档的注释。"""

    def test_drops_it(self, template_dir):
        add_template(template_dir, _promo())
        drop_template(template_dir, "promo_v1")
        assert [t.id for t in load_model(template_dir).templates] == ["order_v1"]

    def test_keeps_the_file_header(self, template_dir):
        """曾经用列表推导重建文档，一撤模板就把文件头和分节注释全删了。"""
        add_template(template_dir, _promo())
        drop_template(template_dir, "promo_v1")
        text = (template_dir / "templates.yaml").read_text(encoding="utf-8")
        assert text.startswith("# 模板：")
        assert "少一个「编」字" in text
        assert "# 脊柱" in text
