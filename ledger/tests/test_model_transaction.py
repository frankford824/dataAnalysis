from concurrent.futures import ThreadPoolExecutor

import pytest

from ledger.model.config import add_store
from ledger.model.loader import ModelError, load_model
from ledger.model.schema import Store
from ledger.model.transaction import assert_revision, model_revision


@pytest.fixture
def model_dir(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    (root / "model.yaml").write_text("id: t\nname: 测试模型\n", encoding="utf-8")
    (root / "stores.yaml").write_text(
        "- id: taobao_a\n  name: 淘宝甲店\n  platform: taobao\n",
        encoding="utf-8",
    )
    return root


def test_concurrent_model_writes_do_not_lose_each_other(model_dir):
    stores = (
        Store(id="pdd_c", name="拼多多丙店", platform="pdd"),
        Store(id="jd_d", name="京东丁店", platform="jd"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda store: add_store(model_dir, store), stores))
    assert {store.id for store in load_model(model_dir).stores} >= {"pdd_c", "jd_d"}


def test_stale_revision_is_rejected(model_dir):
    stale = model_revision(model_dir)
    add_store(model_dir, Store(id="pdd_c", name="拼多多丙店", platform="pdd"))
    with pytest.raises(ModelError, match="试跑之后"):
        assert_revision(model_dir, stale)
