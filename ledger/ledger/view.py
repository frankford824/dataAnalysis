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
    from .commission import Commission
    from .onboard import Assisted, DryRun


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
        "archived": s.archived, "aliases": list(s.aliases),
        "commission_base": s.commission_base, "commission_on_loss": s.commission_on_loss,
        "note": s.note,
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


def commission_dict(c: Commission) -> dict[str, Any]:
    """提成结果的对外结构。

    「按人」在前、「按商品」在后，是有意的：拿去发钱的是按人那一栏，它的合计
    等于各人金额相加，一分不差。按商品那一栏是用来查问题的（谁没配、哪些走了兜底），
    行是四舍五入过的，几百行加起来会和总额差几毛——所以界面上不给它出合计行。
    """
    return {
        "base_node": c.base_node,
        "base_name": c.base_name,
        "base_total": c.base_total,
        "total": c.total,
        "configured": c.configured,
        "unassigned_base": c.unassigned_base,
        "fallback_base": c.fallback_base,
        "negative_orders": c.negative_orders,
        "negative_base": c.negative_base,
        "on_loss": c.on_loss,
        "skipped_loss_base": c.skipped_loss_base,
        "notes": list(c.notes),
        "people": [
            {"person": p.person, "amount": p.amount, "base": p.base, "products": p.products}
            for p in c.people
        ],
        "products": [
            {
                "product_id": p.product_id, "product_name": p.product_name,
                "base": p.base, "total_rate": p.total_rate, "amount": p.amount,
                "sub_orders": p.sub_orders, "fallback": p.fallback,
                "unassigned": p.unassigned, "effective_from": p.effective_from,
                "people": [{"person": n, "amount": a} for n, a in p.people],
            }
            for p in c.products
        ],
    }


def commission_rules(model: Model, store_id: str = "") -> list[dict[str, Any]]:
    """当前生效的提成配置，给界面展示和导出用。"""
    rules = model.commission_for(store_id) if store_id else model.commission
    return [
        {
            "effective_from": r.effective_from, "store": r.store,
            "product_id": r.product_id, "product_name": r.product_name,
            "person": r.person, "share": r.share, "total_rate": r.total_rate,
            "note": r.note,
        }
        for r in sorted(rules, key=lambda r: (r.store, r.product_id, r.effective_from, r.person))
    ]


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


#: 一页下钻明细的行数。再多人也看不完，而且会把浏览器拖死。
DRILL_LIMIT = 200


def _claimed_by(model: Model, metrics: list[str]) -> pl.Expr:
    """挑出真正算进这些指标的事实行。

    指标声明了 `major` 就只认归到这个大类的行——这和投影时的过滤是同一条规则
    （`engine/project.py` 里那句 `filter(major == metric.major)`）。两处必须一致，
    不一致的表现是下钻和报表各说各话，而且两个数看着都像对的。

    没声明 `major` 的指标（推广扣费、运费这类源头就不分科目的表）不加这一层：
    它们的每一行都算数，硬要求 major 相等会把整张表筛空。
    """
    by_id = {m.id: m for m in model.metrics}
    parts: list[pl.Expr] = []
    for mid in metrics:
        metric = by_id.get(mid)
        hit = pl.col("metric_id") == mid
        major = getattr(metric, "major", None) if metric else None
        if major:
            hit = hit & (pl.col("major") == major)
        parts.append(hit)
    if not parts:
        return pl.lit(False)
    out = parts[0]
    for p in parts[1:]:
        out = out | p
    return out


def _selected(
    facts: pl.DataFrame, *, subject: str | None, file: str | None, q: str | None
) -> pl.DataFrame:
    """按界面上点的那几个条件收窄明细。

    科目和文件是从汇总区点进来的，所以按原样精确比对——汇总区显示的 `subject`
    是归一化后的 `minor`（没有才退回原始科目名），这里的比对必须用同一个口径，
    不然点了没反应。

    关键词是人自己敲的，一律当字面量：科目名里带括号和加号的多得是
    （「保证金-天猫-扣除转移」「交易收款-交易收款」），当成正则不是报错就是撞出
    一堆无关的行。
    """
    if subject:
        shown = pl.coalesce(pl.col("minor"), pl.col("subject"))
        facts = facts.filter(shown == subject)
    if file:
        facts = facts.filter(pl.col("file_name") == file)
    if q and q.strip():
        text = q.strip()
        hit = pl.lit(False)
        for col in ("link_key", "subject", "minor", "file_name", "sheet"):
            hit = hit | pl.col(col).cast(pl.Utf8).str.contains(text, literal=True)
        facts = facts.filter(hit.fill_null(False))
    return facts


