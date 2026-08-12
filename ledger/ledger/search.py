"""全局检索：一个数、一个订单号、一个科目名，落到哪个文件第几行。

对不上账的时候，人手里通常只有一样东西：一个数字，或者一个订单号。他要问的是
「这笔钱是从哪儿来的」。在这套系统之前，答案要靠在几十兆的工作簿里按 Ctrl+F，
一张表一张表地翻——所以实际上没人查，对不上就手改一个数把它对上。

所以这里查的是事实行，不是汇总。每一条结果都带文件名、工作表、行号，
能一路指回那个格子。

怎么理解输入
------------
不让人先选「我要按什么查」。人手里那个东西长什么样，系统自己认：

    5111236008850009225   长串数字      → 订单号
    -88091.88 / 88091.88  带小数的数    → 金额（正负都匹配，符号约定各表不同）
    推广                   文字          → 科目、费项、文件名

认错的代价只是多几条结果，所以宁可放宽：订单号也当文字去撞科目名，反正真订单号
撞不上科目。认法会原样返回（`kinds`），界面上说出来——「按订单号找到 3 行」比
一份没有出处的结果列表可信得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .money import money_float

#: 一次最多返回多少行。再多人也看不完，而且会把浏览器拖死。
LIMIT = 200

#: 最多翻多少个店期的留档。按账期从新到旧翻，翻够就停——查的多半是最近的账。
MAX_RUNS = 24

#: 金额匹配的容差。事实行留档到分，比这更细的差异是浮点尾数不是另一笔钱。
CENT = 0.005

#: 纯数字且够长就当订单号。淘宝子订单号 19 位、抖音 19 位、1688 是 16 到 19 位；
#: 门槛放在 8 位是为了让人能贴一截去搜，同时不至于把「2026」这种年份当订单号。
_ORDER = re.compile(r"^\d{8,}$")

#: 带小数点或负号的数当金额。纯整数不当金额——「3」更可能是在找别的东西，
#: 而按金额搜 3 会命中几千行。
_AMOUNT = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+\.\d+$")


@dataclass
class Hit:
    """一条命中的事实行。"""

    store_id: str
    store: str
    period: str
    metric: str
    amount: float
    subject: str
    link_key: str
    linked: bool
    file: str
    sheet: str
    row_no: int


@dataclass
class Result:
    query: str
    #: 这次把输入理解成了什么：order / amount / text，可能不止一种。
    kinds: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    #: 命中分布在哪些店期，各多少行。人一眼看出这笔钱牵扯到几家店。
    by_store: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    amount: float = 0.0
    truncated: bool = False
    #: 翻了几个店期的留档、有没有因为上限而没翻完。
    scanned: int = 0
    exhausted: bool = True
    notes: list[str] = field(default_factory=list)


def classify(query: str) -> list[str]:
    """输入长什么样。返回值可能不止一种——宁可多搜一遍也别漏。"""
    q = query.strip()
    if not q:
        return []
    kinds = []
    if _ORDER.match(q):
        kinds.append("order")
    if _AMOUNT.match(q.replace(",", "")) and any(c in q for c in ".-"):
        kinds.append("amount")
    kinds.append("text")
    return kinds


def predicate(query: str, kinds: list[str]) -> pl.Expr | None:
    """把输入翻成一个 polars 条件。

    分开写各种命中方式再 or 起来，而不是拼一个大正则：正则要对每一行做一次
    Python 层的匹配，而这些表达式全在 Rust 里跑，二十多万行的对账表差着一个量级。
    """
    q = query.strip()
    if not q:
        return None
    parts: list[pl.Expr] = []
    if "order" in kinds:
        # 精确等于放在前面：绝大多数情况人贴的就是完整订单号，contains 是给
        # 「只记得后几位」准备的兜底。
        parts.append(pl.col("link_key") == q)
    if "amount" in kinds:
        try:
            value = abs(float(q.replace(",", "")))
        except ValueError:
            pass
        else:
            # 正负都匹配。同一笔钱在订单表里是正的、在对账表里是负的，
            # 人手里那个数是从哪张表抄来的，系统不知道也不该要求他知道。
            parts.append((pl.col("amount").abs() - value).abs() < CENT)
    if "text" in kinds:
        for col in ("subject", "minor", "file_name", "link_key"):
            parts.append(pl.col(col).cast(pl.Utf8).str.contains(q, literal=True))
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = out | p
    return out


def search(states: list[Any], facts_of, model, query: str, *,
           limit: int = LIMIT) -> Result:
    """在留档的事实行里找。

    `states` 是要翻的店期，调用方负责按平台、店铺、账期筛好并排好序——这里不做
    业务筛选，只负责翻和匹配。`facts_of(run_id)` 取一份留档，取不到返回 None。
    """
    kinds = classify(query)
    res = Result(query=query.strip(), kinds=kinds)
    cond = predicate(query, kinds)
    if cond is None:
        return res

    names = {s.id: s.name for s in model.stores}
    per_store: dict[tuple[str, str], dict[str, Any]] = {}
    for state in states[:MAX_RUNS]:
        res.scanned += 1
        path = facts_of(state.run_id)
        if path is None:
            continue
        try:
            frame = (
                pl.scan_parquet(path)
                .filter(cond)
                .select("metric_id", "amount", "subject", "minor", "link_key",
                        "linked", "file_name", "sheet", "row_no")
                .collect()
            )
        except Exception as exc:  # 留档损坏不该让整个检索报错
            res.notes.append(f"{state.store_id} {state.period} 的留档读不了：{exc}")
            continue
        if frame.is_empty():
            continue

        slot = per_store.setdefault((state.store_id, state.period), {
            "store_id": state.store_id,
            "store": names.get(state.store_id, state.store_id),
            "period": state.period,
            "rows": 0, "amount": 0.0,
        })
        slot["rows"] += frame.height
        slot["amount"] += float(frame.get_column("amount").sum() or 0.0)
        res.total += frame.height
        res.amount += float(frame.get_column("amount").sum() or 0.0)

        # 够了就不再往结果里塞，但继续翻剩下的店期——「这笔钱在三家店都出现过」
        # 这句话比多给一百行明细有用得多。
        if len(res.hits) < limit:
            head = frame.sort(pl.col("amount").abs(), descending=True).head(limit - len(res.hits))
            res.hits.extend(
                Hit(
                    store_id=state.store_id,
                    store=names.get(state.store_id, state.store_id),
                    period=state.period,
                    metric=_metric_name(model, r["metric_id"]),
                    amount=r["amount"],
                    subject=r["minor"] or r["subject"] or "",
                    link_key=r["link_key"] or "",
                    linked=bool(r["linked"]),
                    file=r["file_name"] or "",
                    sheet=r["sheet"] or "",
                    row_no=r["row_no"] or 0,
                )
                for r in head.to_dicts()
            )

    res.amount = money_float(res.amount)
    for slot in per_store.values():
        slot["amount"] = money_float(slot["amount"])
    res.by_store = sorted(per_store.values(), key=lambda d: -d["rows"])
    res.truncated = res.total > len(res.hits)
    res.exhausted = len(states) <= MAX_RUNS
    if not res.exhausted:
        res.notes.append(
            f"只翻了最近 {MAX_RUNS} 个店期，还有 {len(states) - MAX_RUNS} 个没翻。"
            f"缩小店铺或账期范围能翻到更早的。"
        )
    return res


def _metric_name(model, metric_id: str) -> str:
    return next((m.name for m in model.metrics if m.id == metric_id), metric_id or "")


def to_dict(res: Result) -> dict[str, Any]:
    return {
        "query": res.query,
        "kinds": res.kinds,
        "total": res.total,
        "amount": res.amount,
        "truncated": res.truncated,
        "scanned": res.scanned,
        "exhausted": res.exhausted,
        "by_store": res.by_store,
        "notes": res.notes,
        "hits": [h.__dict__ for h in res.hits],
    }


__all__ = ["LIMIT", "Hit", "Result", "classify", "predicate", "search", "to_dict"]
