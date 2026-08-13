"""引擎编排。把七类原语串成一次运行。

    识别 → 解析 → 归一 → 挂钩 → 归类 → 核算 → 自检

这个文件是唯一知道原语先后顺序的地方，仍然不含任何公司知识：它读模型、按模型说的做。
"""

from __future__ import annotations

import itertools
import os
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from ..model.schema import Metric, Model, Template
from . import calculate as calc
from .audit import AuditResult, audit
from .project import Projection, claims, project
from .derivative import Derivative, detect as detect_derivative
from .controls import ControlResult, summarize as summarize_controls, verify as verify_controls
from .classify import classify, merge_reports
from .link import SPINE_PERIOD, SPINE_PRODUCT, SPINE_STORE, Spine, link, target_role
from .normalize import NormalizeError, normalize
from .parse import ParseError, digest, parse
from .recognize import infer_period, infer_store, match_headers
from .types import (
    ClassifyReport,
    Completeness,
    FileRef,
    LinkReport,
    RawTable,
    Recognition,
)

#: 引擎的角色词汇表里表示店铺名的角色。
ROLE_STORE = "store_name"
ROLE_PRODUCT = "product_id"


@dataclass
class Ingested:
    """一张表的接收结果。"""

    ref: FileRef
    recognition: Recognition
    rows: int = 0
    frame: pl.DataFrame | None = None
    template: Template | None = None
    notes: list[str] = field(default_factory=list)
    #: 文件自带控制总数的核对结果。空表示这个文件没提供控制总数。
    controls: list[ControlResult] = field(default_factory=list)
    #: 判定为人工加工产物时的依据。非空表示这张表不参与算钱。
    derivative: Derivative | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.frame is not None and not self.error


@dataclass
class Ingestion:
    """一批文件的接收结果。识别与解析的产物，还没算钱。"""

    model: Model
    items: list[Ingested] = field(default_factory=list)

    @property
    def known(self) -> list[Ingested]:
        return [i for i in self.items if i.ok]

    @property
    def unknown(self) -> list[Ingested]:
        return [i for i in self.items if not i.ok]

    def summary(self) -> str:
        """`12 个文件，11 个认识，1 个没见过`——认出来这一节就显示这句。"""
        total, ok = len(self.items), len(self.known)
        line = f"{total} 张表，{ok} 个认识"
        if total - ok:
            line += f"，{total - ok} 个没见过"
        return line

    def uploaded_sources(self) -> set[str]:
        return {i.recognition.source_id for i in self.known if i.recognition.source_id}

    def frames_of(self, source_id: str) -> list[Ingested]:
        return [i for i in self.known if i.recognition.source_id == source_id]


@dataclass
class Slice:
    """一个店一个账期的完整核算结果。这就是主界面渲染的全部输入。"""

    store: str
    period: str
    nodes: dict[str, calc.NodeValue]
    facts: pl.DataFrame
    completeness: Completeness
    audit: AuditResult
    link_reports: dict[str, LinkReport]
    classify_report: ClassifyReport

    @property
    def can_close(self) -> bool:
        return self.audit.can_close and self.completeness.ok


@dataclass
class RunResult:
    model: Model
    ingestion: Ingestion
    facts: pl.DataFrame
    slices: dict[tuple[str, str], Slice] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    spine_rows: int = 0
    #: 投影到脊柱后的事实。损益表从这里出数。
    spine_facts: pl.DataFrame = field(default_factory=lambda: _empty_spine_facts())
    #: 每个指标的投影情况：孤儿金额、未覆盖行数。
    projections: dict[str, Projection] = field(default_factory=dict)
    #: 订单脊柱本身。spine_facts 里的 spine_row 是它的行号。
    #:
    #: 暴露出来是为了下钻：损益表上的任何一个数，要能一路追到具体哪几个订单、
    #: 每单摊了多少。只报总数不给明细的话，对不上账时没人查得动。
    spine: pl.DataFrame = field(default_factory=pl.DataFrame)

    def slice(self, store: str, period: str) -> Slice | None:
        return self.slices.get((store, period))

    @property
    def stores(self) -> list[str]:
        return sorted({s for s, _ in self.slices})

    @property
    def periods(self) -> list[str]:
        return sorted({p for _, p in self.slices}, reverse=True)


# --------------------------------------------------------------------------- #
# 接收
# --------------------------------------------------------------------------- #


