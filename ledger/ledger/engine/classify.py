"""原语五：归类。按字典把原始科目映射到统一科目。

字典是模型数据，引擎只负责查表与未命中告警。未命中时调用 AI 提出建议，人工确认后
写回字典，之后走确定性归类。

现有资产提供 168 条种子（阿里 82、京东1688 37、拼多多 28、抖店 21），
结构是 `业务描述 → 业务小类 → 业务大类`。
"""

from __future__ import annotations

import polars as pl

from ..model.schema import Model, Template, normalize_header
from .rules import ChainStats, compile_classify_rules, resolve_class
from .types import ClassifyReport

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

    majors: list[str | None] = []
    minors: list[str | None] = []
    hits: list[bool] = []
    naturals: list[bool] = []
    classified = 0
    for subject, amount in zip(subjects, amounts):
        entry = table.get(normalize_header(subject))
        if entry is None:
            majors.append(None)
            minors.append(None)
            hits.append(False)
            naturals.append(False)
            label = (str(subject) if subject not in (None, "") else "(空科目)")
            count, total = report.unmatched.get(label, (0, 0.0))
            report.unmatched[label] = (count + 1, total + (float(amount or 0.0)))
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

    amounts = (
        frame.get_column(amount_column).to_list()
        if amount_column and amount_column in frame.columns
        else [0.0] * frame.height
    )
    rows = frame.select(fields).to_dicts() if fields else [{} for _ in range(frame.height)]

    majors: list[str | None] = []
    minors: list[str | None] = []
    hits: list[bool] = []
    excluded: list[bool] = []
    for row, amount in zip(rows, amounts):
        major, minor, drop = resolve_class(row, compiled, lookup, stats)
        majors.append(major)
        minors.append(minor)
        excluded.append(drop)
        hits.append(bool(major) or drop)
        if major:
            report.classified_rows += 1
        elif not drop:
            label = str(row.get(ROLE_SUBJECT) or "").strip() or _fallback_label(row)
            c, a = report.unmatched.get(label, (0, 0.0))
            report.unmatched[label] = (c + 1, a + float(_num(amount)))

    report.chain = stats
    report.excluded_rows = sum(excluded)
    natural = {m for m, (_, _, u) in ((k, v) for k, v in table.items()) if u}
    del natural
    unlinked_majors = {e.major for e in model.dictionary if e.naturally_unlinked}

    return (
        frame.with_columns(
            pl.Series(COL_MAJOR, majors, dtype=pl.Utf8),
            pl.Series(COL_MINOR, minors, dtype=pl.Utf8),
            pl.Series(COL_CLASSIFIED, hits, dtype=pl.Boolean),
            pl.Series(COL_EXCLUDED, excluded, dtype=pl.Boolean),
            pl.Series(
                COL_NATURAL_UNLINKED,
                [m in unlinked_majors if m else False for m in majors],
                dtype=pl.Boolean,
            ),
        ),
        report,
    )


def _fallback_label(row: dict) -> str:
    """业务描述为空时，用其他字段拼一个能让人认出来的标签。"""
    bits = [f"{k}={v}" for k, v in row.items() if k != ROLE_SUBJECT and v not in (None, "")]
    return "（业务描述为空）" + (" ".join(bits[:2]) if bits else "")


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
        for label, (count, amount) in r.unmatched.items():
            c, a = merged.unmatched.get(label, (0, 0.0))
            merged.unmatched[label] = (c + count, a + amount)
    return merged
