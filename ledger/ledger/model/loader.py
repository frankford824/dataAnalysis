"""建模数据的加载与校验。

一个模型是一个目录：

    model.yaml       元信息
    sources.yaml     数据源契约
    templates.yaml   模板（表头签名到字段角色）
    metrics.yaml     指标定义
    statement.yaml   公式树
    checks.yaml      校验规则
    dictionary.csv   科目字典

校验失败直接抛错。宁可启动不了，也不要带着错模型算钱。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import (
    Check,
    DictionaryEntry,
    Metric,
    Model,
    SourceContract,
    StatementNode,
    Template,
)

_FILES = {
    "sources": ("sources.yaml", SourceContract),
    "templates": ("templates.yaml", Template),
    "metrics": ("metrics.yaml", Metric),
    "statement": ("statement.yaml", StatementNode),
    "checks": ("checks.yaml", Check),
}


class ModelError(Exception):
    """建模数据有问题。消息里必须说清哪个文件哪一条。"""


def load_model(directory: str | Path) -> Model:
    root = Path(directory)
    if not root.is_dir():
        raise ModelError(f"模型目录不存在：{root}")

    meta = _read_yaml(root / "model.yaml") or {}
    if not isinstance(meta, dict):
        raise ModelError(f"{root / 'model.yaml'} 顶层必须是映射")

    payload: dict[str, Any] = {
        "id": meta.get("id") or root.name,
        "name": meta.get("name") or root.name,
        "version": str(meta.get("version", "1")),
        "currency": meta.get("currency", "CNY"),
    }

    for field, (filename, cls) in _FILES.items():
        path = root / filename
        raw = _read_yaml(path)
        if raw is None:
            payload[field] = ()
            continue
        if not isinstance(raw, list):
            raise ModelError(f"{path} 顶层必须是列表，实际是 {type(raw).__name__}")
        items = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ModelError(f"{path} 第 {i + 1} 条不是映射")
            try:
                items.append(cls(**_tuplify(entry)))
            except ValidationError as exc:
                ident = entry.get("id", f"第 {i + 1} 条")
                raise ModelError(f"{filename} 的 {ident} 有问题：\n{_explain(exc)}") from exc
        payload[field] = tuple(items)

    payload["dictionary"] = _read_dictionary(root / "dictionary.csv")

    try:
        return Model(**payload)
    except ValidationError as exc:
        raise ModelError(f"模型 {payload['id']} 整体校验失败：\n{_explain(exc)}") from exc
    except ValueError as exc:
        raise ModelError(str(exc)) from exc


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ModelError(f"{path} 不是合法 YAML：{exc}") from exc


def _read_dictionary(path: Path) -> tuple[DictionaryEntry, ...]:
    """科目字典用 CSV 而非 YAML：条数多、结构扁平，且方便直接从现有资产导出。"""
    if not path.exists():
        return ()
    entries: list[DictionaryEntry] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            raw = (row.get("raw") or "").strip()
            if not raw:
                continue
            try:
                entries.append(
                    DictionaryEntry(
                        platform=(row.get("platform") or "*").strip(),
                        raw=raw,
                        minor=(row.get("minor") or "").strip(),
                        major=(row.get("major") or "").strip(),
                        naturally_unlinked=_truthy(row.get("naturally_unlinked")),
                    )
                )
            except ValidationError as exc:
                raise ModelError(f"{path} 第 {lineno} 行有问题：\n{_explain(exc)}") from exc
    return tuple(entries)


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "是", "true "}


def _tuplify(obj: Any) -> Any:
    """YAML 给出 list，schema 要 tuple（模型对象是 frozen 的，需可哈希）。"""
    if isinstance(obj, dict):
        return {k: _tuplify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return tuple(_tuplify(v) for v in obj)
    return obj


def _explain(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(顶层)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)