def ingest(
    paths: list[str | Path],
    model: Model,
    known_stores: list[str] | None = None,
    each: Callable[[int, int], None] | None = None,
) -> Ingestion:
    """识别 + 解析 + 归一。一个文件里的每张工作表单独处理。

    文件之间互不相干，所以并行读。能并行是因为底下两层重活都不占着 Python 解释器：
    calamine 解 xlsx 在 Rust 里、Polars 归一也在 Rust 里，两边都放开 GIL。实测六个
    文件并行读比串行快 2.2 倍。

    并行不影响结果：每个文件各自产出自己那几张表，最后按传进来的文件顺序拼回去。
    顺序要紧——跨文件去重是「先到的留下」，顺序变了留下的就是另一份（内容一样，
    但留痕里写的文件名会变，那会让人以为数据变了）。

    线程数压在 4：并行读意味着几个工作簿同时摊在内存里，对账表一份就一个多 G，
    放开了跑内存会先撑不住。真正的瓶颈也不在这儿——超过四个之后 GIL 争用就把
    收益吃掉了。

    `each(读完几份, 一共几份)` 是给界面报进度的。这一段是交表里最慢的一截，几十秒
    里界面一个数都不动的话，人分不出它是在读表还是已经死了。并行下报的是「读完
    第几份」而不是「正在读哪一份」——同时有四份在读，说哪一份都是错的。
    """
    result = Ingestion(model=model)
    work = [Path(p) for p in paths]
    candidates = _header_row_candidates(model)
    stores = known_stores or []
    done = itertools.count(1)

    def one(p: Path) -> list[Ingested]:
        got = _ingest_file(p, model, stores, candidates)
        if each:
            # count() 的自增是原子的（CPython 里由 C 层做完），几个读线程同时报
            # 也不会数错。
            each(next(done), len(work))
        return got

    if len(work) > 1:
        with ThreadPoolExecutor(max_workers=min(len(work), _READERS)) as pool:
            batches = list(pool.map(one, work))
    else:
        batches = [one(p) for p in work]

    for batch in batches:
        result.items.extend(batch)
    _dedupe_across_files(result, model)
    return result


#: 同时读几个文件。见 `ingest` 里为什么是这个数。
#:
#: 留了环境变量是给内存小的机器用的：并行读的代价是几个工作簿同时在内存里，
#: 一台只有 8 G 的机器上把它调成 1，慢一点但不会被系统杀掉。
_READERS = max(1, int(os.environ.get("LEDGER_READERS", "4")))


def _ingest_file(
    path: Path, model: Model, known_stores: list[str], candidates: list[int]
) -> list[Ingested]:
    """一个文件读出来的全部表。不碰任何共享状态，才能并行跑。"""
    items: list[Ingested] = []
    sha = digest(path)
    hint_period = infer_period(path.name)
    hint_store = infer_store(path.name, known_stores)
    try:
        tables = parse(path)
    except ParseError as exc:
        return [
            Ingested(
                ref=FileRef(sha256=sha, filename=path.name),
                recognition=Recognition(
                    ref=FileRef(sha256=sha, filename=path.name),
                    signature="", header_count=0, reason=str(exc),
                ),
                error=str(exc),
            )
        ]

    for table in tables:
        table.ref = FileRef(sha256=sha, filename=path.name, sheet=table.ref.sheet)
        recog, header_row = _recognize_any_header_row(table, model, candidates)
        item = Ingested(ref=table.ref, recognition=recog, rows=len(table), notes=list(table.notes))

        # 表头上方的说明文字。加工产物常在这里自述来源（补发表第一行就写着
        # 「路径：聚水潭成本表内订单类型筛选补发订单粘贴过来」），
        # 但按模板重解后表头行下移，这几行会被整个跳过，判定就看不见了。
        preamble = [list(table.headers), *(r.cells for r in table.rows[:2])]

        if not recog.known:
            # 认不出来的表也判一次。人工汇总表本来就不该有模板，
            # 报「这是汇总表，不用交」比报「认不出来」有用得多。
            verdict = detect_derivative(table.headers, [r.cells for r in table.rows[:3]])
            item.derivative = verdict if verdict else None
            item.error = (
                f"人工加工产物，不参与算钱：{verdict.reason}" if verdict else recog.reason
            )
            items.append(item)
            continue

        if header_row:
            item.notes.append(f"表头在第 {header_row + 1} 行，前面 {header_row} 行是说明文字")
        template = model.template(recog.template_id)  # type: ignore[arg-type]
        item.template = template
        # 模板可能声明了不同的表头行或分隔符，需要按模板重解一次。
        if _needs_reparse(template):
            try:
                tables_again = parse(path, template.parse, sha=sha)
            except ParseError as exc:
                item.error = str(exc)
                items.append(item)
                continue
            table = next(
                (t for t in tables_again if t.ref.sheet == table.ref.sheet), tables_again[0]
            )
            item.rows = len(table)

        # 人工加工产物不进账：数据在上游源表里已经有一份，摄进去是把同一笔钱记两遍。
        #
        # 必须放在按模板重解之后。加工痕迹长在真表头上（透视字段前缀、整块重复的列名），
        # 而这批文件的表头普遍不在第一行——用初解的表头去判，看到的是说明文字，
        # 会把淘宝对账表这种真数据误判成加工产物，实测销售收入会凭空少 4.5 万。
        verdict = detect_derivative(
            table.headers, [*preamble, *(r.cells for r in table.rows[:3])]
        )
        if verdict:
            item.derivative = verdict
            item.error = f"人工加工产物，不参与算钱：{verdict.reason}"
            items.append(item)
            continue

        try:
            frame, notes = normalize(table, template)
        except NormalizeError as exc:
            item.error = str(exc)
            items.append(item)
            continue
        item.notes.extend(notes)
        item.controls = verify_controls(table, frame, template)
        if item.controls:
            item.notes.append(summarize_controls(item.controls))
        frame = _attach_hints(frame, hint_store, hint_period)
        item.frame = frame
        items.append(item)
    return items


