"""展示层：把核算结果翻译成界面能直接渲染的结构。

放在这里而不是各写一份，是因为终端、HTTP 接口、网页三处必须说同一句话。以前
`_as_dict` 待在 cli.py 里，接口层要 `from .cli import _as_dict`——命令行成了库，
两边一改就分叉。

翻译规则只有两条，但都不能省：

  一律出中文名。催人补数据时说「order_detail 没交」，没人知道那是什么表。
  数据不全和算出来是 0 必须分开。前者出破折号，后者出 0.00，混在一起会让人
  拿着缺数据的报表当结论用。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import polars as pl

from .engine.calculate import NodeValue
from .engine.runtime import Slice
from .engine.types import RawTable
from .model.propose import Draft, role_facts
from .model.schema import Model, Store

if TYPE_CHECKING:  # 只为类型标注；运行时导入会让 view 依赖向导层，方向反了
    from .onboard import DryRun


def oneline(text: str) -> str:
    """压成一行。

    模型里的提示语用 YAML 折叠写法，换行会变成空格。中文标点后面本来不该有空格，
    直接压会留下「还没同步， 或者」这种夹缝。
    """
    return re.sub(r"(?<=[，。；：、！？）】」“”])\s+", "", " ".join(text.split()))


def source_name(model: Model, source_id: str) -> str:
    """数据源的中文名。"""
    return next((s.name for s in model.sources if s.id == source_id), source_id)


def metric_name(model: Model, metric_id: str) -> str:
    return next((m.name for m in model.metrics if m.id == metric_id), metric_id)


def store_dict(s: Store) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "platform": s.platform,
        "entity": s.entity, "entity_tax_id": s.entity_tax_id,
        "archived": s.archived, "aliases": list(s.aliases), "note": s.note,
    }


def platform_options(model: Model) -> list[dict[str, str]]:
    """登记店铺时的平台下拉选项。

    也把已在用但没登记的平台带上：模型校验现在会拦下这种情况，但历史模型可能有，
    漏掉的话界面会把这家店的平台显示成空的，看起来像没配。
    """
    out = [{"id": p.id, "name": p.name} for p in model.platforms if not p.archived]
    known = {p["id"] for p in out}
    for s in model.stores:
        if s.platform not in known:
            out.append({"id": s.platform, "name": f"{s.platform}（未登记）"})
            known.add(s.platform)
    return out


# --------------------------------------------------------------------------- #
# 一个店一个账期
# --------------------------------------------------------------------------- #


def slice_dict(sl: Slice, store: Store, model: Model) -> dict[str, Any]:
    """一个账期的完整对外结构。快照存的就是这个，所以字段只增不改语义。"""
    return {
        "store": store.name,
        "store_id": store.id,
        "platform": store.platform,
        "entity": store.entity,
        "period": sl.period,
        "can_close": sl.can_close,
        "statement": _statement(sl, model),
        "findings": [
            {"id": f.check_id, "name": f.name, "passed": f.passed,
             "blocking": f.blocking, "message": oneline(f.message)}
            for f in sl.audit.findings
        ],
        "sources": _sources(sl, model),
        "missing_sources": [source_name(model, s) for s in sl.completeness.missing],
        "quality": _quality(sl, model),
        "unclassified": _unclassified(sl),
        "unlinked_total": sl.audit.unlinked_total,
        "unlinked_buckets": [
            {"label": b[0], "count": b[1], "amount": b[2]}
            for b in sl.audit.unlinked_buckets
        ],
        "rows": int(sl.facts.height),
    }


def statement_order(model: Model) -> list[Any]:
    """报表的阅读顺序：树的前序遍历，先出组、紧跟着它的明细。

    不能照模型文件的声明顺序出。YAML 里为了好写，13 个明细项集中写在前面、5 个分组
    写在后面，直接照抄的结果是一屏明细数字之后才出现「收入」「平台费用」这些小计——
    读的人得自己在脑子里把行归组。

    只展开 `children`（加总关系），不展开 `formula.of`：毛利那种引用了别的组的节点
    一展开就会把整组明细再印一遍。
    """
    by_id = {n.id: n for n in model.statement}
    referenced = {c for n in model.statement for c in n.children}
    out: list[Any] = []
    seen: set[str] = set()

    def emit(node: Any) -> None:
        if node.id in seen:
            return
        seen.add(node.id)
        out.append(node)
        for child in node.children:
            if child in by_id:
                emit(by_id[child])

    for node in model.statement:
        if node.id not in referenced:
            emit(node)
    # 谁都没引用又没被走到的节点也要出，否则它悄无声息地从报表上消失了。
    out.extend(n for n in model.statement if n.id not in seen)
    return out


def reorder_statement(payload: dict[str, Any], model: Model) -> dict[str, Any]:
    """把快照里的报表按当前模型的顺序重排。

    快照冻住的是数字，不该连排版一起冻。已结账的账期按设计不能重算，如果顺序也冻在
    里面，以后每改进一次报表结构，历史账期就永远停在旧排版上。

    模型里已经没有的节点排在最后，不丢掉——它代表当时确实算出来过的一笔钱。
    """
    rank = {n.id: i for i, n in enumerate(statement_order(model))}
    rows = payload.get("statement") or []
    payload["statement"] = sorted(rows, key=lambda r: rank.get(r.get("id", ""), len(rank)))
    return payload


def _statement(sl: Slice, model: Model) -> list[dict[str, Any]]:
    """按报表顺序出。

    直接倒 `sl.nodes` 会把指标级节点也带出来，界面上「商品成本」会重复三行，
    而且顺序是求值顺序不是报表顺序。
    """
    out = []
    for node in statement_order(model):
        nv = sl.nodes.get(node.id)
        if nv is None or not nv.applicable:
            continue
        out.append({
            "id": nv.id, "name": nv.name, "level": nv.level,
            "value": nv.value, "available": nv.available, "display": nv.display,
            "missing_sources": [source_name(model, s) for s in nv.missing_sources],
            "is_total": nv.is_total,
            # 能不能点开看构成。比率行展开成分子分母两组指标，加总出来毫无意义；
            # 加总行本身没有明细。这两类不给点，免得点开一看是笔糊涂账。
            "drillable": (
                nv.display == "amount"
                and not nv.is_total
                and bool(node_metrics(model, nv.id))
            ),
        })
    return out


def _sources(sl: Slice, model: Model) -> list[dict[str, Any]]:
    """这个账期该有哪些表、到了没有、没到是什么原因。交付看板的一行。"""
    out = []
    for sid in [*sl.completeness.arrived, *sl.completeness.missing]:
        arrived = sid in sl.completeness.arrived
        out.append({
            "id": sid,
            "name": source_name(model, sid),
            "arrived": arrived,
            "reason": "" if arrived else oneline(sl.completeness.reasons.get(sid, "还没交")),
        })
    return sorted(out, key=lambda d: (d["arrived"], d["name"]))


def _quality(sl: Slice, model: Model) -> list[dict[str, Any]]:
    """每条指标挂得准不准、盖得全不全。

    命中率高而覆盖率低是最危险的组合：钱少算了一半，但所有关联指标都是绿的。
    所以这两个数必须并排摆出来，而且覆盖率要说清分母是哪批订单。

    两处不出数字而出说明，都是为了不喊狼来了：
      偶发科目不报覆盖率（模型上的 occasional）；
      公司级主表不评命中率——那张表是全公司的运单/打款，单店只认领一部分，
      挂不上的绝大多数属于别的店。
    """
    company_wide = {s.id for s in model.sources if s.company_wide}
    by_id = {m.id: m for m in model.metrics}
    out = []
    for mid, r in sl.link_reports.items():
        metric = by_id.get(mid)
        occasional = bool(metric and metric.occasional)
        shared = bool(metric and metric.source in company_wide)
        out.append({
            "metric": mid,
            "name": metric_name(model, mid),
            "rows": r.total_rows,
            "linked": r.linked_rows,
            "hit_rate": None if shared else r.hit_rate,
            "coverage": None if occasional else r.coverage,
            "covered": r.spine_keys_covered,
            "expected": r.spine_keys,
            "spine_total": r.spine_keys_total,
            "expect_label": r.expect_label,
            "excluded": r.excluded_rows,
            "occasional": occasional,
            "company_wide": shared,
        })
    # 有覆盖率的排前面，低的更前面：那是唯一会真的漏钱的信号。
    return sorted(out, key=lambda d: (d["coverage"] is None, d["coverage"] or 0.0))


def _unclassified(sl: Slice) -> list[dict[str, Any]]:
    """没认出来的原始科目。字典该补哪一条，看这张表。"""
    items = [
        {"label": label, "count": count, "amount": amount}
        for label, (count, amount) in sl.classify_report.unmatched.items()
    ]
    # 按绝对金额排，先处理值钱的。笔数多但金额小的往往是运费尾差之类。
    return sorted(items, key=lambda d: -abs(d["amount"]))


# --------------------------------------------------------------------------- #
# 下钻
# --------------------------------------------------------------------------- #


def node_metrics(model: Model, node_id: str) -> list[str]:
    """一个报表节点由哪些指标构成。递归展开到叶子。

    报表节点的 children 里既可能是别的节点，也可能直接是指标 id。展开到指标才能
    去事实表里捞行——用户点「推广费」，要看到的是那 3,000 行推广扣费，
    不是「它等于三个子项之和」。
    """
    nodes = {n.id: n for n in model.statement}
    metrics = {m.id for m in model.metrics}
    seen: set[str] = set()
    out: list[str] = []

    def walk(nid: str) -> None:
        if nid in seen:
            return
        seen.add(nid)
        if nid in metrics:
            out.append(nid)
            return
        node = nodes.get(nid)
        if node is None:
            return
        for child in node.children:
            walk(child)
        if node.formula is not None:
            for ref in getattr(node.formula, "of", ()) or ():
                walk(ref)

    walk(node_id)
    return out


#: 一次下钻最多返回多少行明细。再多人也看不完，而且会把浏览器拖死。
DRILL_LIMIT = 200


def drill(facts: pl.DataFrame, model: Model, node_id: str, limit: int = DRILL_LIMIT) -> dict[str, Any]:
    """一个报表数字是怎么来的。

    分两层给：先按科目和来源文件汇总，让人一眼看出钱主要压在哪；再给若干行原始
    明细，每行带文件名、工作表、行号。只报总数不给行号的话，对不上账时没人查得动。

    吃的是事实表而不是 Slice，因为下钻多半发生在算完之后——人看完报表才想点开。
    那时候内存里的 Slice 早没了，只有留档的事实行。
    """
    metrics = node_metrics(model, node_id)
    node = next((n for n in model.statement if n.id == node_id), None)
    empty = {
        "node": node_id, "name": node.name if node else node_id,
        "metrics": [], "total": 0.0, "rows": 0, "by_subject": [], "by_file": [], "sample": [],
    }
    if not metrics or facts.is_empty():
        return empty

    facts = facts.filter(pl.col("metric_id").is_in(metrics))
    if facts.is_empty():
        return empty

    # 有科目列才按科目分。推广扣费那张表根本没有科目这一列，硬分出来是一行
    #「未分类 6,324 行」——看着像 6,324 行漏了归类，实际是这项本来就不分科目。
    named = facts.filter(
        pl.col("minor").is_not_null() | pl.col("subject").is_not_null()
    )
    by_subject = (
        named.group_by("minor", "subject")
        .agg(pl.len().alias("count"), pl.col("amount").sum().alias("amount"))
        .sort("amount")
        if not named.is_empty()
        else named
    )
    by_file = (
        facts.group_by("file_name", "sheet")
        .agg(pl.len().alias("count"), pl.col("amount").sum().alias("amount"))
        .sort("amount")
    )
    sample = (
        facts.select(
            "metric_id", "link_key", "linked", "amount", "subject", "minor",
            "file_name", "sheet", "row_no",
        )
        # 先看金额大的那几行，异常基本都在两端。
        .sort(pl.col("amount").abs(), descending=True)
        .head(limit)
    )
    return {
        "node": node_id,
        "name": node.name if node else node_id,
        "metrics": [{"id": m, "name": metric_name(model, m)} for m in metrics],
        "total": float(facts.get_column("amount").sum() or 0.0),
        "rows": int(facts.height),
        "by_subject": [
            {"subject": r["minor"] or r["subject"],
             "raw": r["subject"], "count": r["count"], "amount": r["amount"]}
            for r in by_subject.to_dicts()
        ],
        "by_file": [
            {"file": r["file_name"], "sheet": r["sheet"] or "",
             "count": r["count"], "amount": r["amount"]}
            for r in by_file.to_dicts()
        ],
        "sample": [
            {**r, "metric": metric_name(model, r.pop("metric_id"))}
            for r in sample.to_dicts()
        ],
        "truncated": int(facts.height) > limit,
    }


# --------------------------------------------------------------------------- #
# 接表向导
# --------------------------------------------------------------------------- #


def draft_dict(draft: Draft, table: RawTable, model: Model) -> dict[str, Any]:
    """一份映射草案的完整对外结构。向导第一屏渲染的就是这个。

    每一列都带着「为什么这么提」。这一点不能省：接表是一次性动作，人当时不核对，
    以后不会有人再回来看这张表；而错的映射不报错，只是静默少算钱。
    要人核对，就得把依据摆在他眼前，不能只给一个下拉框。
    """
    facts = role_facts(model, draft.source)
    return {
        "signature": draft.signature,
        "file": table.ref.filename,
        "sheet": table.ref.sheet or "",
        "sha": table.ref.sha256,
        "rows": len(table.rows),
        "kind": draft.kind,
        "base": draft.base,
        "source": draft.source,
        "source_name": source_name(model, draft.source) if draft.source else "",
        "summary": draft.summary(),
        "header_row": draft.parse.header_row,
        "time_slots": dict(draft.time_slots),
        "total_row_marker": draft.total_row_marker or "",
        "suggest_id": _suggest_template_id(draft, model),
        "match_columns": list(draft.template("tmp_id", source=draft.source or "x").match_columns)
        if draft.mapped else [],
        "columns": [
            {
                # 界面回传映射靠这个序号寻址，不靠列名：列名会重复。
                "index": g.index,
                "column": g.column,
                "role": g.role,
                "confidence": g.confidence,
                "settled": g.settled,
                "why": oneline(g.why),
                "shape": g.shape,
                "samples": list(g.samples),
                "occurrence": g.occurrence,
                "derived": g.derived,
                "no_name_match": g.no_name_match,
                "alternatives": [
                    {"role": r, "hint": facts[r].hint if r in facts else ""}
                    for r in g.alternatives
                ],
            }
            for g in draft.columns
        ],
        "vanished": list(draft.vanished),
        "warnings": [oneline(w) for w in draft.warnings],
    }


def _suggest_template_id(draft: Draft, model: Model) -> str:
    """提个模板 id。

    人取 id 时容易取成中文或带空格，而 id 会进快照和日志。给个能直接用的默认值，
    比事后校验拒绝他强。
    """
    if not draft.source:
        return ""
    n = 1 + sum(1 for t in model.templates if t.source == draft.source)
    return f"{draft.source}_v{n}"


def dryrun_dict(run: "DryRun") -> dict[str, Any]:
    """试跑结果的对外结构。人点「落库」之前看到的全部依据。"""
    return {
        "ok": run.ok,
        "summary": run.summary(),
        "rows": run.rows,
        "errors": [oneline(e) for e in run.errors],
        "warnings": [oneline(w) for w in run.warnings],
        "metrics": run.metrics,
        "roles": [
            {"role": r.role, "column": r.column, "filled": r.filled,
             "samples": list(r.samples), "total": r.total}
            for r in run.roles
        ],
        "controls": run.controls,
        "preview": run.preview,
        "match_columns": run.match_columns,
        "total_row_marker": run.total_row_marker,
    }


__all__ = [
    "DRILL_LIMIT",
    "draft_dict",
    "drill",
    "dryrun_dict",
    "platform_options",
    "reorder_statement",
    "statement_order",
    "metric_name",
    "node_metrics",
    "oneline",
    "slice_dict",
    "source_name",
    "store_dict",
]
