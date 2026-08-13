"""原语五：归类。按字典把原始科目映射到统一科目。

字典是模型数据，引擎只负责查表与未命中告警。未命中时调用 AI 提出建议，人工确认后
写回字典，之后走确定性归类。

现有资产提供 168 条种子（阿里 82、京东1688 37、拼多多 28、抖店 21），
结构是 `业务描述 → 业务小类 → 业务大类`。
"""

from __future__ import annotations

import polars as pl

from ..model.schema import Model, Template, normalize_header
from .rules import (
    ChainStats,
    CompiledClassifyRule,
    compile_classify_rules,
    resolve_class,
    text_expr,
)
from .types import ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, ClassifyReport

#: 引擎的角色词汇表里，承载平台原始科目名的角色叫这个名字。
ROLE_SUBJECT = "subject"

COL_MAJOR = "__major__"
COL_MINOR = "__minor__"
COL_CLASSIFIED = "__classified__"
COL_NATURAL_UNLINKED = "__natural_unlinked__"
#: 被归类规则链显式排除的行。保证金解冻这类要清空费项，不参与核算也不算异常。
COL_EXCLUDED = "__excluded_row__"


def classify(
    frame: pl.DataFrame,
    model: Model,
    platform: str,
    amount_column: str | None = None,
    template: Template | None = None,
) -> tuple[pl.DataFrame, ClassifyReport]:
    """归类。模板声明了规则链就走规则链，否则退回单列查字典。

    走规则链的场合：实测支付宝账务明细里 28,615 行业务描述为空，靠业务类型加备注
    关键词归类，其中"交易分账"那 11,462 行正是营销费用的主要来源。只查一列字典的话
    这部分金额会整块丢掉。
    """
    if frame.is_empty():
        return _passthrough(frame), ClassifyReport()

    if template is not None and template.classify_rules:
        return _classify_by_chain(frame, model, platform, amount_column, template)

    report = ClassifyReport(total_rows=frame.height, classified_rows=frame.height)
    if ROLE_SUBJECT not in frame.columns:
        return _passthrough(frame), report

    table = _dictionary_for(model, platform)
    subjects = frame.get_column(ROLE_SUBJECT).to_list()
    amounts = (
        frame.get_column(amount_column).to_list()
        if amount_column and amount_column in frame.columns
        else [0.0] * frame.height
    )

    anchors = _row_anchors(frame)
    majors: list[str | None] = []
    minors: list[str | None] = []
    hits: list[bool] = []
    naturals: list[bool] = []
    classified = 0
    for i, (subject, amount) in enumerate(zip(subjects, amounts)):
        entry = table.get(normalize_header(subject))
        if entry is None:
            majors.append(None)
            minors.append(None)
            hits.append(False)
            naturals.append(False)
            label = (str(subject) if subject not in (None, "") else "(空科目)")
            report.note_unmatched(label, anchors[i], float(amount or 0.0))
            continue
        majors.append(entry[0])
        minors.append(entry[1])
        hits.append(True)
        naturals.append(entry[2])
        classified += 1

    report.classified_rows = classified
    return (
        frame.with_columns(
            pl.Series(COL_MAJOR, majors, dtype=pl.Utf8),
            pl.Series(COL_MINOR, minors, dtype=pl.Utf8),
            pl.Series(COL_CLASSIFIED, hits, dtype=pl.Boolean),
            pl.Series(COL_EXCLUDED, [False] * frame.height, dtype=pl.Boolean),
            pl.Series(COL_NATURAL_UNLINKED, naturals, dtype=pl.Boolean),
        ),
        report,
    )