def _dedupe_across_files(result: Ingestion, model: Model) -> None:
    """同一数据源收到多份文件时，按去重键跨文件去重。

    全公司共用的主表（运费、小额打款）每个店长都会导出一份交上来，内容是同一批数据。
    实测三份运费文件的 299,554 个运单号完全重合，直接拼接会让运费变成三倍。

    先到的文件保留，后到的只留新增行。哪个文件先到不影响结果，因为重复行本来就一样。
    去掉多少行要说出来——这是证据，不是可以悄悄做掉的清洗。
    """
    for source in model.sources:
        if not source.dedupe_key:
            continue
        items = [i for i in result.items if i.recognition.source_id == source.id and i.frame is not None]
        if len(items) < 2:
            continue
        keys = [k for k in source.dedupe_key if all(k in i.frame.columns for i in items)]
        if not keys:
            missing = ", ".join(source.dedupe_key)
            for item in items:
                item.notes.append(f"声明了去重键（{missing}）但数据里没有这些列，没法跨文件去重")
            continue

        seen: pl.DataFrame | None = None
        for item in items:
            frame = item.frame
            before = frame.height
            frame = frame.unique(subset=keys, keep="first", maintain_order=True)
            if seen is not None:
                frame = frame.join(seen, on=keys, how="anti")
            kept = frame.height
            if kept < before:
                item.notes.append(
                    f"按 {'+'.join(keys)} 去重，{before:,} 行留下 {kept:,} 行"
                    f"（去掉 {before - kept:,} 行与其他文件重复的数据）"
                )
            item.frame = frame
            part = frame.select(keys)
            seen = part if seen is None else pl.concat([seen, part], how="vertical_relaxed")


def _header_row_candidates(model: Model) -> list[int]:
    """模型里出现过的表头行位置。识别时逐个试。

    先有鸡先有蛋：要识别模板得先有表头，要知道表头在第几行得先知道模板。
    解法是把模型声明过的表头行都当候选试一遍——候选集来自模型数据，
    不是写死的 0/1/2。
    """
    return sorted({0} | {t.parse.header_row for t in model.templates})


def _recognize_any_header_row(
    table: RawTable, model: Model, candidates: list[int]
) -> tuple[Recognition, int]:
    """在候选表头行上依次尝试识别，返回第一个认得出来的。

    不需要重读文件：按第 0 行当表头解析出来之后，第 h 行数据就是候选表头。
    """
    first = match_headers(table.headers, model, table.ref)
    if first.known or not table.rows:
        return first, 0
    for h in candidates:
        if h == 0 or h > len(table.rows):
            continue
        headers = [str(c).strip() if c is not None else "" for c in table.rows[h - 1].cells]
        recog = match_headers(headers, model, table.ref)
        if recog.known:
            return recog, h
    return first, 0


