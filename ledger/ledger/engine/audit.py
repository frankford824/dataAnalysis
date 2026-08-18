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

    # 这里原先拿字符串 "看起来是订单的钱" 去比桶名。那个桶名早就改成了
    # BUCKET_NEEDS_WORK，比对因此一条也匹配不上，`suspicious` 恒为空，这条检查
    # 于是永远是绿的——一条永远通过的检查和没有这条检查是一回事，而它看起来还像在把关。
    # 换成认常量，改桶名时这里跟着走。
    explained = [b for b in result.unlinked_buckets if b[0] != BUCKET_NEEDS_WORK]
    suspicious = [b for b in result.unlinked_buckets if b[0] == BUCKET_NEEDS_WORK]
    rows = sum(n for _, n, _ in result.unlinked_buckets)
    # 要人查的那桶排最前，和界面上的顺序、和分桶时的排序都一致。它是这几行里唯一
    # 需要行动的，排在三条「不用管」后面就等于没报。
    lines = [
        f"  · {name} {n:,} 笔，{amount:,.2f} 元 —— 要人查清归属"
        for name, n, amount in suspicious
    ]
    lines += [
        f"  · {name} {n:,} 笔，{amount:,.2f} 元 —— {BUCKET_WHY.get(name, '有解释，不影响利润')}"
        for name, n, amount in explained
    ]
    # 第一行是结论，后面每一行是一桶。界面按这个结构摆：结论正常写，逐桶的那几行
    # 列成条目。所以结论要一句写完，不能把「分几类看」单起一行——它会被当成第一个
    # 条目，读起来像多出来一条没有金额的桶。
    tally = f"这个月共 {rows:,} 笔钱没对上订单，分 {len(result.unlinked_buckets)} 类："
    passed = not suspicious
    if passed:
        head = tally
    else:
        # 数字由引擎给，建议由模型给，两者都要——和覆盖率那条同一个道理：
        # 光有建议看不出有多少钱要查，光有数字不知道该拿它怎么办。
        #
        # 笔数和金额要取自同一桶。原先笔数取「要查的那桶」、金额取未归属总额，
        # 而总额里还含着提现、保证金这些有解释的钱：天猫皇莉诗那句话写的是
        # 「395 笔、-133,546.41 元」，其中 -124,071.03 是两笔提现，真要查的
        # 只有 -3,146.49。人照着这个数去查，先查到的是一个不需要查的东西。
        n = sum(c for _, c, _ in suspicious)
        amount = float(sum_amounts(a for _, _, a in suspicious))
        head = (
            f"有 {n:,} 笔、{amount:,.2f} 元既取不出订单号、也没有规则认领。"
            + (check.message or "这部分不能悄悄丢掉，也不能硬摊进利润。")
            + tally
        )
    message = head + "\n" + "\n".join(lines)
    return Finding(
        check.id, check.name, passed=passed, blocking=check.blocking and not passed,
        message=message,
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

#: 每一桶为什么挂不上，一句话。
#:
#: 写在这里而不是写在界面上，是因为桶名是这个文件定的：两边各存一份的话，
#: 改个桶名界面就少一句解释，而少一句解释不会报错——界面上那一列曾经整列是空的，
#: 就是这么来的（前端读的是 name，后端给的是 label）。
BUCKET_WHY = {
    BUCKET_OTHER_STORES: "运费、小额打款这类表交上来是全公司一份，别家店的行挂不到这家店",
    BUCKET_EXCLUDED_FLOW: "理财申购、银行间调拨、保证金进出、广告预充值——规则链认出来了并且决定不算",
    BUCKET_OTHER_PERIOD: "订单号取到了、格式也对，但不在本期的订单明细里。跨期结算和导出时日期选宽了都会这样",
    BUCKET_NEEDS_WORK: "连订单号都取不出来，也没有任何规则认领它。这一类要人去查",
}

#: 不算进未归属总额的那几桶。它们各有各的解释，加进总额只会把真正要查的那几百块
#: 埋在几十万里。这份名单是 `_bucket_unlinked` 和对外结构共用的——两处各写一份的话，
#: 界面上标着「不计入合计」的行和实际算进合计的行迟早会不是同一批。
BUCKET_EXPLAINED = (BUCKET_OTHER_STORES, BUCKET_EXCLUDED_FLOW, BUCKET_OTHER_PERIOD)


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
    natural = {
        e.raw for e in model.dictionary if e.naturally_unlinked
    }
    # 天然无订单号也可以由大类判定，不是只看科目名。
    #
    # 原先只认两件事：科目名在字典里标了，或者整个指标标了。可归类规则链同样能定大类——
    # 微信账单里那四笔提现的业务描述整列是空的，字典无从查起，是模板规则按入账类型
    # 判成提现的。于是 -195,711.65 元（其中 2026-06 占 -124,071.03）落进了
    # 「取不出订单号，要查归属」，比这家店整月利润还大，而且它是拦着结账的那一桶。
    # 提现本来就没有订单号，让人去查一笔银行搬运的归属是查不出结果的。
    #
    # 两个条件都要满足，不能只看字典标没标。字典的这个标记挂在具体科目上而不是大类上：
    # 拼多多那四条「售后费用-延迟发货」之类标着天然无号，它们的大类是 trade_compensation,
    # 只按大类反推就会把整个交易赔付大类都豁免掉——淘宝的「记账本转账」那 45 笔会跟着
    # 一起被放行，而那些是真该查的（订单号在备注里，取号规则漏了）。
    # 加上「没有任何指标认领这个大类」这一条之后，剩下的正好是提现、保证金、广告充值、
    # 往来款：它们进得来但一处都不进损益，问它们挂不挂得上订单没有意义。
    #
    # 反过来「没有指标声明这个大类」单独也不够，不能省掉字典那个标记：代发成本的大类
    # dropship_cost 同样没有任何指标声明——代发成本走的是自己那张表，指标不写 major，
    # major 这个字段只在对账表里用（一张表供给多个指标，靠它区分）。只按「没人声明」
    # 判的话，代发成本会被当成不进损益的钱豁免掉，而它是实实在在进利润的。
    claimed_majors = {m.major for m in model.metrics if m.major}
    natural_majors = {
        e.major for e in model.dictionary
        if e.naturally_unlinked and e.major and e.major not in claimed_majors
    }
    naturally_unlinked_metrics = {m.id for m in model.metrics if m.naturally_unlinked}
    company_wide = {s.id for s in model.sources if s.company_wide}

    # 收进来的不只是挂不上订单的行，还有「挂上了订单、但归到的口径项一处都不进损益」
    # 的行。后者不进这份清单的话，它在界面上一处都不出现：损益表没有它（没人认领），
    # 「没进利润的钱」也没有它（那份原先只收挂不上的行）。
    #
    # 实测出来的：保证金-天猫-出账缴存改判成充值之后，天猫皇莉诗 2026-06 有 159 行、
    # -2,090.32 元就这样消失了——它们带着正常的订单号，挂得上，于是躲过了这份清单。
    # 同一个科目挂不上的另外 58 行 -6,110.50 反而看得见。一笔钱在界面上出不出现，
    # 取决于它有没有订单号，这件事没法跟人解释。
    unclaimed = pl.col("major").is_in(list(natural_majors)).fill_null(False)
    unlinked = facts.filter(~pl.col("linked") | unclaimed)
    if unlinked.is_empty():
        return [], 0.0
    unlinked = _one_row_once(unlinked, model)

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
        .when(pl.col("major").is_in(list(natural_majors)))
        .then(pl.coalesce(pl.col("minor"), pl.col("subject"), pl.col("major")))
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
        a for label, _, a in buckets if label not in BUCKET_EXPLAINED
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
