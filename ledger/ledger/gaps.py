"""缺口：这家店这个账期有什么不对。

一个账期的快照里已经有五处在讲「哪里不对」——完整度、覆盖率、未归类科目、
未归属金额、自检结论。分散在五个地方的后果是没有一个地方能回答那个真正的问题：

    这家店这个月，我该不该信这张损益表。

所以这里把五处收成一份清单，每条都是一句人话加一个能点进去的落点。判断标准只有
一个：看到这条之后，人知道下一步做什么。做不到的不写进来——清单一长，人就整份不看，
那比不报更糟。

两类分开叫，因为处置完全不同：

    空值项   这一项没有数。可能是表没交，可能是交了但一行都没挂上。
             对应动作是补数据。
    异常值项 有数，但数看着不对。覆盖率掉了、科目认不出、钱挂不上、上个月还有
             这个月突然成了 0。对应动作是查。

「上个月还有这个月突然成了 0」是这份清单里唯一一条需要跨账期才看得出来的，
也是最值钱的一条：模板绑错一列、平台改了个表头，这一项就会无声地变成 0.00，
而 0.00 在界面上和「这个月确实没花这笔钱」长得一模一样。
"""

from __future__ import annotations

from typing import Any

from .engine.audit import BUCKET_NEEDS_WORK
from .fees import pretty_unmatched_label
from .model.schema import Model

#: 覆盖率低于这个数就报。和自检里的门槛（0.98）刻意留了距离：
#: 自检那条是拦结账的红线，这里是提前一步的提示，两个数一样的话这条永远不会先响。
COVERAGE_FLOOR = 0.98

#: 金额小于这个数的异常不报。一分两分的尾差每个月都有，报出来只会淹掉真问题。
MIN_AMOUNT = 1.0

#: 「上个月还有这个月成了 0」要求上个月至少有这么多钱。几十块的科目本来就时有时无。
DROP_FLOOR = 100.0

SEVERITY_ORDER = {"blocking": 0, "warn": 1, "info": 2}


def _gap(kind: str, severity: str, title: str, detail: str, **rest: Any) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "title": title, "detail": detail,
            "node": rest.get("node", ""), "metric": rest.get("metric", ""),
            "source": rest.get("source", ""), "amount": rest.get("amount"),
            "only": rest.get("only", "counted")}