def _needs_reparse(template: Template) -> bool:
    p = template.parse
    return bool(p.sheet) or p.header_row != 0 or p.skip_after_header != 0 or bool(p.delimiter)


def _attach_hints(frame: pl.DataFrame, store: str | None, period: str | None) -> pl.DataFrame:
    """文件名推断出的店铺与账期作为兜底，数据里有就用数据里的。"""
    return frame.with_columns(
        pl.lit(store, dtype=pl.Utf8).alias("__hint_store__"),
        pl.lit(period, dtype=pl.Utf8).alias("__hint_period__"),
    )


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #


def run(ingestion: Ingestion, platform: str = "*") -> RunResult:
    """挂钩 → 归类 → 核算 → 自检。"""
    model = ingestion.model
    notes: list[str] = []
    spine = _build_spine(ingestion, notes)

    bridges = _build_bridges(ingestion, notes)

    fact_parts: list[pl.DataFrame] = []
    link_reports: dict[str, LinkReport] = {}
    classify_reports: list[ClassifyReport] = []

    # 平台限定的指标只在对应平台生效。三家店的利润口径互不相同，
    # 全部一起算会让 1688 的收支口径混进淘宝的账。下面两个循环都要按这份名单走。
    metrics = [r for r in (m.for_platform(platform) for m in model.metrics) if r is not None]

    for metric in metrics:
        items = ingestion.frames_of(metric.source)
        if not items:
            continue
        if metric.link and metric.link.to:
            spine.build(target_role(metric.link.to))
        supplied = False
        for item in items:
            assert item.frame is not None and item.template is not None
            # 一个数据源下可以有好几张形状不同的表：1688 的对账文件里，收款明细只有
            # 「已收金额」、付款明细只有「已付金额」。一张表拿不出这条指标要的角色，
            # 说明它不供给这条指标，跳过它是陈述事实，不是出错。
            if any(r not in item.frame.columns for r in metric.value.of):
                continue
            supplied = True
            frame, report = link(item.frame, metric, spine, item.template, bridges)
            _merge_link(link_reports, metric.id, report)

            # 未归类科目要带准确金额，那是用户判断该不该管的唯一依据。只取取值
            # 表达式的第一个角色是不够的：支付宝把一笔钱拆成收入、支出两栏，余利宝
            # 申购那 88 行的钱全在支出栏，只看收入栏会报成 0 元，看着像不用管的小事，
            # 实际是 81 万的资金划转。
            frame = _with_row_amount(frame, metric)
            frame, creport = classify(frame, model, platform, ROW_AMOUNT, item.template)
            classify_reports.append(creport)

            hint_store = _first_hint(item.frame, "__hint_store__")
            hint_period = _first_hint(item.frame, "__hint_period__")
            try:
                facts, fnotes = calc.evaluate_metric(
                    frame, metric, item.template, hint_store or "", hint_period or ""
                )
            except calc.CalculateError as exc:
                notes.append(f"{item.ref.label()} 算 {metric.name} 出错：{exc}")
                continue
            notes.extend(fnotes)
            if not facts.is_empty():
                fact_parts.append(facts)

        # 一张表都拿不出这条指标要的角色，那多半是角色名写错了，不能悄悄当成零。
        if not supplied:
            roles = "、".join(metric.value.of)
            notes.append(
                f"{metric.name} 没算出任何数：数据源 {metric.source} 下的 {len(items)} 张表"
                f"都没有 {roles} 这个角色。检查模板绑定里的角色名。"
            )

    facts = pl.concat(fact_parts, how="vertical_relaxed") if fact_parts else calc._empty_facts()

    # 投影到脊柱。源事实是证据链（一行一条源记录，带文件行号），脊柱事实是口径
    # （一行一条订单记录，含分摊）。损益表从脊柱事实出数，否则主订单级的钱会被
    # 每个子订单各算一遍。
    spine_parts: list[pl.DataFrame] = []
    projections: dict[str, Projection] = {}
    for metric in metrics:
        if not (metric.link and metric.link.to):
            continue
        proj = project(facts, metric, spine)
        projections[metric.id] = proj
        notes.extend(proj.notes)
        if not proj.facts.is_empty():
            spine_parts.append(proj.facts)
    spine_facts = (
        pl.concat(spine_parts, how="vertical_relaxed") if spine_parts else _empty_spine_facts()
    )
    facts = _mark_counted(facts, spine_facts, metrics)

    result = RunResult(
        model=model, ingestion=ingestion, facts=facts, notes=notes, spine_rows=spine.size,
        spine_facts=spine_facts, projections=projections, spine=spine.frame,
    )
    classify_report = merge_reports(classify_reports)

    for store, period in _slice_keys(spine_facts if not spine_facts.is_empty() else facts):
        result.slices[(store, period)] = _build_slice(
            model, ingestion, facts, spine_facts, spine.frame, store, period,
            link_reports, classify_report, platform,
        )
    return result


