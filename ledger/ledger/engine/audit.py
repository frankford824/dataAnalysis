"""原语七：自检。在结账前拦截。

这道关卡是产品最重要的设计。它把"我不知道这数对不对"变成"系统不让我在数不对的
时候结账"。

告警最大的失败模式是推送太多，用户学会忽略全部告警。所以这里的输出必须是人话，
并且必须把"用户不需要管的"和"需要管的"分开——天然无订单号的科目（提现、广告充值、
保证金、往来款）挂不上订单是正常的，不该占用用户注意力。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..model.schema import Check, Model
from ..money import decimal_amount, money_float, sum_amounts
from .calculate import NodeValue, _apply
from .link import EXCLUDED_KEY
from .types import ClassifyReport, Completeness, Finding, LinkReport


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    #: 没进利润的钱。绝不静默丢弃。
    unlinked_buckets: list[tuple[str, int, float]] = field(default_factory=list)
    unlinked_total: float = 0.0

    @property
    def can_close(self) -> bool:
        return not any(f.blocking and not f.passed for f in self.findings)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking and not f.passed]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if not f.blocking and not f.passed]


def audit(
    model: Model,
    facts: pl.DataFrame,
    link_reports: dict[str, LinkReport],
    classify_report: ClassifyReport,
    completeness: Completeness,
    nodes: dict[str, NodeValue],
) -> AuditResult:
    result = AuditResult()
    result.unlinked_buckets, result.unlinked_total = _bucket_unlinked(facts, model)

    for check in model.checks:
        handler = _HANDLERS.get(check.kind)
        if handler is None:  # pragma: no cover
            continue
        result.findings.append(
            handler(check, model, facts, link_reports, classify_report, completeness, nodes, result)
        )
    return result


# --------------------------------------------------------------------------- #
# 各类校验
# --------------------------------------------------------------------------- #


def _check_link_rate(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    report = links.get(check.metric or "")
    if report is None:
        return Finding(
            check.id, check.name, passed=True, blocking=False,
            message=f"{check.name}：这个月没有相关数据，跳过",
        )
    threshold = check.threshold if check.threshold is not None else report_threshold(model, check)
    passed = report.hit_rate >= threshold
    metric_name = _metric_name(model, check.metric)
    if passed:
        message = f"{metric_name} 挂订单 {report.hit_rate:.1%}，正常"
    else:
        gap = report.eligible - report.linked_rows
        message = (
            f"{metric_name} 有 {gap:,} 行没对上订单（挂上 {report.hit_rate:.1%}，"
            f"要求 {threshold:.0%}）。这部分金额不会计入利润，建议先看一下。"
        )
    return Finding(
        check.id, check.name, passed=passed, blocking=check.blocking and not passed,
        message=check.message or message,
        detail={
            "metric": check.metric,
            "hit_rate": report.hit_rate,
            "threshold": threshold,
            "unlinked_rows": report.eligible - report.linked_rows,
            "naturally_unlinked_rows": report.naturally_unlinked_rows,
            "extract_failed_rows": report.extract_failed_rows,
        },
    )


def _check_coverage(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    """覆盖率：订单里有多少笔拿到了这项数据。

    这是命中率查不出来的失败模式。只传了半个月成本的文件，命中率是漂亮的 100%，
    但利润虚高一倍。
    """
    report = links.get(check.metric or "")
    metric_name = _metric_name(model, check.metric)
    if report is None or not report.spine_keys:
        return Finding(
            check.id, check.name, passed=True, blocking=False,
            message=f"{check.name}：没有可比对的订单，跳过",
        )
    threshold = check.threshold if check.threshold is not None else 0.95
    passed = report.coverage >= threshold
    gap = report.spine_keys - report.spine_keys_covered
    # 分母被 expect 收窄时要说清算的是哪一批订单，不然「1,060 笔」对不上订单明细的行数。
    scope = (
        f"{report.expect_label}的 {report.spine_keys:,} 笔"
        if report.expect_label
        else f"{report.spine_keys:,} 笔"
    )
    if passed:
        message = f"{metric_name} 覆盖了{scope}订单里的 {report.coverage:.1%}，正常"
    else:
        # 数字由引擎给，排查建议由模型给。两者都要，不能二选一：
        # 光有建议看不出差多少，光有数字不知道该去查什么。
        message = (
            f"有 {gap:,} 笔订单没有{metric_name}（{scope}订单里覆盖 {report.coverage:.1%}，"
            f"要求 {threshold:.0%}）。" + (check.message or "这些订单的利润会偏高。")
        )
    detail = {
        "metric": check.metric,
        "coverage": report.coverage,
        "threshold": threshold,
        "spine_keys": report.spine_keys,
        "uncovered": gap,
    }
    if report.expect_label:
        detail["scope"] = report.expect_label
        detail["spine_keys_total"] = report.spine_keys_total
    return Finding(
        check.id, check.name, passed=passed, blocking=check.blocking and not passed,
        message=message,
        detail=detail,
    )


def _check_unclassified(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    limit = int(check.threshold or 0)
    unmatched = classify.unmatched
    count = sum(c for c, _ in unmatched.values())
    passed = count <= limit
    if passed:
        return Finding(check.id, check.name, passed=True, blocking=False, message="所有科目都认识")
    top = sorted(unmatched.items(), key=lambda kv: -abs(kv[1][1]))[:5]
    lines = [f"  · {name}：{c} 笔，{amount:,.2f} 元" for name, (c, amount) in top]
    total = float(sum_amounts(abs(decimal_amount(a)) for _, a in unmatched.values()))
    message = (
        f"有 {len(unmatched)} 个科目字典里没有，共 {count:,} 笔、{total:,.2f} 元：\n"
        + "\n".join(lines)
        + ("\n  · …" if len(unmatched) > 5 else "")
        + "\n确认这些科目归到哪一类，之后就不用再管了。"
    )
    return Finding(
        check.id, check.name, passed=False, blocking=check.blocking,
        message=check.message or message,
        detail={"subjects": [
            {"raw": name, "rows": c, "amount": a} for name, (c, a) in
            sorted(unmatched.items(), key=lambda kv: -abs(kv[1][1]))
        ]},
    )


def _check_completeness(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    if completeness.ok:
        return Finding(
            check.id, check.name, passed=True, blocking=False,
            message=f"该到的数据都到了（{completeness.label()}）",
        )
    owed: list[str] = []
    for sid in completeness.missing:
        source = model.source(sid)
        owed.append(f"  · {source.name} —— {_ROLE_LABEL.get(source.owner_role, source.owner_role)}")
    message = (
        f"还差 {len(completeness.missing)} 份数据（已到 {completeness.label()}）：\n"
        + "\n".join(owed)
        + "\n数据到齐前不出利润数字。"
    )
    return Finding(
        check.id, check.name, passed=False, blocking=check.blocking,
        message=check.message or message,
        detail={"missing": [
            {"source": sid, "name": model.source(sid).name,
             "owner_role": model.source(sid).owner_role,
             "owner_label": _ROLE_LABEL.get(model.source(sid).owner_role, "")}
            for sid in completeness.missing
        ]},
    )


def _check_tie_out(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    if not check.left or not check.right:
        return Finding(check.id, check.name, True, False, "勾稽等式没有定义两边，跳过")
    left = _eval_side(check.left, nodes)
    right = _eval_side(check.right, nodes)
    if left is None or right is None:
        return Finding(
            check.id, check.name, passed=True, blocking=False,
            message=f"{check.name}：等式有一边数据不全，等数据齐了再对",
        )
    diff = money_float(decimal_amount(left) - decimal_amount(right))
    passed = abs(decimal_amount(diff)) <= decimal_amount(check.tolerance)
    message = (
        f"{check.name} 对得上"
        if passed
        else f"{check.name} 差 {diff:,.2f} 元（{left:,.2f} 对 {right:,.2f}）。这笔差额必须能解释清楚。"
    )
    return Finding(
        check.id, check.name, passed=passed, blocking=check.blocking and not passed,
        message=check.message or message,
        detail={"left": left, "right": right, "diff": diff, "tolerance": check.tolerance},
    )


def _check_unlinked_disclosed(check, model, facts, links, classify, completeness, nodes, result) -> Finding:
    """未归属金额必须显式呈现。

    现有 BI 就是悄悄丢了这部分，导致店铺利润和支付宝净收支对不上，谁也说不清差在哪。
    """
    if not result.unlinked_buckets:
        return Finding(check.id, check.name, True, False, "没有挂不上订单的钱")

    natural = [b for b in result.unlinked_buckets if b[0] != "看起来是订单的钱"]
    suspicious = [b for b in result.unlinked_buckets if b[0] == "看起来是订单的钱"]
    rows = sum(n for _, n, _ in result.unlinked_buckets)
    lines = [
        f"  · {name} {n:,} 笔，{amount:,.2f} 元 —— 这类本来就没有订单号，不影响利润"
        for name, n, amount in natural
    ]
    lines += [
        f"  · {name} {n:,} 笔，{amount:,.2f} 元 —— 建议看一下"
        for name, n, amount in suspicious
    ]
    message = (
        f"这个月有 {rows:,} 笔钱没对上订单，共 {result.unlinked_total:,.2f} 元。\n"
        + "\n".join(lines)
    )
    passed = not suspicious
    return Finding(
        check.id, check.name, passed=passed, blocking=check.blocking and not passed,
        message=check.message or message,
        detail={"buckets": [
            {"name": n, "rows": r, "amount": a} for n, r, a in result.unlinked_buckets
        ]},
    )


_HANDLERS = {
    "link_rate": _check_link_rate,
    "spine_coverage": _check_coverage,
    "no_unclassified": _check_unclassified,
    "completeness": _check_completeness,
    "tie_out": _check_tie_out,
    "unlinked_disclosed": _check_unlinked_disclosed,
}

_ROLE_LABEL = {
    "shop_owner": "店铺负责人",
    "warehouse": "仓储",
    "logistics": "物流",
    "operations": "运营",
    "finance": "财务",
}


# --------------------------------------------------------------------------- #
# 未归属金额的分类降噪
# --------------------------------------------------------------------------- #


#: 公司级主表里挂不上本店订单的那部分。绝大多数是别家店的，也可能夹着本店漏的单，
#: 所以照样列出来，只是不算进本店未归属总额。
BUCKET_OTHER_STORES = "其他店的数据（公司级主表）"

#: 取键规则链显式判定为非经营流水的行。理财申购、银行间调拨、保证金进出、广告预充值
#: 都在这里——规则链认出来了并且决定不算，报成"挂不上要查"就是自相矛盾。
#: 实测淘宝 5 月有 64 行、47.78 万，全是这类。
BUCKET_EXCLUDED_FLOW = "非经营流水（规则已排除）"

#: 订单号取到了，格式也对，但不在本期的订单明细里。
#:
#: 这是账期边界，不是数据问题。两个来源：一是店长导出时日期选宽了，交上来的对账表
#: 跨了三个月（实测支付宝账单 4/5/6 月各占 28%/42%/30%）；二是跨期结算，淘宝确认
#: 收货后才打款，4 月的订单 5 月才到账。实测淘宝 5 月这类 31,549 行、17.52 万，
#: 其中 99.4% 的订单号在聚水潭 5+6 月表里也查不到，即下单在 4 月及更早。
BUCKET_OTHER_PERIOD = "其他账期的订单"

#: 剩下的才是真要人查的：连订单号都取不出来，也没被规则认领。
BUCKET_NEEDS_WORK = "取不出订单号，要查归属"


def _one_row_once(unlinked: pl.DataFrame, model: Model) -> pl.DataFrame:
    """让一个物理行只算一次。

    一张表会被多个指标共用：淘宝的软件服务费、物流费、赔付、营销费用等七项
    全从同一张对账表出数，每个指标都对整表求值，所以同一物理行在源事实里出现多次——
    实测那 31,618 行各出现了 6 次。

    损益表不受影响，它在投影时按科目过滤过。但未归属统计直接读源事实，不去重就会把
    同一笔钱报六遍：淘宝的未归属会从 30 万虚报成 120 万。这种量级的错报比不报更坏，
    人会因此不信整套账。

    保留「指标口径和这行的科目一致」的那一条：只有它的取数方向和这行相符，
    符号才是对的。
    """
    keys = [c for c in ("file_sha", "sheet", "row_no") if c in unlinked.columns]
    if not keys:
        return unlinked
    majors = {m.id: (m.major or "") for m in model.metrics}
    return (
        unlinked.with_columns(
            pl.col("metric_id")
            .replace_strict(majors, default="", return_dtype=pl.Utf8)
            .alias("__declared_major__")
        )
        .sort(
            (pl.col("__declared_major__") == pl.col("major")).fill_null(False).cast(pl.Int8),
            descending=True,
        )
        .unique(subset=keys, keep="first", maintain_order=True)
        .drop("__declared_major__")
    )


def _bucket_unlinked(facts: pl.DataFrame, model: Model) -> tuple[list[tuple[str, int, float]], float]:
    """把挂不上订单的钱分成"不用管的"和"需要看的"。

    分桶的依据是"为什么挂不上"，因为不同原因要的处置完全不同。混在一起报出来的
    净额没有业务含义：淘宝 5 月那 -30.29 万，是 -47.78 万非经营流水（规则故意排除的）
    和 +17.52 万其他账期订单收款相减的巧合，而真正要人查的只有 5 行、308.31 元。
    把这三样加在一起摆在界面上，用户要么白查一场，要么学会无视这个数——两种都比
    不报更坏。

    字典里已标注"天然无订单号"的科目同样自动归入不用管的那边。
    """
    if facts.is_empty():
        return [], 0.0
    unlinked = facts.filter(~pl.col("linked"))
    if unlinked.is_empty():
        return [], 0.0
    unlinked = _one_row_once(unlinked, model)

    natural = {
        e.raw for e in model.dictionary if e.naturally_unlinked
    }
    naturally_unlinked_metrics = {m.id for m in model.metrics if m.naturally_unlinked}
    company_wide = {s.id for s in model.sources if s.company_wide}

    has_key = pl.col("link_key").is_not_null() & (pl.col("link_key").cast(pl.Utf8) != "")
    tagged = unlinked.with_columns(
        pl.when(pl.col("source_id").is_in(list(company_wide)))
        .then(pl.lit(BUCKET_OTHER_STORES))
        # 规则链的显式决定优先于其他判断：它已经认出这行是什么并且决定不算了。
        .when(pl.col("link_key").cast(pl.Utf8) == EXCLUDED_KEY)
        .then(pl.lit(BUCKET_EXCLUDED_FLOW))
        .when(pl.col("metric_id").is_in(list(naturally_unlinked_metrics)))
        .then(pl.coalesce(pl.col("minor"), pl.col("subject"), pl.lit("其他")))
        .when(pl.col("subject").is_in(list(natural)))
        .then(pl.coalesce(pl.col("minor"), pl.col("subject")))
        .when(has_key)
        .then(pl.lit(BUCKET_OTHER_PERIOD))
        .otherwise(pl.lit(BUCKET_NEEDS_WORK))
        .alias("bucket")
    )
    # 要人查的那一桶排最前。其余按金额排——它们是给人扫一眼确认"哦这些不用管"的，
    # 而要查的那桶是唯一需要行动的，埋在中间就等于没报。
    grouped = {}
    for bucket, amount in tagged.select("bucket", "amount").iter_rows():
        entry = grouped.setdefault(bucket, [0, []])
        entry[0] += 1
        entry[1].append(amount)
    buckets = [
        (bucket, int(values[0]), float(sum_amounts(values[1])))
        for bucket, values in grouped.items()
    ]
    buckets.sort(key=lambda row: (row[0] != BUCKET_NEEDS_WORK, row[2]))
    # 只有真正要人查的才进总额。其余三类都列在桶里让人看得见，但不进总额：
    #   公司级主表      运费和小额打款交上来是全公司的，别家店的运单不该算这家店的账
    #   非经营流水      规则链已经认出并决定不算，再报一遍等于自相矛盾
    #   其他账期的订单  账期边界，那笔钱属于别的月份，本期查不出结果
    # 否则真正需要查的那几百块会被埋在几十万里，谁也不会去看。
    total = float(sum_amounts(
        a for label, _, a in buckets
        if label not in (BUCKET_OTHER_STORES, BUCKET_EXCLUDED_FLOW, BUCKET_OTHER_PERIOD)
    ))
    return buckets, total


def _eval_side(expr, nodes: dict[str, NodeValue]) -> float | None:
    values = []
    for ref in expr.of:
        node = nodes.get(ref)
        if node is None or node.value is None:
            return None
        values.append(node.value)
    if expr.op == "constant":
        return float(expr.value or 0.0)
    return _apply(expr.op, values)


def _metric_name(model: Model, mid: str | None) -> str:
    if not mid:
        return "数据"
    try:
        return model.metric(mid).name
    except KeyError:
        return mid


def report_threshold(model: Model, check: Check) -> float:
    if check.metric:
        try:
            metric = model.metric(check.metric)
        except KeyError:
            return 0.95
        if metric.link:
            return metric.link.min_hit_rate
    return 0.95
