"""建模数据的加载与校验。

一个模型是一个目录：

    model.yaml       元信息
    platforms.yaml   平台清单
    stores.yaml      店铺注册表（店铺、平台、法人主体）
    sources.yaml     数据源契约
    templates.yaml   模板（表头签名到字段角色）
    metrics.yaml     指标定义
    statement.yaml   公式树
    checks.yaml      校验规则
    dictionary.csv   科目字典
    fee-rules.csv    界面上配的归类规则（备注/业务类型/业务描述 → 口径项）
    commission.csv   提成配置（商品-人-比例，按生效日期）
    overheads.csv    公摊费用（账期 → 全公司总额，摊到各店）

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
    CommissionRule,
    DictionaryEntry,
    FeeRule,
    Metric,
    Model,
    Overhead,
    Platform,
    SourceContract,
    StatementNode,
    Store,
    Template,
)

_FILES = {
    "platforms": ("platforms.yaml", Platform),
    "stores": ("stores.yaml", Store),
    "sources": ("sources.yaml", SourceContract),
    "templates": ("templates.yaml", Template),
    "metrics": ("metrics.yaml", Metric),
    "statement": ("statement.yaml", StatementNode),
    "checks": ("checks.yaml", Check),
}


#: 界面配的归类规则存这儿。列顺序就是写回时的列顺序。
FEE_RULES = "fee-rules.csv"

FEE_RULE_COLUMNS = (
    "stage", "platform", "field", "how", "value", "major", "minor",
    "exclude", "count_without_order", "note", "by", "at",
)


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
    payload["fee_rules"] = _read_fee_rules(root / FEE_RULES)
    payload["commission"] = _read_commission(root / "commission.csv")
    payload["overheads"] = _read_overheads(root / "overheads.csv")

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


def _read_fee_rules(path: Path) -> tuple[FeeRule, ...]:
    """界面上配的归类规则。

    次序就是文件里的行序，这一点和别的 CSV 不同：规则链「第一条命中的生效」，
    所以调整行序是在改语义。写回这份文件必须整份写，不能按某一列排序。
    """
    if not path.exists():
        return ()
    rules: list[FeeRule] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            value = (row.get("value") or "").strip()
            if not value:
                continue
            try:
                rules.append(
                    FeeRule(
                        platform=(row.get("platform") or "*").strip() or "*",
                        field=(row.get("field") or "subject").strip() or "subject",
                        how=(row.get("how") or "exact").strip() or "exact",  # type: ignore[arg-type]
                        value=value,
                        major=(row.get("major") or "").strip(),
                        minor=(row.get("minor") or "").strip(),
                        exclude=_truthy(row.get("exclude")),
                        count_without_order=_truthy(row.get("count_without_order")),
                        stage=(row.get("stage") or "after").strip() or "after",  # type: ignore[arg-type]
                        note=(row.get("note") or "").strip(),
                        by=(row.get("by") or "").strip(),
                        at=(row.get("at") or "").strip(),
                    )
                )
            except ValidationError as exc:
                raise ModelError(f"{path} 第 {lineno} 行有问题：\n{_explain(exc)}") from exc
    return tuple(rules)


def _read_commission(path: Path) -> tuple[CommissionRule, ...]:
    """提成配置。用 CSV 的理由和科目字典一样：条数多、结构扁平，而且它本来就是
    从表格里来的——业务改提成是在 Excel 里改，让他们改完直接存成 CSV 最省事。

    比例允许写成 `3%` 或 `0.03`。这不是纵容随手写：两种写法在业务表格里都真实存在，
    而 `3` 和 `0.03` 差一百倍，靠人在录入时记住该写哪种，早晚会错一次。宁可两种都认。
    """
    if not path.exists():
        return ()
    rules: list[CommissionRule] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            person = (row.get("person") or "").strip()
            store = (row.get("store") or "").strip()
            if not person and not store:
                continue
            try:
                rules.append(
                    CommissionRule(
                        effective_from=(row.get("effective_from") or "").strip(),
                        store=store,
                        product_id=(row.get("product_id") or "").strip(),
                        product_name=(row.get("product_name") or "").strip(),
                        person=person,
                        share=_rate(row.get("share"), path, lineno, "share"),
                        total_rate=_rate(row.get("total_rate"), path, lineno, "total_rate"),
                        note=(row.get("note") or "").strip(),
                    )
                )
            except ValidationError as exc:
                raise ModelError(f"{path} 第 {lineno} 行有问题：\n{_explain(exc)}") from exc
    return tuple(rules)


def _read_overheads(path: Path) -> tuple[Overhead, ...]:
    """公摊费用（目前只有兼职工资）。业务维护的那张表就是「月份 → 总额」两列。

    月份两种写法都认：2026-05 和 2605。历史文件里写的是 2605 那种四位数，
    而系统内部一律用 2026-05；只认一种的后果是人照着旧表抄一遍全落不进任何账期，
    而落不进不会报错，只会让兼职这一项静悄悄地是 0。
    """
    if not path.exists():
        return ()
    rows: list[Overhead] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            period = _period(row.get("period"), path, lineno)
            if not period:
                continue
            text = str(row.get("amount") or "").strip().replace(",", "")
            try:
                amount = float(text)
            except ValueError:
                raise ModelError(
                    f"{path} 第 {lineno} 行的 amount 不是数字：{text!r}"
                ) from None
            rows.append(Overhead(period=period, amount=amount,
                                 note=(row.get("note") or "").strip()))
    return tuple(rows)


def _period(value: Any, path: Path, lineno: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 4 and text.isdigit():
        return f"20{text[:2]}-{text[2:]}"
    if len(text) == 7 and text[4] == "-":
        return text
    raise ModelError(f"{path} 第 {lineno} 行的 period 看不懂：{text!r}。写成 2026-05 或 2605。")


def _rate(value: Any, path: Path, lineno: int, column: str) -> float:
    text = str(value or "").strip()
    if not text:
        raise ModelError(f"{path} 第 {lineno} 行的 {column} 是空的")
    percent = text.endswith("%")
    try:
        num = float(text.rstrip("%").strip())
    except ValueError:
        raise ModelError(
            f"{path} 第 {lineno} 行的 {column} 不是数字：{text!r}。"
            f"写成 0.03 或 3% 都行。"
        ) from None
    return num / 100 if percent else num


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
