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

from .engine.audit import BUCKET_EXPLAINED, BUCKET_NEEDS_WORK, BUCKET_WHY, tag_unlinked
from .engine.calculate import NodeValue
from .engine.project import claims
from .engine.runtime import Slice
from .engine.types import RawTable
from .fees import humanize_via, pretty_unmatched_label
from .model.loader import ModelError
from .model.propose import Draft, role_facts
from .model.schema import Model, Store
from .money import money_float

if TYPE_CHECKING:  # 只为类型标注；运行时导入会让 view 依赖向导层，方向反了
    from .commission import Commission
    from .onboard import Assisted, DryRun


def oneline(text: str) -> str:
    """压成一行。

    模型里的提示语用 YAML 折叠写法，换行会变成空格。中文标点后面本来不该有空格，
    直接压会留下「还没同步， 或者」这种夹缝。
    """
    return re.sub(r"(?<=[，。；：、！？）】」“”])\s+", "", " ".join(text.split()))


def _finding_lines(text: str) -> list[str]:
    """把检查结论里逐条列举的那几行拆出来。

    检查的结论是「一句话结论 + 几条明细」，明细在引擎里是以「  · 」开头的独立行。
    `oneline` 会把它们压成一长串，界面上就成了一堵墙——缺三份数据和缺一份数据看起来
    一样长。这里在压之前先拆好，界面照着一行一行摆；界面自己去认「 · 」这个记号是
    行不通的，桶名那一列整列空白就是两边各认一套记号认出来的。
    """
    return [
        oneline(line.lstrip(" ·"))
        for line in text.splitlines()[1:]
        if line.strip()
    ]


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
             "blocking": f.blocking, "message": oneline(f.message),
             "head": oneline(f.message.splitlines()[0] if f.message else ""),
             "lines": _finding_lines(f.message)}
            for f in sl.audit.findings
        ],
        "sources": _sources(sl, model),
        "missing_sources": [source_name(model, s) for s in sl.completeness.missing],
        "quality": _quality(sl, model),
        "unclassified": _unclassified(sl),
        "unlinked_total": sl.audit.unlinked_total,
        # 每一桶都带上「为什么挂不上」和「算不算进合计」。
        #
        # 界面上这一列曾经整列是空的：前端读 name，这里给的是 label，谁也没报错。
        # 光把列名改对还会剩下第二个坑——四行明细和顶上的合计差几个数量级，
        # 因为合计只算要查的那一桶。所以解释和「算不算进合计」都由这里给，
        # 界面照着摆就行，不必自己维护一份桶名清单。
        "unlinked_buckets": [
            {"label": b[0], "count": b[1], "amount": b[2],
             "why": BUCKET_WHY.get(b[0], ""), "counted": b[0] not in BUCKET_EXPLAINED}
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
        {
            "label": label,
            "caption": pretty_unmatched_label(label),
            "count": count,
            "amount": amount,
        }
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


def metric_node(model: Model, metric_id: str) -> str:
    """一个指标落在损益表的哪一行。取最深的那一行。

    点「发货运费只盖到 94%」应该落到发货运费，不是履约成本合计——合计点开是一笔
    糊涂账，人要看的是这一项自己。
    """
    best, level = "", -1
    for node in model.statement:
        if metric_id in node_metrics(model, node.id) and node.level >= level:
            best, level = node.id, node.level
    return best


#: 不是损益表行的下钻。挂不上的钱、未归类科目、某个指标自己，走这些入口，
#: 因为它们在报表树上没有对应的节点——硬塞进某一行会让那一行的合计对不上。
UNLINKED_NODE = "__unlinked__"
UNCLASSIFIED_NODE = "__unclassified__"
METRIC_PREFIX = "__metric__:"


def finding_action(finding: dict[str, Any], model: Model,
                   payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """给自检结论补上人能点的落点。

    快照里的结论只有一段话。话是给「为什么」用的，查账要的是这 66 行在哪个文件
    第几行。老快照没有 kind / detail 也要能点——用当前模型的检查定义对上号。
    """
    check = next((c for c in model.checks if c.id == finding.get("id")), None)
    kind = finding.get("kind") or (check.kind if check else "")
    drill, only, tab = "", "counted", ""
    buckets: list[dict[str, Any]] = []
    if kind == "unlinked_disclosed":
        drill, only = UNLINKED_NODE, "all"
        for b in (payload or {}).get("unlinked_buckets") or []:
            label = b.get("label") or ""
            buckets.append({
                "label": label, "count": b.get("count"), "amount": b.get("amount"),
                "why": b.get("why") or BUCKET_WHY.get(label, ""),
                "drill": UNLINKED_NODE if label == BUCKET_NEEDS_WORK
                else f"{UNLINKED_NODE}:{label}",
                "only": "all",
            })
    elif kind == "no_unclassified":
        drill, only = UNCLASSIFIED_NODE, "all"
    elif kind == "completeness":
        tab = "sources"
    elif kind in ("link_rate", "spine_coverage"):
        mid = (finding.get("detail") or {}).get("metric") or (check.metric if check else "")
        if mid and kind == "spine_coverage":
            drill = metric_node(model, mid) or f"{METRIC_PREFIX}{mid}"
            only = "counted"
        elif mid:
            drill = f"{METRIC_PREFIX}{mid}"
            only = "uncounted"
    out = {**finding, "kind": kind, "drill": drill, "only": only, "tab": tab}
    if buckets:
        out["buckets"] = buckets
    return out


#: 一页下钻明细的行数。再多人也看不完，而且会把浏览器拖死。
DRILL_LIMIT = 200


def _claimed_by(model: Model, metrics: list[str]) -> pl.Expr:
    """挑出真正算进这些指标的事实行。

    认领条件由引擎定义（`engine.project.claims`），这里只是把这个节点底下几个指标的
    条件或起来。不自己写一份是因为写两份必然分叉，而分叉的表现是下钻和报表各说各话，
    两个数看着都像对的。
    """
    by_id = {m.id: m for m in model.metrics}
    parts = [claims(by_id[mid]) for mid in metrics if mid in by_id]
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
    only = only if only in ("counted", "uncounted", "all") else "counted"
    special = _special_drill(facts, model, node_id)
    if special is not None:
        facts, name, bucket_value, kind = special
        if value is None:
            value = bucket_value
        # 这些行本来就不在损益表上，默认「进了账」会筛成空白，人以为点了没反应。
        if only == "counted":
            only = "all"
        metrics = []
        skip_claim = True
    else:
        metrics = node_metrics(model, node_id)
        node = next((n for n in model.statement if n.id == node_id), None)
        name = node.name if node else node_id
        kind = "statement"
        skip_claim = False
    key_label = _link_key_label(model, metrics)
    empty = {
        "node": node_id, "name": name,
        "metrics": [], "total": 0.0, "source_total": 0.0, "value": value,
        "rows": 0, "by_subject": [], "by_file": [], "sample": [],
        "selection": _selection(0, 0.0, offset, limit, subject, file, q),
        "only": only, "graded": True, "uncounted": _uncounted(0, 0.0),
        "truncated": False, "kind": kind, "key_label": key_label,
    }
    if facts.is_empty() or (not skip_claim and not metrics):
        return empty

    if not skip_claim:
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
    # 按界面上那一行的名字分，不按 (细项, 原始科目) 两列分。
    #
    # 界面显示的是 coalesce(minor, subject)。按两列分的话，同一件费会出现两行
    # 同名：「类目软件服务费」33,017 行是字典收过的（原始科目带着 0030003|），
    # 另外 323 行原始科目本身就叫这个名字，细项也是这个名字——人看见两个
    # 「类目软件服务费」，按费项对账对不上。点进去筛的已经是显示名，汇总却按
    # 原始科目切开，两边口径不一致。
    by_subject = (
        named.with_columns(
            pl.coalesce(pl.col("minor"), pl.col("subject")).alias("shown")
        )
        .group_by("shown")
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
            *[c for c in (
                "metric_id", "link_key", "linked", "counted", "contribution",
                "amount", "subject", "minor", "classify_via",
                "file_name", "sheet", "row_no",
            ) if c in picked.columns]
        )
        .sort(by, descending=descending)
        .slice(max(offset, 0), limit)
    )
    # 收到分。留档里的金额是 f64（parquet 存不了 Decimal），四万行加起来会攒出
    # 半分钱的浮点误差，于是报表写着 -172,082.78、下钻写着 -172,082.79，界面上还
    # 老老实实标一句「差 -0.01」。那一分钱不存在，是加法本身的误差；把它显示出来
    # 只会让人去查一笔根本没有的账。
    source_total = money_float(scope.select(money.sum()).item() or 0.0)
    picked_total = money_float(picked.select(money.sum()).item() or 0.0)
    return {
        "node": node_id,
        "name": name,
        "kind": kind,
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
            {"subject": r["shown"], "raw": r["shown"],
             "count": r["count"], "amount": r["amount"]}
            for r in by_subject.to_dicts()
        ],
        "by_file": [
            {"file": r["file_name"], "sheet": r["sheet"] or "",
             "count": r["count"], "amount": r["amount"]}
            for r in by_file.to_dicts()
        ],
        "sample": [
            {
                **r,
                "metric": metric_name(model, r.pop("metric_id")),
                "classify_via": humanize_via(r.get("classify_via") or "", model),
            }
            for r in sample.to_dicts()
        ],
        #: 这一页是从哪些行里取的。筛选条件原样带回去，界面照着它渲染筛选状态，
        #: 不用自己记——记岔了会出现「显示按科目筛着、其实没筛」这种最难查的错。
        "selection": _selection(int(picked.height), picked_total, offset, limit,
                                subject, file, q),
        "key_label": key_label,
        # 老字段，留着不动界面。现在的含义是「还有下一页」。
        "truncated": max(offset, 0) + limit < int(picked.height),
    }


