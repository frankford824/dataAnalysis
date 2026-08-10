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

from ledger.model.config import EDITABLE, add_store, update_store
from ledger.model.loader import ModelError, load_model
from ledger.model.schema import Store

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