def _mark_counted(facts: pl.DataFrame, spine_facts: pl.DataFrame,
                  metrics: Sequence[Metric]) -> pl.DataFrame:
    """给每条源记录标上：它进没进损益表，进了多少。

    源事实是「这张表里有这么一行」，脊柱事实是「这笔钱算进了账」。两者差得很远，
    而且差多少完全看不出来：淘宝那家店的运费表是全公司的运单，29.9 万行里只有
    1.4 万行挂得上这家店的订单，其余 28.5 万行属于别的店铺。不标出来，点开「发货运费」
    看到的是 -550,944，而报表上写着 -20,294——人只会认为报表算错了。

    进账金额不等于原始金额，因为粗粒度的钱要按比例摊到脊柱行上。这里按
    「这个键在脊柱上分到的比例之和」折算，所以逐行加总恰好等于报表数字，
    不是一个差不多的估计。

    挂不上的行照样留档，只是标成没进账。它们是真实存在的记录，删掉就没法回答
    「这笔钱到底去哪了」——而这个问题每个月都会被问到。

    光看键匹不匹配是不够的：对账表有五个指标读它，同一个订单号在五个指标的脊柱事实
    里都在。只按键标的话，一笔软件服务费会在「销售收入」名下也标成进账——检索里
    这行钱就顶着「销售收入」的名字出来。所以还要过一遍认领条件。
    """
    if "counted" in facts.columns:
        return facts
    if spine_facts.is_empty() or facts.is_empty():
        return facts.with_columns(
            pl.lit(False, dtype=pl.Boolean).alias("counted"),
            pl.lit(0.0, dtype=pl.Float64).alias("contribution"),
        )
    weights = spine_facts.group_by("metric_id", "store", "period", "link_key").agg(
        pl.col("factor").sum().alias("__share__")
    )
    claim = pl.lit(False)
    for metric in metrics:
        claim = claim | claims(metric)
    return (
        facts.join(weights, on=["metric_id", "store", "period", "link_key"], how="left")
        .with_columns(
            (claim & pl.col("__share__").is_not_null()).alias("counted"),
        )
        .with_columns(
            pl.when(pl.col("counted"))
            .then(pl.col("amount") * pl.col("__share__"))
            .otherwise(pl.lit(0.0))
            .alias("contribution"),
        )
        .drop("__share__")
    )


def _empty_spine_facts() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "metric_id": pl.Utf8, "source_id": pl.Utf8, "store": pl.Utf8, "period": pl.Utf8,
            "link_key": pl.Utf8, "amount": pl.Float64, "factor": pl.Float64,
            "spine_row": pl.UInt32,
        }
    )


def _build_bridges(ingestion: Ingestion, notes: list[str]) -> dict[str, dict[str, str]]:
    """建跨表回查索引。

    运费表只有运单号，要先去订单明细按物流单号回查主订单编号，查不到再去聚水潭
    按快递单号回查原始线上订单号。索引在这里一次建好，逐行回查时直接查字典。
    """
    wanted: dict[str, tuple[str, str]] = {}
    for t in ingestion.model.templates:
        for rule in t.key_rules:
            if rule.via is not None:
                wanted[rule.via.source] = (rule.via.match, rule.via.take)

    out: dict[str, dict[str, str]] = {}
    for source_id, (match_role, take_role) in wanted.items():
        index: dict[str, str] = {}
        for item in ingestion.frames_of(source_id):
            frame = item.frame
            if frame is None or match_role not in frame.columns or take_role not in frame.columns:
                continue
            pairs = (
                frame.select(
                    pl.col(match_role).cast(pl.Utf8).alias("k"),
                    pl.col(take_role).cast(pl.Utf8).alias("v"),
                )
                .drop_nulls()
                .unique(subset=["k"], keep="first")
            )
            for k, v in pairs.iter_rows():
                if k and v and k not in index:
                    index[k] = v
        out[source_id] = index
        notes.append(f"回查索引 {source_id}：{match_role} → {take_role}，{len(index):,} 条")
    return out


