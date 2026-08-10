"""改配置。

模型是一目录 YAML，人也能直接编辑，但不该只能这样改——要人去改 YAML 才能
登记一家店，这就不是产品而是脚手架了。所以配置项要能从界面和命令行改，
写回同一份文件。

写回有三条硬要求：

  一、保住注释。stores.yaml 里每条主体都记着它是从哪张表哪一列读出来的，
      那些取证说明比字段本身更值钱。PyYAML 一写回全部冲掉，所以用 ruamel。
  二、写完必须能通过模型校验。校验不过就回滚，绝不留下一个加载不了的模型——
      那会让整个系统起不来。
  三、原子写。写临时文件再改名，中途断电也不会剩下半个文件。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .loader import ModelError, load_model
from .schema import Store

#: 店铺的哪些字段允许改。
#:
#: id 不在里面：它是关联键，改了等于换一家店，历史账会对不上。name 也不在：
#: 认文件靠它，要改名就加 aliases，把旧名留着，否则以前交过的文件立刻认不出。
EDITABLE = ("entity", "entity_tax_id", "archived", "aliases", "note")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    # 主体名和说明都比较长，默认 80 列会把中文折得很难读。
    y.width = 4096
    return y


def update_store(model_dir: str | Path, store_id: str, changes: dict[str, Any]) -> Store:
    """改一家店的配置。返回改完之后的这家店。"""
    root = Path(model_dir)
    path = root / "stores.yaml"
    if not path.exists():
        raise ModelError(f"店铺注册表不存在：{path}")

    bad = [k for k in changes if k not in EDITABLE]
    if bad:
        raise ModelError(
            f"这些字段不让改：{'、'.join(bad)}。可改的是 {'、'.join(EDITABLE)}。"
            f"id 是关联键，name 是认文件的依据，改了历史账就对不上——要改名请加别名。"
        )

    y = _yaml()
    with path.open(encoding="utf-8") as fh:
        doc = y.load(fh)
    if not isinstance(doc, list):
        raise ModelError(f"{path} 顶层必须是列表")

    entry = next((e for e in doc if isinstance(e, dict) and e.get("id") == store_id), None)
    if entry is None:
        raise ModelError(f"注册表里没有 {store_id} 这家店")

    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        elif key == "aliases":
            entry[key] = list(value)
        else:
            entry[key] = value

    _write_back(root, path, doc, y)
    return load_model(root).store(store_id)


def add_store(model_dir: str | Path, store: Store) -> Store:
    """登记一家新店。"""
    root = Path(model_dir)
    path = root / "stores.yaml"
    y = _yaml()
    doc: Any = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            doc = y.load(fh) or []
    if not isinstance(doc, list):
        raise ModelError(f"{path} 顶层必须是列表")
    if any(isinstance(e, dict) and e.get("id") == store.id for e in doc):
        raise ModelError(f"{store.id} 已经登记过了")
    if any(isinstance(e, dict) and e.get("name") == store.name for e in doc):
        raise ModelError(f"已经有一家店叫 {store.name} 了。同名会让文件认不清归谁。")

    entry: dict[str, Any] = {"id": store.id, "name": store.name, "platform": store.platform}
    for key in EDITABLE:
        value = getattr(store, key)
        if key == "aliases" and value:
            entry[key] = list(value)
        elif key == "archived" and value:
            entry[key] = True
        elif key not in ("aliases", "archived") and value:
            entry[key] = value
    doc.append(entry)

    _write_back(root, path, doc, y)
    return load_model(root).store(store.id)


def _write_back(root: Path, path: Path, doc: Any, y: YAML) -> None:
    """原子写，然后校验；校验不过就还原。

    模型加载不了的话整个系统起不来，所以宁可拒绝这次修改，也不能留个坏文件。
    """
    before = path.read_bytes() if path.exists() else None
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".stores-", suffix=".yaml", delete=False
    )
    try:
        with tmp:
            y.dump(doc, tmp)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    try:
        load_model(root)
    except ModelError:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
        raise