def _classify_by_chain(
    frame: pl.DataFrame,
    model: Model,
    platform: str,
    amount_column: str | None,
    template: Template,
) -> tuple[pl.DataFrame, ClassifyReport]:
    compiled = compile_classify_rules(template.classify_rules)
    table = _dictionary_for(model, platform)

    def lookup(raw: str):
        return table.get(normalize_header(raw))

    fields = sorted(
        {r.matcher.field for r in compiled if r.matcher and r.matcher.field in frame.columns}
        | ({ROLE_SUBJECT} if ROLE_SUBJECT in frame.columns else set())
    )
    stats = ChainStats()
    report = ClassifyReport(total_rows=frame.height)

    winner = _chain_winner(frame, compiled, table)
    if winner is not None:
        decided = _decide(frame, winner, compiled, table, stats, report)
        majors = decided.get_column(COL_MAJOR)
        minors = decided.get_column(COL_MINOR)
        hits = decided.get_column(COL_CLASSIFIED)
        excluded = decided.get_column(COL_EXCLUDED)
        _note_unmatched(frame, decided, amount_column, fields, report)
    else:
        amounts = (
            frame.get_column(amount_column).to_list()
            if amount_column and amount_column in frame.columns
            else [0.0] * frame.height
        )
        rows = frame.select(fields).to_dicts() if fields else [{} for _ in range(frame.height)]

        anchors = _row_anchors(frame)
        maj: list[str | None] = []
        mnr: list[str | None] = []
        hit: list[bool] = []
        drp: list[bool] = []
        for i, (row, amount) in enumerate(zip(rows, amounts)):
            major, minor, drop = resolve_class(row, compiled, lookup, stats)
            maj.append(major)
            mnr.append(minor)
            drp.append(drop)
            hit.append(bool(major) or drop)
            if major:
                report.classified_rows += 1
            elif not drop:
                label = str(row.get(ROLE_SUBJECT) or "").strip() or _fallback_label(row)
                report.note_unmatched(label, anchors[i], float(_num(amount)))
        majors = pl.Series(COL_MAJOR, maj, dtype=pl.Utf8)
        minors = pl.Series(COL_MINOR, mnr, dtype=pl.Utf8)
        hits = pl.Series(COL_CLASSIFIED, hit, dtype=pl.Boolean)
        excluded = pl.Series(COL_EXCLUDED, drp, dtype=pl.Boolean)

    report.chain = stats
    report.excluded_rows = int(excluded.sum())
    unlinked_majors = sorted({e.major for e in model.dictionary if e.naturally_unlinked})

    return (
        frame.with_columns(
            majors.alias(COL_MAJOR),
            minors.alias(COL_MINOR),
            hits.alias(COL_CLASSIFIED),
            excluded.alias(COL_EXCLUDED),
            majors.is_in(unlinked_majors).fill_null(False).alias(COL_NATURAL_UNLINKED),
        ),
        report,
    )


# --------------------------------------------------------------------------- #
# 规则链的整列算法
# --------------------------------------------------------------------------- #

#: 没有任何一环命中。用一个不可能的规则序号表示，省得整列都是 null 要处处判空。
_NO_RULE = -1


def _chain_winner(
    frame: pl.DataFrame,
    compiled: list[CompiledClassifyRule],
    table: dict[str, tuple[str, str, bool]],
) -> pl.Series | None:
    """整列算出每一行是被第几环接住的。算不了就返回 None，让调用方走逐行。

    规则链的语义是「按顺序试，第一条命中的生效」。这件事看上去必须逐行做，其实不必：
    每一环「适用于哪些行」是一个整列就能算出来的布尔掩码，而「第一条命中的」就是
    把这些掩码按顺序 coalesce 一次。两步都在 Rust 里跑完，中间不回 Python。

    差别有多大：淘宝一家店 139 万行、13 环规则，逐行是 139 万次 Python 函数调用
    套 706 万次字段判定，整列是 13 次列扫描。

    返回的是「第几环」而不是直接返回口径项，因为命中环序号还要用来出规则链统计
    （哪条规则一次都没用上），而且排除、小类、字典命中这几种结果都能从序号推出来。
    """
    if not compiled:
        return None
    exprs: list[pl.Expr] = []
    for i, rule in enumerate(compiled):
        if rule.dictionary:
            if ROLE_SUBJECT not in frame.columns:
                continue  # 没有科目列，这一环对谁都不适用
            hit = norm_subject(frame.schema[ROLE_SUBJECT]).is_in(sorted(table))
        else:
            m = rule.matcher
            assert m is not None
            if not m.vectorizable or m.field not in frame.columns:
                return None
            hit = m.mask(text_expr(frame.schema[m.field], m.field))
        exprs.append(pl.when(hit).then(pl.lit(i, dtype=pl.Int32)))
    if not exprs:
        return None
    return frame.select(
        pl.coalesce([*exprs, pl.lit(_NO_RULE, dtype=pl.Int32)]).alias("i")
    ).get_column("i")