def gaps(payload: dict[str, Any], model: Model,
         before: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """一个账期的缺口清单。

    只读快照，不重算——已结账的账期按设计不能重算，而它同样需要能回答「哪里不对」。
    `before` 是同一家店上一个账期的快照，给「突然变成 0」那一条用；没有就跳过那条。
    """
    out: list[dict[str, Any]] = []
    out.extend(_blocking(payload, model))
    out.extend(_missing(payload, model))
    out.extend(_empty_lines(payload))
    out.extend(_zero_with_rows(payload, model))
    out.extend(_nothing_linked(payload, model))
    out.extend(_dropped_to_zero(payload, before))
    out.extend(_coverage(payload, model))
    out.extend(_unclassified(payload))
    out.extend(_unlinked(payload))
    out.sort(key=lambda g: (SEVERITY_ORDER.get(g["severity"], 9), -abs(g["amount"] or 0.0)))
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """给总览的格子用：几条、最重的是哪一级、空值和异常各几条。

    总览一格塞不下清单本身，但塞得下「这里有 3 条要看」。不给这个数的话，
    人得逐个店逐个月点进去才知道哪个月有问题——十几家店三个月就是几百次点击，
    等于这份清单不存在。
    """
    empty = sum(1 for g in rows if g["kind"] in ("missing", "empty"))
    return {
        "count": len(rows),
        "empty": empty,
        "odd": len(rows) - empty,
        "worst": min((g["severity"] for g in rows), key=lambda s: SEVERITY_ORDER.get(s, 9))
        if rows else "",
    }


# --------------------------------------------------------------------------- #
# 各类缺口
# --------------------------------------------------------------------------- #


def _blocking(payload: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """自检拦下来的。这一类必须排最前——它是「这个月结不了账」的原因。"""
    from .view import finding_action
    out = []
    for f in payload.get("findings") or []:
        if not (f.get("blocking") and not f.get("passed")):
            continue
        hit = finding_action(f, model, payload)
        node = "__sources__" if hit.get("tab") == "sources" else (hit.get("drill") or "")
        out.append(_gap(
            "blocking", "blocking", f["name"], f.get("message") or "",
            node=node, only=hit.get("only") or "counted",
        ))
    return out


def _missing(payload: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """该交的表没交。

    分两级：结账必需的表缺了是拦路的，其余是提示。这个区分在模型里已经声明过
    （`required_for_close`），这里只是把它念出来——都报成同一级的话，
    「推广表还没交」和「对账表还没交」看起来一样紧急，而后者是这个月的账根本算不出来。
    """
    required = {s.name for s in model.sources if s.required_for_close}
    out = []
    for name in payload.get("missing_sources") or []:
        hard = name in required
        out.append(_gap(
            "missing", "blocking" if hard else "info",
            f"{name} 没交",
            "结账要用这张表，缺了这个月的账算不全" if hard
            else "不影响结账，但这一项会一直是 0",
            source=name,
        ))
    return out


def _empty_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """损益表上出不来数的行。

    `available=False` 和「算出来是 0」是两回事，模型里一直分着，这里也必须分着报：
    前者是缺数据，后者是这个月真没这笔钱。合成一句「这一项是 0」的话，
    人没法知道该去补表还是该放心。
    """
    out = []
    for row in payload.get("statement") or []:
        if row.get("available") or row.get("is_total"):
            continue
        missing = row.get("missing_sources") or []
        out.append(_gap(
            "empty", "warn", f"{row.get('name')} 出不来数",
            f"缺{('、'.join(missing))}" if missing else "这一项没有可用的数据源",
            node=row.get("id") or "",
        ))
    return out


def _zero_with_rows(payload: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """这一项是 0.00，可它的表里有行。

    和上一条（缺数据源）不同：表交了、行也读进来了，但一分钱都没算进这一项。
    十次里九次是绑错了列或者字典少一条，剩下一次是这个月真的一笔都没有。
    分不出来的那一次也值得摆出来问一句——0.00 是最容易被当成「没问题」的数。

    这一条不依赖上个账期，所以第一次上数据的店也能看见。

    只看模型认为「每个月都该有」的口径，偶发科目一概跳过。质量报告里的行数是整张
    源表的行数，不是这一项自己那几行——交易赔付这个月一笔都没有的时候，它照样能
    看到对账表的一千七百行。拿这个数报警的话，每家店每个月都会多出五六条
    「这一项是 0.00」，而每一条都是废话。
    """
    from .view import node_metrics  # 只有这一处要用，顶层导入会绕成环

    rows = {q.get("metric"): q for q in payload.get("quality") or []}
    occasional = {m.id for m in model.metrics if m.occasional}
    out = []
    for row in payload.get("statement") or []:
        if row.get("is_total") or row.get("display") != "amount":
            continue
        if not row.get("available") or abs(row.get("value") or 0.0) >= 0.005:
            continue
        mids = [m for m in node_metrics(model, row.get("id") or "") if m not in occasional]
        have = [rows[m] for m in mids if m in rows and (rows[m].get("rows") or 0) > 0]
        if not have:
            continue
        total = sum(q.get("rows") or 0 for q in have)
        out.append(_gap(
            "zero", "warn", f"{row.get('name')} 是 0.00",
            f"它的表里读进来 {total:,} 行，但一分钱都没算进这一项——"
            f"多半是列绑错了或者字典少一条，也可能这个月真的没有",
            node=row.get("id") or "",
        ))
    return out


def _nothing_linked(payload: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """表有行，但一行都没挂到订单上。

    单独报是因为它和「覆盖率低」不是一个量级的事：覆盖率 90% 是漏了一角，
    命中率 0 是这张表整份白读，通常意味着订单号那一列取错了。
    """
    from .view import METRIC_PREFIX, metric_node
    out = []
    for q in payload.get("quality") or []:
        hit, rows = q.get("hit_rate"), q.get("rows") or 0
        if hit is None or rows == 0 or hit > 0:
            continue
        mid = q.get("metric") or ""
        out.append(_gap(
            "unmatched", "warn", f"{q.get('name')}一行都没挂上",
            f"读进来 {rows:,} 行，没有一行对上订单——多半是订单号那一列取错了",
            metric=mid,
            node=metric_node(model, mid) or (f"{METRIC_PREFIX}{mid}" if mid else ""),
            only="uncounted",
        ))
    return out


def _dropped_to_zero(payload: dict[str, Any],
                     before: dict[str, Any] | None) -> list[dict[str, Any]]:
    """上个月有钱、这个月成了 0 的行。

    这是整份清单里最值钱的一条。模板绑错一列、平台悄悄改了个表头，这一项就会变成
    0.00——而 0.00 在界面上和「这个月确实没花这笔钱」一模一样，不比对上个月看不出来。

    只报「有变成没有」，不报金额涨跌：后者是经营波动，天天都在发生，报出来就是噪声。

    偶发科目也报。交易赔付这类本来就时有时无，报出来有一定概率是白问一句，但这一条
    手里有证据——上个月这一项确实有 3,180.46。上面那条按行数判断的没有证据，
    所以它跳过偶发科目，这一条不跳。
    """
    if not before:
        return []
    was = {
        r.get("id"): r.get("value")
        for r in before.get("statement") or []
        if r.get("available") and not r.get("is_total") and r.get("display") == "amount"
    }
    out = []
    for row in payload.get("statement") or []:
        if row.get("is_total") or row.get("display") != "amount":
            continue
        now, then = row.get("value"), was.get(row.get("id"))
        if not row.get("available") or now is None or then is None:
            continue
        if abs(now) >= 0.005 or abs(then) < DROP_FLOOR:
            continue
        out.append(_gap(
            "dropped", "warn", f"{row.get('name')} 这个月是 0",
            f"上个账期是 {then:,.2f}。要么这个月真没有，要么这一项的数没接上——"
            f"0 和「没接上」在报表上长得一样，所以摆出来让人确认一次",
            node=row.get("id") or "", amount=then,
        ))
    return out


def _coverage(payload: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """有多少订单没拿到这一项成本。

    只报模型认为「每个订单都该有」的那几项（覆盖率不为空的那些）。偶发科目不报，
    理由和 view._quality 一样：交易赔付一个月十几笔，拿覆盖率衡量它永远是红的，
    而一列常年通红等于没有这一列。
    """
    from .view import metric_node
    out = []
    for q in payload.get("quality") or []:
        cov = q.get("coverage")
        if cov is None or cov >= COVERAGE_FLOOR:
            continue
        missed = (q.get("expected") or 0) - (q.get("covered") or 0)
        label = q.get("expect_label") or "全部"
        mid = q.get("metric") or ""
        out.append(_gap(
            "coverage", "warn", f"{q.get('name')}只盖到 {cov:.1%}",
            f"{label}的 {q.get('expected'):,} 笔订单里有 {missed:,} 笔没有这一项",
            metric=mid, node=metric_node(model, mid),
        ))
    return out


def _unclassified(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """字典里没有这个科目，所以这笔钱没进任何一项。"""
    out = []
    for item in payload.get("unclassified") or []:
        if abs(item.get("amount") or 0.0) < MIN_AMOUNT:
            continue
        caption = item.get("caption") or pretty_unmatched_label(item.get("label") or "")
        out.append(_gap(
            "unclassified", "warn", f"尚未归类：{caption}",
            f"{item.get('count'):,} 笔、合计 {item.get('amount'):,.2f}，"
            f"字典和规则都没有这一条，这笔钱没进损益表",
            amount=item.get("amount"),
            node="__unclassified__",
            only="all",
        ))
    return out


def _unlinked(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """挂不上任何订单、又没有别的解释的钱。

    只取「要查归属」那一桶。另外几桶（别家店的、规则排除的、别的账期的、
    提现/广告充值这类天然没订单号的）都已经有解释，摆进「要处理的事」里
    等于让人白查一场——点进去也对不上「要查」那一桶，界面是空的。
    """
    buckets = payload.get("unlinked_buckets") or []
    bucket = next((b for b in buckets if b.get("label") == BUCKET_NEEDS_WORK), None)
    # 金额和笔数都取「要查」那一桶。未归属总额里还含着提现、保证金、广告充值：
    # 抖音浅花涧 2026-05 的总额是 -357.06，全部是 664 笔货款直投千川，
    # 「要查」那一桶是空的。卡片上写「有 357.06 挂不上订单」再点进去一行都没有，
    # 就是退回总额惹的。
    #
    # 老快照没有分桶，只能看总额。有分桶却没有「要查」那一桶，说明总额里剩的
    # 都是已有解释的钱，人去「没进利润的钱」那一栏点对应行就能看到。
    if bucket is not None:
        total, count = bucket.get("amount"), bucket.get("count")
    elif not buckets:
        total, count = payload.get("unlinked_total"), None
    else:
        return []
    if total is None or abs(total) < MIN_AMOUNT:
        return []
    # 标题里写绝对值。这个总额是净额，可能是负的，而「有 -357.06 挂不上订单」
    # 得在脑子里绕一圈才知道是三百多块钱没落地。方向留给金额那一栏去表达。
    return [_gap(
        "unlinked", "warn", f"有 {abs(total):,.2f} 挂不上订单",
        (f"{count:,} 行取不出订单号。" if count else "")
        + "这部分没进店铺利润，也不该硬摊进去，要人查清归属",
        amount=total,
        node="__unlinked__",
        only="all",
    )]