def _slice_keys(facts: pl.DataFrame) -> list[tuple[str, str]]:
    if facts.is_empty():
        return []
    pairs = (
        facts.filter(pl.col("store") != "(未知店铺)")
        .select("store", "period")
        .unique()
        .sort("store", "period")
    )
    return [(r[0], r[1]) for r in pairs.iter_rows()]


def _build_slice(
    model: Model,
    ingestion: Ingestion,
    facts: pl.DataFrame,
    spine_facts: pl.DataFrame,
    spine: pl.DataFrame,
    store: str,
    period: str,
    link_reports: dict[str, LinkReport],
    classify_report: ClassifyReport,
    platform: str,
) -> Slice:
    scoped = facts.filter((pl.col("store") == store) & (pl.col("period") == period))
    # 损益从脊柱事实出数；源事实留作证据链与挂钩率统计。
    scoped_spine = (
        spine_facts.filter((pl.col("store") == store) & (pl.col("period") == period))
        if not spine_facts.is_empty()
        else spine_facts
    )
    spine_rows = (
        spine.filter((pl.col(SPINE_STORE) == store) & (pl.col(SPINE_PERIOD) == period)).height
        if not spine.is_empty() and {SPINE_STORE, SPINE_PERIOD} <= set(spine.columns)
        else 0
    )
    completeness = _completeness(
        model, ingestion, facts, scoped, scoped_spine, store, period, spine_rows
    )

    unavailable = {
        m.id for m in model.metrics if m.source in completeness.missing
    }
    totals = calc.totals_by_metric(
        scoped_spine if not scoped_spine.is_empty() else scoped, only_linked=False
    )
    inapplicable = {m.id for m in model.metrics if m.for_platform(platform) is None}
    nodes = calc.evaluate_statement(model, totals, unavailable, inapplicable)
    result = audit(model, scoped, link_reports, classify_report, completeness, nodes)
    return Slice(
        store=store, period=period, nodes=nodes, facts=scoped,
        completeness=completeness, audit=result,
        link_reports=link_reports, classify_report=classify_report,
    )


def _completeness(
    model: Model,
    ingestion: Ingestion,
    all_facts: pl.DataFrame,
    scoped: pl.DataFrame,
    scoped_spine: pl.DataFrame,
    store: str,
    period: str,
    spine_rows: int,
) -> Completeness:
    """完整度。责任人信息来自数据源契约，不需要额外维护。

    缺失原因必须区分"根本没传"和"传了但没有这个店的数据"，否则会对着已经上传的人
    催传，用户马上就不信这套提示了。

    判断一个数据源有没有为这个店供数，看的是脊柱事实而不是源事实：源表自己报的店铺名
    和平台店铺名常常对不上（聚水潭写"喜必顺旗舰店"，文件名是"淘宝喜必顺"），
    而钱落在谁的订单上是确定的。店铺归属由脊柱决定，就不需要再维护一张店铺别名表。
    """
    out = Completeness()
    uploaded = ingestion.uploaded_sources()
    contributing: set[str] = set()
    if not scoped_spine.is_empty():
        contributing |= set(scoped_spine.get_column("source_id").unique().to_list())
    if not scoped.is_empty():
        contributing |= set(scoped.get_column("source_id").unique().to_list())

    for source in model.sources:
        if not source.required_for_close:
            continue
        if source.id in contributing:
            out.arrived.append(source.id)
            continue
        # 脊柱源不产出金额事实，它交付的是订单骨架本身，所以在事实里永远找不到它。
        # 拿事实判断会一直报「订单明细没交」——而正是它撑起了整张损益表。
        # 有脊柱行就说明它到了。
        if source.is_spine and spine_rows > 0:
            out.arrived.append(source.id)
            continue
        out.missing.append(source.id)
        if source.id not in uploaded:
            out.reasons[source.id] = "还没传"
        elif _source_has_period(all_facts, source.id, period):
            out.reasons[source.id] = "传了，但里面没有这个店的数据"
        else:
            out.reasons[source.id] = f"传了，但里面没有 {period} 的数据"
    return out


def _source_has_period(facts: pl.DataFrame, source_id: str, period: str) -> bool:
    if facts.is_empty():
        return False
    return not facts.filter(
        (pl.col("source_id") == source_id) & (pl.col("period") == period)
    ).is_empty()