def norm_subject(dtype: pl.DataType) -> pl.Expr:
    """科目名归一的整列版。必须和 `normalize_header` 给出同一个结果。

    `replace_many` 底下是 Aho-Corasick，九个全角字符一趟换完。写成九次
    `replace_all` 也对，但那是九趟列扫描。
    """
    col = pl.col(ROLE_SUBJECT)
    if dtype != pl.Utf8:
        col = col.cast(pl.Utf8)
    return (
        col.fill_null("")
        .str.replace_all(r"[\s\u3000\ufeff]+", "")
        .str.replace_many(list("（）［］｛｝：，．／"), list("()[]{}:,./"))
    )


def _decide(
    frame: pl.DataFrame,
    winner: pl.Series,
    compiled: list[CompiledClassifyRule],
    table: dict[str, tuple[str, str, bool]],
    stats: ChainStats,
    report: ClassifyReport,
) -> pl.DataFrame:
    """把「第几环命中」翻成口径项、小类、是否归类、是否排除。

    非字典环的结果是常量：一个规则序号对应一组固定的值，翻译就是查一张十几项的表。
    字典环的结果要看科目名，那是另一张几十项的表，查法一样。两张表都用
    `replace_strict` 整列查完。
    """
    stats.total = winner.len()
    for i, n in winner.value_counts().iter_rows():
        if i == _NO_RULE:
            stats.unmatched = n
        elif compiled[i].exclude:
            stats.excluded += n
        else:
            stats.hits[i] = n

    by_rule: dict[int, str | None] = {_NO_RULE: None}
    minor_by_rule: dict[int, str | None] = {_NO_RULE: None}
    drop_by_rule: dict[int, bool] = {_NO_RULE: False}
    dict_rule: int | None = None
    for i, rule in enumerate(compiled):
        if rule.dictionary:
            dict_rule = i
            # 字典环的口径项按行查，这里先占位；drop 恒为 False。
            by_rule[i] = None
            minor_by_rule[i] = None
            drop_by_rule[i] = False
        else:
            by_rule[i] = None if rule.exclude else rule.major
            # 规则没写细项就留空，界面退回显示平台自己那个科目名。填大类的话，
            # 填进去的是 `software_fee` 这种内部代号——它会一路漏到下钻和检索的
            # 科目栏上，人看到的是一个自己表里根本不存在的词。
            minor_by_rule[i] = None if rule.exclude else rule.minor
            drop_by_rule[i] = rule.exclude

    work = pl.DataFrame({"i": winner})
    major = pl.col("i").replace_strict(by_rule, default=None, return_dtype=pl.Utf8)
    minor = pl.col("i").replace_strict(minor_by_rule, default=None, return_dtype=pl.Utf8)

    if dict_rule is not None and ROLE_SUBJECT in frame.columns:
        work = work.with_columns(
            frame.select(norm_subject(frame.schema[ROLE_SUBJECT]).alias("k")).get_column("k")
        )
        on_dict = pl.col("i") == dict_rule
        major = (
            pl.when(on_dict)
            .then(pl.col("k").replace_strict(
                {k: v[0] for k, v in table.items()}, default=None, return_dtype=pl.Utf8
            ))
            .otherwise(major)
        )
        minor = (
            pl.when(on_dict)
            .then(pl.col("k").replace_strict(
                {k: v[1] for k, v in table.items()}, default=None, return_dtype=pl.Utf8
            ))
            .otherwise(minor)
        )

    out = work.select(
        major.alias(COL_MAJOR),
        minor.alias(COL_MINOR),
        pl.col("i").replace_strict(drop_by_rule, default=False, return_dtype=pl.Boolean)
        .alias(COL_EXCLUDED),
    )
    # 「归类到了」的判定跟着逐行版走：口径项非空，或者被显式排除。字典里存在
    # 口径项为空的条目，那种行查得到但等于没归类，要留给未分类清单去报。
    got = pl.col(COL_MAJOR).is_not_null() & (pl.col(COL_MAJOR) != "")
    out = out.with_columns((got | pl.col(COL_EXCLUDED)).alias(COL_CLASSIFIED))
    report.classified_rows = int(out.select(got.sum()).item())
    return out


