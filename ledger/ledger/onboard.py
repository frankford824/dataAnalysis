"""接表向导：从「没见过这张表」到「这张表能进账」。

四步，每一步都得能反悔：

  一、提议。看表头和采样值，草拟列到角色的映射（`model.propose`）。
  二、试跑。**用草案真解析一遍这个文件**，把出来的行数、每列的填充率、
      文件自带的控制合计核对结果摆给人看。
  三、确认。人改映射，改完再试跑。
  四、落库。写进 templates.yaml，重算受影响的店。

第二步是这个模块存在的主要理由。只看列名点确认，人确认的是一份纸面映射：
列名对上了不代表值取得对——表头在第 3 行而不是第 2 行、金额列里混着「-」、
合计行没被丢掉，这些全都在纸面上看不见，但会实打实地让金额错掉。试跑的成本是
几秒钟，漏掉它的成本是一个错的账期被结出去。

所以试跑不是「预览一下更放心」，它是这一步的验收标准：解析不出行数、或者控制合计
对不上，就不该让人往下走。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .engine.normalize import NormalizeError, normalize
from .engine.parse import ParseError, parse
from .engine.recognize import match_headers
from .engine.types import RawTable
from .model.config import add_template
from .model.propose import Draft, propose
from .model.propose import spine_roles as propose_spine_roles
from .model.loader import ModelError, load_model
from .model.schema import Model, ParseOptions, SourceContract, Template
from .workspace import Workspace

#: 试跑采样多少行给人看。看几行是为了确认「取出来的确实是这个意思」，不是为了看全。
_PREVIEW_ROWS = 8


# --------------------------------------------------------------------------- #
# 拿到那张没认出来的表
# --------------------------------------------------------------------------- #


def locate(ws: Workspace, sha: str, sheet: str = "", parse_opts: ParseOptions | None = None) -> RawTable:
    """按内容哈希把留档的文件捞出来解析。

    向导全程按 sha 找文件而不是按文件名：文件名会被店长改，内容哈希不会。
    """
    path = ws.materialize(sha)
    if path is None:
        raise ModelError(f"留档里没有这份文件（{sha[:12]}）。重新上传一次就有了。")
    try:
        tables = parse(path, parse_opts)
    except ParseError as exc:
        raise ModelError(str(exc)) from exc
    if not tables:
        raise ModelError(f"{path.name} 解析不出任何表")
    if sheet:
        for t in tables:
            if (t.ref.sheet or "") == sheet:
                return t
        raise ModelError(f"{path.name} 里没有 {sheet} 这张工作表")
    return tables[0]


def draft_for(
    ws: Workspace,
    model: Model,
    sha: str,
    *,
    sheet: str = "",
    header_row: int | None = None,
    source_hint: str = "",
) -> tuple[Draft, RawTable]:
    """给留档里的某张表出一份草案。

    `header_row` 让人能纠正表头位置。这一项必须能改：表头在第几行是所有解析参数里
    最容易错、错了之后表现最离谱的一个——猜错一行，整张表的「表头」会是一行数据，
    于是每一列都认不出来，而报出来的现象只是「没见过这种表头」。
    """
    opts = ParseOptions(header_row=header_row) if header_row is not None else None
    table = locate(ws, sha, sheet, opts)
    seen = match_headers(table.headers, model, table.ref)
    d = propose(
        table.headers,
        [r.cells for r in table.rows],
        model,
        near_misses=seen.near_misses,
        source_hint=source_hint,
        parse=opts or ParseOptions(),
    )
    if seen.known:
        d.warnings.insert(0, f"这张表现在已经能被「{seen.template_id}」认出来了，不用再接。")
    return d, table


# --------------------------------------------------------------------------- #
# 试跑
# --------------------------------------------------------------------------- #


@dataclass
class RoleCheck:
    """试跑后一个角色的实际情况。"""

    role: str
    column: str
    #: 有值的行占比。0 说明这列取出来全是空——列名对上了但取的是别的位置。
    filled: float = 0.0
    samples: tuple[str, ...] = ()
    #: 数值列的合计。人对着源文件一眼就能认出对不对。
    total: float | None = None


@dataclass
class DryRun:
    """一次试跑的结果。这就是人点「落库」之前看到的全部依据。"""

    ok: bool = False
    #: 拦住落库的硬问题。
    errors: list[str] = field(default_factory=list)
    #: 不拦，但得看一眼。
    warnings: list[str] = field(default_factory=list)
    rows: int = 0
    roles: list[RoleCheck] = field(default_factory=list)
    #: 文件自带的控制合计核对结果。
    controls: list[dict[str, Any]] = field(default_factory=list)
    #: 前几行取出来长什么样。
    preview: list[dict[str, str]] = field(default_factory=list)
    #: 这张表接上之后，哪些指标能从它取数。空的话得说清「不会进损益表」。
    metrics: list[str] = field(default_factory=list)
    #: 以后靠哪几列认出这张表。必须让人看见：签名是按确认后的映射算的，
    #: 而它决定了下个月的表会不会被认成这个模板。
    match_columns: list[str] = field(default_factory=list)
    #: 合计行标记。选错这一项会静默丢掉大批数据行，所以也要摆出来。
    total_row_marker: str = ""

    def summary(self) -> str:
        if not self.ok:
            return "试跑没过：" + "；".join(self.errors[:2])
        parts = [f"解析出 {self.rows:,} 行"]
        if blank := [r.role for r in self.roles if r.filled == 0.0]:
            parts.append(f"{len(blank)} 个角色一行值都没取到")
        if self.metrics:
            parts.append(f"{len(self.metrics)} 个指标能从它取数")
        else:
            parts.append("暂时没有指标从它取数")
        return "，".join(parts)


def dry_run(table: RawTable, template: Template, model: Model) -> DryRun:
    """用草案模板真解析一遍。

    只在内存里跑，不写任何东西。这一步的产出是证据，不是结果。
    """
    out = DryRun()

    # 先确认这套签名不会和现有模板打架。落库之后才发现两个模板抢同一张表，
    # 表现是「有时候认成这个有时候认成那个」，极难查。
    out.errors.extend(_signature_clashes(table, template, model))

    try:
        frame, notes = normalize(table, template)
    except NormalizeError as exc:
        out.errors.append(str(exc))
        return out
    out.warnings.extend(notes)

    out.rows = frame.height
    if not out.rows:
        out.errors.append(
            "一行都没解析出来。多半是表头行不对：表头位置差一行，"
            "整张表的第一行数据会被当成表头，于是每列都认不出来。"
        )
        return out

    columns = {b.role: b.columns[0] for b in template.bindings}
    for role, column in columns.items():
        out.roles.append(_check_role(frame, role, column))

    if blank := [r.role for r in out.roles if r.filled == 0.0]:
        out.errors.append(
            "这些角色一行值都没取到：" + "、".join(blank)
            + "。列名匹配上了却取不到值，通常是取错了列位置（同名列有多个）"
              "或者整列本来就是空的。带着这个落库，靠它算的钱会静默变成 0。"
        )

    _check_total_row(table, template, out)
    _check_drop_rate(table, template, out)
    _check_spine(template, model, out)
    out.controls = _check_controls(table, frame, out)
    out.preview = _preview(frame, list(columns))
    out.metrics = [m.name or m.id for m in model.metrics_of(template.source)] \
        if any(s.id == template.source for s in model.sources) else []
    out.match_columns = list(template.match_columns)
    out.total_row_marker = template.total_row_marker or ""
    out.ok = not out.errors
    return out


def _signature_clashes(table: RawTable, template: Template, model: Model) -> list[str]:
    """这套签名会不会和现有模板抢同一张表。

    双向都要查：现有模板会不会也命中这张表，以及新模板会不会反过来命中别人的表。
    只查一个方向的话，新表接上就把老表抢走了，老账在下一次重算时才悄悄变。
    """
    out: list[str] = []
    candidate = model.model_copy(update={"templates": (*model.templates, template)})
    seen = match_headers(table.headers, candidate, table.ref)
    if seen.template_id and seen.template_id != template.id:
        out.append(
            f"这张表会被「{seen.template_id}」抢走，新模板不会生效。"
            f"要么直接用那个模板，要么给它加上排除列。"
        )

    from .model.propose import known_columns  # 局部导入：避免建模层和向导层循环依赖

    need = {c for c in template.match_columns}
    for other in model.templates:
        if need <= known_columns(model, other.id):
            out.append(
                f"新模板的识别签名是「{other.id}」认得的列的子集，"
                f"以后那张表会被新模板抢走。签名里得带上能区分两者的列。"
            )
    return out


def _check_role(frame: pl.DataFrame, role: str, column: str) -> RoleCheck:
    if role not in frame.columns:
        return RoleCheck(role=role, column=column)
    col = frame[role]
    filled = 0.0 if frame.height == 0 else (frame.height - col.null_count()) / frame.height
    samples = tuple(
        str(v) for v in col.drop_nulls().head(4).to_list()
    )
    total: float | None = None
    if col.dtype.is_numeric():
        total = float(col.sum() or 0.0)
    return RoleCheck(role=role, column=column, filled=filled, samples=samples, total=total)


def _check_spine(template: Template, model: Model, out: DryRun) -> None:
    """脊柱表要提供的角色齐不齐。

    这一项是硬拦。脊柱少一个 `alloc_ratio`，引擎跑到分摊那一步直接抛异常，
    而异常说的是「脊柱上没有这一列」，跟人刚在向导里做的事看不出关系。
    宁可在这里拦住，说清是哪个指标要用它。
    """
    try:
        source = model.source(template.source)
    except KeyError:
        return
    if not source.is_spine:
        return
    mapped = {b.role for b in template.bindings}
    for role, (level, who) in propose_spine_roles(model).items():
        if role in mapped:
            continue
        if level == "hard":
            out.errors.append(f"脊柱表必须有 {role}：{who}。缺了引擎算到那一步会直接报错。")
        else:
            out.warnings.append(f"脊柱少了 {role}：{who}。不报错，但口径会悄悄放宽。")


def _check_total_row(table: RawTable, template: Template, out: DryRun) -> None:
    """表底那行合计有没有被丢掉。

    判据不靠猜：合计行如果混进了数据，这一列的总和刚好是真实总和的两倍，
    于是合计行自己的值等于总和的一半。反过来，一行数据的值刚好等于全列一半，
    在几千行的表里几乎不可能是巧合——两列以上同时满足就更不可能。

    这一项必须查，因为它是纸面上完全看不出来的错：列名全对、填充率 100%、
    行数也只多一行，而每一列金额都翻了倍。
    """
    if template.total_row_marker:
        return
    # 重新按不丢合计行的方式归一，才能看到那一行。
    naive = template.model_copy(update={"total_row_marker": None})
    try:
        frame, _ = normalize(table, naive)
    except NormalizeError:
        return
    numeric = [c for c in frame.columns if frame[c].dtype.is_numeric()]
    if frame.height < 5 or not numeric:
        return

    votes: dict[int, list[str]] = {}
    for col in numeric:
        series = frame[col]
        total = series.sum()
        if total is None or abs(total) < 0.01:
            continue
        half = total / 2
        tol = max(0.01, abs(total) * 1e-9)
        for i, v in enumerate(series.to_list()):
            if v is not None and abs(v - half) <= tol:
                votes.setdefault(i, []).append(col)
    if not votes:
        return

    # 两列同时「正好等于全列一半」已经不可能是巧合。只有一列的时候补一条旁证：
    # 那一行的键是空的——合计行没有订单号，这也正是丢它的依据。
    keys = [
        b.role for b in template.bindings
        if b.role in frame.columns and not frame[b.role].dtype.is_numeric()
    ]
    for row, cols in sorted(votes.items(), key=lambda kv: -len(kv[1])):
        empty = [k for k in keys if frame[k][row] is None]
        if len(cols) < 2 and not empty:
            continue
        out.errors.append(
            f"第 {row + 1} 行看着是合计行："
            + "、".join(cols[:4])
            + f" {'这几列' if len(cols) > 1 else '这列'}的值刚好等于全列合计的一半"
            + (f"，而且 {empty[0]} 是空的" if empty else "")
            + "。不把它丢掉，每一列金额都会翻倍。"
            + (
                f"把「合计行标记」设成 {empty[0]} 就会丢掉它。"
                if empty else "得指定一个在合计行上为空的角色当合计行标记。"
            )
        )
        return


#: 合计行标记最多能丢掉几成行。合计行按定义是零星几行，丢掉一成以上就不是合计行。
_DROP_CEILING = 0.10


def _check_drop_rate(table: RawTable, template: Template, out: DryRun) -> None:
    """合计行标记有没有顺手把数据行一起丢了。

    合计行标记的判据是「这个角色为空」。它选错列的代价极不对称：选到一列几乎全空的，
    那就是「几乎全部行都是合计行」，整张表被丢到只剩零头。而界面上看不出异常——
    填充率是按留下来的行算的，留下来的那几行当然 100% 填充。

    实测过一次：两列同名「推广主体ID」都被映成 product_id，取到了空的那一列，
    8226 行丢掉只剩 397 行，金额只剩零头，试跑一路绿灯。
    """
    if not (marker := template.total_row_marker):
        return
    naive = template.model_copy(update={"total_row_marker": None})
    try:
        before, _ = normalize(table, naive)
        after, _ = normalize(table, template)
    except NormalizeError:
        return
    dropped = before.height - after.height
    if before.height == 0 or dropped <= 1:
        return
    share = dropped / before.height
    if share <= _DROP_CEILING:
        return
    out.errors.append(
        f"合计行标记 {marker} 丢掉了 {dropped:,} 行，占全表 {share:.0%}，"
        f"只剩 {after.height:,} 行。合计行只该是零星几行，"
        f"丢掉这么多说明 {marker} 取到的那一列本来就大片是空的——"
        f"多半是列选错了（比如重名列取到了空的那一个）。"
        f"照这样落库，账里只会剩零头，而且不报错。"
    )


def _check_controls(table: RawTable, frame: pl.DataFrame, out: DryRun) -> list[dict[str, Any]]:
    """拿文件自己声明的合计核对解析结果。

    这是免费的正确性证据：支付宝账务明细尾部写着「#支出合计：75171笔，共-540182.61元」，
    那是文件自己给出的正确答案。对不上就是漏读或多读，不用等人去核。
    """
    rows = []
    for c in table.controls:
        item: dict[str, Any] = {"label": c.label, "raw": c.raw, "ok": True, "why": ""}
        if c.count is not None:
            item["said"] = c.count
            item["got"] = frame.height
            if c.count != frame.height:
                item["ok"] = False
                item["why"] = f"文件说 {c.count:,} 笔，解析出 {frame.height:,} 笔"
                out.errors.append(f"控制合计对不上：{item['why']}（{c.label}）")
        rows.append(item)
    return rows


def _preview(frame: pl.DataFrame, roles: list[str]) -> list[dict[str, str]]:
    cols = [r for r in roles if r in frame.columns]
    if not cols:
        return []
    head = frame.select(cols).head(_PREVIEW_ROWS)
    return [
        {k: ("" if v is None else str(v)) for k, v in row.items()}
        for row in head.iter_rows(named=True)
    ]


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


@dataclass
class Landed:
    """接表落库的结果。"""

    template_id: str
    source_id: str
    #: 重算过的店。
    stores: list[str] = field(default_factory=list)
    periods: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


def land(
    model_dir: str | Path,
    ws: Workspace,
    template: Template,
    *,
    source: SourceContract | None = None,
    by: str = "",
    recompute_stores: bool = True,
) -> Landed:
    """把确认过的模板写进模型，然后把用得上它的店重算一遍。

    重算是必须的，不是顺手做的：接一张表的目的就是让它进账。写完模板不重算，
    人会以为接完了，而界面上的数字一分钱都没变。
    """
    from . import service  # 局部导入：service 依赖 workspace，向导又被 service 之外的地方用

    # 退路是按字节还原，不是反过来再改一遍文件。删一条记录得重写整个文档，而重写
    # 会顺手把嵌套缩进全改掉——退回去的文件跟原来那份差 600 行。存下原样最省事，
    # 也最可信：退回去的就是原来那个文件，一个字节都不差。
    before = _snapshot(Path(model_dir))
    saved = add_template(model_dir, template, source=source, by=by)
    model = load_model(model_dir)
    out = Landed(template_id=saved.id, source_id=saved.source)

    if not recompute_stores:
        return out

    # 写模板只保证「模型能加载」，不保证「引擎能算完」——两者差得很远：
    # 脊柱少一列分摊比例，模型校验一路绿灯，引擎跑到分摊那一步才抛异常。
    # 真让这种模板留在模型里，整个系统从此算不出账，而现场没人知道是刚接的表干的。
    # 所以算不出就退回去，宁可这次接不上。
    try:
        for store in model.active_stores():
            if not ws.active_files(store.id):
                continue
            report = service.recompute(ws, model, store)
            out.stores.append(store.id)
            out.periods.extend(report.periods)
    except Exception as exc:
        _restore(before)
        raise ModelError(
            f"模板写进去之后算不出账，已经退回，模型还是原来那份。原因：{exc}"
        ) from exc
    return out


#: 落库会动到的文件。只存这两份，别整目录快照——整目录会把人在这期间手改的
#: 别的文件也一起回退掉。
_TOUCHED = ("templates.yaml", "sources.yaml")


def _snapshot(root: Path) -> dict[Path, bytes | None]:
    return {
        root / name: (root / name).read_bytes() if (root / name).exists() else None
        for name in _TOUCHED
    }


def _restore(before: dict[Path, bytes | None]) -> None:
    for path, raw in before.items():
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)


__all__ = [
    "DryRun",
    "Landed",
    "RoleCheck",
    "draft_for",
    "dry_run",
    "land",
    "locate",
]