def drill(facts: pl.DataFrame, model: Model, node_id: str, limit: int = DRILL_LIMIT,
          value: float | None = None, *, offset: int = 0,
          subject: str | None = None, file: str | None = None,
          q: str | None = None, order: str = "amount",
          only: str = "counted") -> dict[str, Any]:
    """一个报表数字是怎么来的。

    分两层给：先按科目和来源文件汇总，让人一眼看出钱主要压在哪；再给若干行原始
    明细，每行带文件名、工作表、行号。只报总数不给行号的话，对不上账时没人查得动。

    吃的是事实表而不是 Slice，因为下钻多半发生在算完之后——人看完报表才想点开。
    那时候内存里的 Slice 早没了，只有留档的事实行。

    只认这个指标真正认领的行
    ------------------------
    事实表里存的是「每个指标看过的每一行」，而不是「每个指标算进去的行」——同一张
    对账表的一行会在五个指标名下各出现一次，最后由归类结果（`major`）决定它属于谁。
    这是引擎的设计：投影时才做这一层过滤。

    所以这里必须自己补上同一个过滤，否则五个指标下钻出来是同一个数。实测淘宝那家店
    「平台服务费」下钻出 -3,258.99，报表上写着 -42,236.94，而且「平台营销费用」
    下钻出的也是 -3,258.99。

    默认只看进了账的行
    ------------------
    源表里的行不是都进损益表的。运费表是全公司的运单，淘宝那家店 29.9 万行里只有
    1.4 万行挂得上自己的订单；其余 28.5 万行、五十多万块钱属于别的店铺。全摆出来的话，
    点开「发货运费」看到的是 -550,944，而报表上写着 -20,294——人只会认为报表算错了。

    所以默认给进了账的那部分，按 `contribution`（这一行实际算进去多少，已折算分摊
    比例）加总，逐行加起来就是报表数字。没进账的行不删，收在 `uncounted` 里说明
    有多少行、多少钱、为什么没进——「这笔钱去哪了」每个月都会被问到。

    `only="uncounted"` 就是去看那部分；`only="all"` 是两边一起看，此时合计
    对不上报表，属于正常。

    筛选和翻页只动明细
    ------------------
    `subject` / `file` / `q` 收窄的是 `sample` 那部分，`selection` 说明这一页是从
    多少行里取的、这些行合计多少。汇总区（`by_subject`、`by_file`）和顶上那两个数
    （`source_total`、`value`）始终是整个节点的全貌，不随筛选变。

    汇总区是导航入口：点科目就把它筛掉的话，剩一行、也回不去了。顶上两个数不变则是
    因为人下钻的目的就是拿它跟报表核对——核对基准在翻页过程中变来变去，这事就没法做了。
    """
    metrics = node_metrics(model, node_id)
    node = next((n for n in model.statement if n.id == node_id), None)
    only = only if only in ("counted", "uncounted", "all") else "counted"
    empty = {
        "node": node_id, "name": node.name if node else node_id,
        "metrics": [], "total": 0.0, "source_total": 0.0, "value": value,
        "rows": 0, "by_subject": [], "by_file": [], "sample": [],
        "selection": _selection(0, 0.0, offset, limit, subject, file, q),
        "only": only, "graded": True, "uncounted": _uncounted(0, 0.0),
        "truncated": False,
    }
    if not metrics or facts.is_empty():
        return empty

    facts = facts.filter(_claimed_by(model, metrics))
    if facts.is_empty():
        return empty

    # 老的留档没有进账标记（`counted` 是后加的）。这种情况下退回「全都算进账」，
    # 数字会对不上报表，但至少不会一行都不显示。界面照着 graded 提示重算一次。
    graded = "counted" in facts.columns
    if not graded:
        only = "all"
        facts = facts.with_columns(
            pl.lit(True).alias("counted"), pl.col("amount").alias("contribution")
        )

    out_rows = int(facts.filter(~pl.col("counted")).height)
    out_amount = float(facts.filter(~pl.col("counted")).get_column("amount").sum() or 0.0)

    scope = {
        "counted": facts.filter(pl.col("counted")),
        "uncounted": facts.filter(~pl.col("counted")),
    }.get(only, facts)
    if scope.is_empty():
        return {**empty, "graded": graded,
                "uncounted": _uncounted(out_rows, out_amount)}

    # 进了账的那部分要按实际算进去的金额报，否则跟报表差一个分摊比例。
    money = pl.col("contribution") if only == "counted" else pl.col("amount")

    # 有科目列才按科目分。推广扣费那张表根本没有科目这一列，硬分出来是一行
    #「未分类 6,324 行」——看着像 6,324 行漏了归类，实际是这项本来就不分科目。
    named = scope.filter(
        pl.col("minor").is_not_null() | pl.col("subject").is_not_null()
    )
    by_subject = (
        named.group_by("minor", "subject")
        .agg(pl.len().alias("count"), money.sum().alias("amount"))
        .sort("amount")
        if not named.is_empty()
        else named
    )
    by_file = (
        scope.group_by("file_name", "sheet")
        .agg(pl.len().alias("count"), money.sum().alias("amount"))
        .sort("amount")
    )
    picked = _selected(scope, subject=subject, file=file, q=q)
    by, descending = _ORDERS.get(order, _ORDERS["amount"])
    sample = (
        picked.select(
            "metric_id", "link_key", "linked", "counted", "contribution",
            "amount", "subject", "minor", "file_name", "sheet", "row_no",
        )
        .sort(by, descending=descending)
        .slice(max(offset, 0), limit)
    )
    source_total = float(scope.select(money.sum()).item() or 0.0)
    picked_total = float(picked.select(money.sum()).item() or 0.0)
    return {
        "node": node_id,
        "name": node.name if node else node_id,
        "metrics": [{"id": m, "name": metric_name(model, m)} for m in metrics],
        #: 当前这一档行的合计。默认这一档是「进了账的」，逐行加起来就是报表数字。
        "source_total": source_total,
        #: 报表上那个数。调用方给得出就给，给不出是 None——界面上宁可不显示，
        #: 也不要摆一个自己算的近似值冒充报表数字。
        "value": value,
        # 老字段，留着不动界面。含义就是 source_total。
        "total": source_total,
        "rows": int(scope.height),
        "only": only,
        #: 这次留档有没有记进账标记。没有就说明是旧快照，数字对不上报表。
        "graded": graded,
        #: 没进账的那部分。运费表是全公司的运单，这里会是绝大多数行——
        #: 它们不进这家店的账，但删掉就没法回答「这笔钱去哪了」。
        "uncounted": _uncounted(out_rows, out_amount),
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
        #: 这一页是从哪些行里取的。筛选条件原样带回去，界面照着它渲染筛选状态，
        #: 不用自己记——记岔了会出现「显示按科目筛着、其实没筛」这种最难查的错。
        "selection": _selection(int(picked.height), picked_total, offset, limit,
                                subject, file, q),
        # 老字段，留着不动界面。现在的含义是「还有下一页」。
        "truncated": max(offset, 0) + limit < int(picked.height),
    }