def _link_key_label(model: Model, metrics: list[str]) -> str:
    """下钻表头：推广表挂的是商品 ID，不能写成订单号。

    拼多多商品分天推广那列绑定的是 product_id，下钻却整列写着「订单号」。
    人对着订单库去查这些号，永远查不到，还会以为匹配规则写错了。
    """
    grains = []
    for mid in metrics:
        try:
            metric = model.metric(mid)
        except KeyError:
            continue
        if metric.link:
            grains.append(metric.link.grain)
    if grains and all(g == "product" for g in grains):
        return "商品ID"
    return "订单号"


def _special_drill(facts: pl.DataFrame, model: Model, node_id: str):
    """不是损益表行的下钻。对不上就返回 None，走原来的节点路径。"""
    if node_id == UNLINKED_NODE or node_id.startswith(UNLINKED_NODE + ":"):
        bucket = node_id.split(":", 1)[1] if ":" in node_id else BUCKET_NEEDS_WORK
        tagged = tag_unlinked(facts, model)
        if tagged.is_empty() or "bucket" not in tagged.columns:
            scoped = tagged
        else:
            scoped = tagged.filter(pl.col("bucket") == bucket).drop("bucket")
        total = 0.0 if scoped.is_empty() else float(scoped.select(pl.col("amount").sum()).item() or 0)
        return scoped, bucket, money_float(total), "unlinked"
    if node_id == UNCLASSIFIED_NODE:
        known = {e.raw for e in model.dictionary}
        if facts.is_empty() or "subject" not in facts.columns:
            scoped = facts
        elif not known:
            scoped = facts.filter(pl.col("subject").fill_null("") != "")
        else:
            scoped = facts.filter(
                (pl.col("subject").fill_null("") != "")
                & ~pl.col("subject").is_in(list(known))
            )
        keys = [c for c in ("file_sha", "sheet", "row_no") if c in scoped.columns]
        if keys and not scoped.is_empty():
            scoped = scoped.unique(subset=keys, keep="first", maintain_order=True)
        total = 0.0 if scoped.is_empty() else float(scoped.select(pl.col("amount").sum()).item() or 0)
        return scoped, "尚未归类的费项", money_float(total), "unclassified"
    if node_id.startswith(METRIC_PREFIX):
        mid = node_id[len(METRIC_PREFIX):]
        scoped = facts.filter(pl.col("metric_id") == mid) if not facts.is_empty() else facts
        if not scoped.is_empty():
            by_id = {m.id: m for m in model.metrics}
            if mid in by_id:
                scoped = scoped.filter(claims(by_id[mid]))
        total = 0.0 if scoped.is_empty() else float(scoped.select(pl.col("amount").sum()).item() or 0)
        return scoped, metric_name(model, mid), money_float(total), "metric"
    return None


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
        "match_columns": _draft_match_columns(draft),
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


def _draft_match_columns(draft: Draft) -> list[str]:
    """向导预览用的识别签名。草案还没人改完时，两列可能暂时映到同一角色，
    `template()` 会拒绝——向导必须先能打开，落库时仍会拦住。
    """
    if not draft.mapped:
        return []
    try:
        return list(draft.template("tmp_id", source=draft.source or "x").match_columns)
    except ModelError:
        return list(draft._match_columns({c.index: c.role for c in draft.columns}))


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