def _note_unmatched(
    frame: pl.DataFrame,
    decided: pl.DataFrame,
    amount_column: str | None,
    fields: list[str],
    report: ClassifyReport,
) -> None:
    """把没归上类的行记进报告，带上金额和物理位置。

    这一段仍然是逐行的，而且没必要不逐行：它要产出的是给人看的清单——哪个科目、
    多少行、多少钱、在哪个文件第几行。逐行的代价只按未归类的行数算，
    实测三家店本期是零行，等于不花钱。真出现大批未归类时慢一点也无所谓，
    那时候要解决的是「为什么没归上」，不是「报告出得快不快」。
    """
    unmatched = (
        ~decided.get_column(COL_CLASSIFIED) & ~decided.get_column(COL_EXCLUDED)
    )
    n = int(unmatched.sum())
    if not n:
        return

    take = frame.with_row_index("__i__").filter(unmatched)
    amounts = (
        take.get_column(amount_column).to_list()
        if amount_column and amount_column in take.columns
        else [0.0] * n
    )
    anchors = _row_anchors(frame)
    rows = take.select(fields).to_dicts() if fields else [{}] * n
    for row, amount, at in zip(rows, amounts, take.get_column("__i__").to_list()):
        label = str(row.get(ROLE_SUBJECT) or "").strip() or _fallback_label(row)
        report.note_unmatched(label, anchors[at], float(_num(amount)))


def _fallback_label(row: dict) -> str:
    """业务描述为空时，用其他字段拼一个能让人认出来的标签。"""
    bits = [f"{k}={v}" for k, v in row.items() if k != ROLE_SUBJECT and v not in (None, "")]
    return "（业务描述为空）" + (" ".join(bits[:2]) if bits else "")


def _row_anchors(frame: pl.DataFrame) -> list[tuple[str, str, str]]:
    """每行的物理位置。用来跨指标认出同一行，别把一行数成七行。

    锚点列缺失时退回行序号：同一张表被多个指标各归类一遍，行顺序是一样的，
    所以按序号照样能认出是同一行。
    """
    if all(c in frame.columns for c in (ANCHOR_SHA, ANCHOR_SHEET, ANCHOR_ROW)):
        cols = frame.select(ANCHOR_SHA, ANCHOR_SHEET, ANCHOR_ROW)
        return [(str(a or ""), str(b or ""), str(c or "")) for a, b, c in cols.iter_rows()]
    return [("", "", str(i)) for i in range(frame.height)]


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _passthrough(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.lit(None, dtype=pl.Utf8).alias(COL_MAJOR),
        pl.lit(None, dtype=pl.Utf8).alias(COL_MINOR),
        pl.lit(True).alias(COL_CLASSIFIED),
        pl.lit(False).alias(COL_EXCLUDED),
        pl.lit(False).alias(COL_NATURAL_UNLINKED),
    )


def _dictionary_for(model: Model, platform: str) -> dict[str, tuple[str, str, bool]]:
    """平台条目覆盖通用条目。同一科目名在不同平台可以有不同归类。"""
    table: dict[str, tuple[str, str, bool]] = {}
    for entry in model.dictionary:
        if entry.platform == "*":
            table[normalize_header(entry.raw)] = (entry.major, entry.minor, entry.naturally_unlinked)
    for entry in model.dictionary:
        if entry.platform == platform:
            table[normalize_header(entry.raw)] = (entry.major, entry.minor, entry.naturally_unlinked)
    return table


def merge_reports(reports: list[ClassifyReport]) -> ClassifyReport:
    merged = ClassifyReport()
    for r in reports:
        merged.total_rows += r.total_rows
        merged.classified_rows += r.classified_rows
        for label, rows in r.unmatched_rows.items():
            merged.unmatched_rows.setdefault(label, {}).update(rows)
    return merged