def _uncounted(rows: int, amount: float) -> dict[str, Any]:
    return {"rows": rows, "amount": amount}


#: 明细的排序。金额序看异常（大额都在两端），行号序对着源文件逐行核。
#: 两种都补上文件、工作表、行号做次序兜底：并列的行在两页之间跳来跳去的话，
#: 翻页会漏行，而且漏得不留痕迹。
_ORDERS: dict[str, tuple[list[pl.Expr], list[bool]]] = {
    "amount": ([pl.col("amount").abs(), pl.col("file_name"), pl.col("sheet"),
                pl.col("row_no")], [True, False, False, False]),
    "row": ([pl.col("file_name"), pl.col("sheet"), pl.col("row_no")],
            [False, False, False]),
}


def _selection(rows: int, amount: float, offset: int, limit: int,
               subject: str | None, file: str | None, q: str | None) -> dict[str, Any]:
    offset = max(offset, 0)
    return {
        "rows": rows,
        "amount": amount,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < rows,
        "filtered": bool(subject or file or (q or "").strip()),
        "subject": subject or "",
        "file": file or "",
        "q": (q or "").strip(),
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
                "model_role": g.model_role,
                "model_why": oneline(g.model_why),
                "model_filled": g.model_filled,
                "alternatives": [
                    {"role": r, "hint": facts[r].hint if r in facts else ""}
                    for r in g.alternatives
                ],
            }
            for g in draft.columns
        ],
        "vanished": list(draft.vanished),
        "warnings": [oneline(w) for w in (*draft.notices, *draft.warnings)],
    }


def assist_dict(assisted: "Assisted") -> dict[str, Any]:
    """模型这一轮做了什么。

    采纳、分歧、挡掉的都摆出来，一条不省。人要判断的不是「模型准不准」这种笼统的事，
    是「这一次它动的这几列对不对」——只给个「模型提了 5 列」，人无从判断起。
    """
    return {
        "ok": assisted.ok,
        "model": assisted.model,
        "elapsed_ms": assisted.elapsed_ms,
        "summary": assisted.summary(),
        "adopted": list(assisted.adopted),
        "disputed": list(assisted.disputed),
        "agreed": list(assisted.agreed),
        "refused": list(assisted.refused),
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