#: 按指标取值表达式算出的每行净额。只用于报告，不参与核算。
ROW_AMOUNT = "__row_amount__"


def _with_row_amount(frame: pl.DataFrame, metric: Metric) -> pl.DataFrame:
    try:
        return frame.with_columns(calc.row_amount(metric.value, frame).alias(ROW_AMOUNT))
    except Exception:
        # 算不出来不该拖垮归类，报告里那一栏显示 0 就是了。
        return frame.with_columns(pl.lit(0.0).alias(ROW_AMOUNT))


def _merge_link(store: dict[str, LinkReport], metric_id: str, report: LinkReport) -> None:
    existing = store.get(metric_id)
    if existing is None:
        store[metric_id] = report
        return
    existing.total_rows += report.total_rows
    existing.linked_rows += report.linked_rows
    existing.naturally_unlinked_rows += report.naturally_unlinked_rows
    existing.extract_failed_rows += report.extract_failed_rows
    existing.unlinked_amount += report.unlinked_amount
    existing.spine_keys = max(existing.spine_keys, report.spine_keys)
    existing.spine_keys_total = max(existing.spine_keys_total, report.spine_keys_total)
    existing.expect_label = existing.expect_label or report.expect_label
    existing.covered_keys |= report.covered_keys


def _first_hint(frame: pl.DataFrame, column: str) -> str | None:
    if column not in frame.columns or frame.is_empty():
        return None
    value = frame.get_column(column).drop_nulls()
    return str(value[0]) if len(value) else None


# --------------------------------------------------------------------------- #
# 脊柱
# --------------------------------------------------------------------------- #


def _build_spine(ingestion: Ingestion, notes: list[str]) -> Spine:
    """从声明为脊柱的数据源构建订单脊柱。"""
    model = ingestion.model
    spine_sources = [s for s in model.sources if s.is_spine]
    if not spine_sources:
        notes.append("模型没有声明订单脊柱，所有数据只能做期间级核算")
        return Spine.empty()

    parts: list[pl.DataFrame] = []
    for source in spine_sources:
        for item in ingestion.frames_of(source.id):
            assert item.frame is not None and item.template is not None
            parts.append(_spine_frame(item.frame, item.template))

    if not parts:
        names = "、".join(s.name for s in spine_sources)
        notes.append(f"订单数据（{names}）没有到，其他数据没有可挂的订单")
        return Spine.empty()

    frame = pl.concat(parts, how="diagonal_relaxed")
    return Spine(frame=frame)


def _spine_frame(frame: pl.DataFrame, template: Template) -> pl.DataFrame:
    """把脊柱数据整理成 关联键角色 + 店铺 + 账期 + 商品。"""
    slot = next(iter(template.time_slots), "order_date")
    period = (
        pl.col(str(slot)).dt.strftime("%Y-%m")
        if str(slot) in frame.columns
        else pl.lit(None, dtype=pl.Utf8)
    )
    store = pl.col(ROLE_STORE).cast(pl.Utf8) if ROLE_STORE in frame.columns else pl.lit(None, dtype=pl.Utf8)
    product = pl.col(ROLE_PRODUCT).cast(pl.Utf8) if ROLE_PRODUCT in frame.columns else pl.lit(None, dtype=pl.Utf8)

    # 保留脊柱模板绑定的所有角色，不只是订单号类。分摊比例、商品ID、实付金额都要留：
    # 分摊除数来自脊柱，下钻要展示的字段也来自脊柱。脊柱只有两万行量级，留全了不贵。
    keep = [
        b.role for b in template.bindings
        if b.role in frame.columns and b.role not in (ROLE_STORE, ROLE_PRODUCT)
    ]
    numeric = {"alloc_ratio", "buyer_paid", "refund_amount", "quantity"}
    return frame.select(
        *[
            (pl.col(r) if r in numeric else pl.col(r).cast(pl.Utf8)).alias(r)
            for r in keep
        ],
        pl.coalesce(store, pl.col("__hint_store__") if "__hint_store__" in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias(SPINE_STORE),
        pl.coalesce(period, pl.col("__hint_period__") if "__hint_period__" in frame.columns else pl.lit(None, dtype=pl.Utf8)).alias(SPINE_PERIOD),
        product.alias(SPINE_PRODUCT),
    )
