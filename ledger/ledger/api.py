"""HTTP 接口。

四组端点，对应界面上四件事：交表、看账、查数、配置。

上传时必须保留原始文件名。店铺归属、数据源识别都靠文件名——交上来的文件名形如
「聚水潭成本-淘宝喜必顺.xlsx」，破折号前是类别、后面是店铺。换成随机名存盘，
这两件事立刻全瞎。

接口一律返回中文名和人话消息。前端不该拿着 `order_detail` 这种 id 去猜中文，
更不该自己拼错误话术——同一件事在终端、界面、接口里必须是同一句话。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import onboard, service, view
from .model import propose
from .model.config import EDITABLE, add_store, update_store
from .model.loader import ModelError, load_model
from .model.schema import Model, SourceContract, Store, Template
from .model.transaction import model_revision
from .money import decimal_amount, money_float
from .security import SecurityError, authenticate, authorize
from .web import STATIC, page
from .workspace import Workspace, WorkspaceError, default_root

app = FastAPI(title="记账", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def api_security(request: Request, call_next):
    if request.url.path.startswith("/api"):
        try:
            principal = authenticate(
                request.client.host if request.client else "",
                request.headers.get("authorization", ""),
            )
            authorize(principal, request.method, request.url.path)
            request.state.principal = principal
        except SecurityError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers=headers)
    return await call_next(request)

#: 仓库自带的模型。
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"

#: 工作区。测试里换成临时目录。
WORKSPACE_ROOT: Path | None = None

_ws: Workspace | None = None


def _model() -> Model:
    try:
        return load_model(DEFAULT_MODEL)
    except ModelError as exc:
        raise HTTPException(500, f"模型有问题：{exc}") from exc


def workspace() -> Workspace:
    """进程内共用一个工作区。sqlite 开了 WAL，读写并发没问题。"""
    global _ws
    root = WORKSPACE_ROOT or default_root()
    if _ws is None or _ws.root != Path(root):
        if _ws is not None:
            _ws.close()
        _ws = Workspace(Path(root))
    return _ws


def _store(model: Model, store_id: str) -> Store:
    store = next((s for s in model.stores if s.id == store_id), None)
    if store is None:
        raise HTTPException(404, f"没有登记过 {store_id} 这家店")
    return store


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """入口页一律不缓存。

    页面里的资源链接带版本号，可以放心长缓存；但入口页自己被缓存住的话，改完前端
    部署上去，浏览器还拿着旧 HTML 去加载旧版本的脚本，版本号就白带了。
    """
    return HTMLResponse(page(), headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# 启动信息
# --------------------------------------------------------------------------- #


@app.get("/api/bootstrap")
def bootstrap(request: Request) -> dict:
    """界面启动拉一次就够。店铺、平台、可改字段、报表骨架都在里面。

    合成一个端点而不是让前端连打四枪，是因为这四样东西必须来自同一次模型加载：
    分开取的话，中间有人改了配置，界面会拿着半新半旧的结构去渲染。
    """
    model = _model()
    return {
        "stores": [view.store_dict(s) for s in model.stores],
        "platforms": view.platform_options(model),
        "editable": list(EDITABLE),
        "statement": [
            {"id": n.id, "name": n.name, "level": n.level, "display": n.display,
             "is_total": n.is_total, "headline": n.headline}
            for n in view.statement_order(model)
        ],
        "sources": [{"id": s.id, "name": s.name} for s in model.sources],
        "accepts": sorted(service.SUFFIXES),
        "model_revision": model_revision(DEFAULT_MODEL),
        "principal": {
            "name": request.state.principal.name,
            "role": request.state.principal.role,
        },
    }


# --------------------------------------------------------------------------- #
# 交表
# --------------------------------------------------------------------------- #


@app.post("/api/upload")
async def upload(
    request: Request,
    files: Annotated[list[UploadFile], File()],
) -> dict:
    """收一批表，留档，把受影响的店重算。

    重算整家店而不是这一批文件：损益要靠订单明细做脊柱，单独一张运费表算不出账。
    留档的价值就在这儿——上周交的订单明细还在，这周补张运费表就能出完整结果。
    """
    model = _model()
    ws = workspace()
    uploads = [(Path(f.filename or "").name, f.file) for f in files if f.filename]
    if not uploads:
        raise HTTPException(400, "没有文件")
    result = service.intake(ws, model, uploads, by=request.state.principal.name)
    return {
        "summary": result.summary(),
        "kept": [
            {"file": k.name, "store_id": k.store_id,
             "unchanged": k.unchanged, "replaced": bool(k.replaced)}
            for k in result.kept
        ],
        "rejected": [
            {"file": r.file, "why": r.why, "suggest": r.suggest} for r in result.rejected
        ],
        "periods": result.periods,
        "failures": result.failures,
        "unknown_tables": result.unknown_tables,
    }


@app.delete("/api/stores/{store_id}/files")
def drop_file(store_id: str, name: str) -> dict:
    """把一份表撤下来，不再参与计算。内容留档不删，之后还能查。"""
    model = _model()
    store = _store(model, store_id)
    ws = workspace()
    ws.forget(store_id, name)
    report = service.recompute(ws, model, store)
    return {"periods": report.periods, "failure": report.failure}


# --------------------------------------------------------------------------- #
# 看账
# --------------------------------------------------------------------------- #


@app.get("/api/overview")
def overview() -> dict:
    """总览：所有店 × 所有账期。首页就是这张矩阵。"""
    model = _model()
    ws = workspace()
    by_id = {s.id: s for s in model.stores}
    headline = {n.headline: n.id for n in model.statement if n.headline}
    cells = []
    for st in ws.overview():
        store = by_id.get(st.store_id)
        payload = st.result or {}
        cells.append({
            "store_id": st.store_id,
            "store": store.name if store else st.store_id,
            "platform": store.platform if store else "",
            "entity": store.entity if store else "",
            "period": st.period,
            "state": st.state,
            "stale": st.stale,
            "at": st.at,
            "run_id": st.run_id,
            "can_close": bool(payload.get("can_close")),
            "revenue": _node(payload, headline.get("revenue")),
            "profit": _node(payload, headline.get("profit")),
            "margin": _node(payload, headline.get("margin")),
            "missing": payload.get("missing_sources") or [],
            "blocking": [
                f["message"] for f in payload.get("findings", [])
                if f.get("blocking") and not f.get("passed")
            ],
        })
    periods = sorted({c["period"] for c in cells}, reverse=True)
    return {
        "cells": cells,
        "periods": periods,
        "stores": [
            view.store_dict(s) for s in model.stores
            if not s.archived or any(c["store_id"] == s.id for c in cells)
        ],
        "totals": _totals(cells),
    }


def _node(payload: dict, wanted: str | None) -> float | None:
    """从快照里挑一个报表节点的数。哪个节点由模型的 headline 标记决定。

    总览一个格子只要三个数，为此把整份损益表传给前端再筛，等于每格多背十几行。
    十几家店三个月就是几百个格子。
    """
    if not wanted:
        return None
    for row in payload.get("statement", []):
        if row.get("id") == wanted and row.get("available"):
            return row.get("value")
    return None


def _totals(cells: list[dict]) -> list[dict]:
    """按账期汇总。老板要看的是「这个月全公司挣了多少」。

    只加已经算出数的店。缺数据的店按 0 加进去，汇总数会假装完整，那比不出数更糟。
    """
    out: dict[str, dict] = {}
    for c in cells:
        t = out.setdefault(c["period"], {
            "period": c["period"], "stores": 0, "closed": 0,
            "revenue": 0.0, "profit": 0.0, "incomplete": 0,
        })
        t["stores"] += 1
        t["closed"] += 1 if c["state"] == "closed" else 0
        if c["revenue"] is None or c["profit"] is None:
            t["incomplete"] += 1
            continue
        t["revenue"] = money_float(decimal_amount(t["revenue"]) + decimal_amount(c["revenue"]))
        t["profit"] = money_float(decimal_amount(t["profit"]) + decimal_amount(c["profit"]))
    return sorted(out.values(), key=lambda d: d["period"], reverse=True)


@app.get("/api/stores/{store_id}")
def store_detail(store_id: str) -> dict:
    """一家店的全部：账期清单 + 交了哪些表。"""
    model = _model()
    store = _store(model, store_id)
    ws = workspace()
    periods = [
        {
            "period": st.period, "state": st.state, "stale": st.stale,
            "at": st.at, "run_id": st.run_id, "by": st.by, "note": st.note,
            "can_close": bool((st.result or {}).get("can_close")),
        }
        for st in ws.overview() if st.store_id == store_id
    ]
    return {
        "store": view.store_dict(store),
        "periods": periods,
        "files": ws.submissions(store_id),
    }


@app.get("/api/stores/{store_id}/periods/{period}")
def period_detail(store_id: str, period: str) -> dict:
    """一个账期的完整快照。单店页面渲染这个。"""
    model = _model()
    _store(model, store_id)

    st = workspace().state(store_id, period)
    if st is None or st.result is None:
        raise HTTPException(404, f"{period} 还没算过账")
    return {
        "state": st.state, "stale": st.stale, "at": st.at, "run_id": st.run_id,
        "by": st.by, "note": st.note, "engine": st.engine,
        "history": workspace().history(store_id, period),
        **view.reorder_statement(st.result, model),
    }


@app.post("/api/stores/{store_id}/recompute")
def recompute(store_id: str) -> dict:
    """重算一家店。模型改了、字典补了之后用。"""
    model = _model()
    store = _store(model, store_id)
    report = service.recompute(workspace(), model, store)
    return {
        "periods": report.periods,
        "failure": report.failure,
        "unknown_tables": report.unknown_tables,
    }


class PeriodAction(BaseModel):
    note: str = ""


@app.post("/api/stores/{store_id}/periods/{period}/close")
def close_period(store_id: str, period: str, action: PeriodAction, request: Request) -> dict:
    """结账。自检层不放行就结不了，这是整套东西存在的意义。"""
    _store(_model(), store_id)
    try:
        st = workspace().close_period(
            store_id, period, by=request.state.principal.name, note=action.note,
        )
    except WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": st.state, "at": st.at, "run_id": st.run_id}


@app.post("/api/stores/{store_id}/periods/{period}/reopen")
def reopen_period(store_id: str, period: str, action: PeriodAction, request: Request) -> dict:
    """反结账。谁反的、为什么反，必须留痕。"""
    _store(_model(), store_id)
    try:
        st = workspace().reopen_period(
            store_id, period, by=request.state.principal.name, note=action.note,
        )
    except WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": st.state, "note": st.note}


# --------------------------------------------------------------------------- #
# 查数
# --------------------------------------------------------------------------- #


@app.get("/api/runs/{run_id}/drill/{node_id}")
def drill(run_id: int, node_id: str, limit: int = view.DRILL_LIMIT) -> dict:
    """一个报表数字是怎么来的：按科目、按文件、以及带行号的原始明细。

    只报总数不给行号的话，对不上账时没人查得动，整套系统就退化成又一个看不懂的报表。
    """
    facts = service.facts_of(workspace(), run_id)
    if facts is None:
        raise HTTPException(404, "这次算账没留明细，重算一次就有了")
    return view.drill(facts, _model(), node_id, limit=min(limit, 2000))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@app.get("/api/stores")
def stores() -> dict:
    model = _model()
    return {
        "stores": [view.store_dict(s) for s in model.stores],
        "editable": list(EDITABLE),
        "platforms": view.platform_options(model),
    }


class StorePatch(BaseModel):
    """能改的就这几项。id 和 name 不在里面——见 config.EDITABLE 的说明。"""

    entity: str | None = None
    entity_tax_id: str | None = None
    archived: bool | None = None
    aliases: list[str] | None = None
    note: str | None = None


@app.patch("/api/stores/{store_id}")
def patch_store(store_id: str, patch: StorePatch) -> dict:
    """改一家店的配置，写回 stores.yaml。

    法人主体这类东西数据里读不出来（支付宝和微信账单不带主体信息），只能靠人配。
    要人去改 YAML 才能配，那就不是产品。
    """
    changes: dict[str, Any] = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "没有要改的字段")
    try:
        store = update_store(DEFAULT_MODEL, store_id, changes)
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"store": view.store_dict(store)}


class StoreNew(BaseModel):
    id: str
    name: str
    platform: str
    entity: str = ""
    entity_tax_id: str = ""
    aliases: list[str] = []
    note: str = ""


@app.post("/api/stores")
def create_store(new: StoreNew) -> dict:
    """登记一家新店。开新店、接新平台都走这里，不用改代码也不用改文件。"""
    try:
        store = add_store(
            DEFAULT_MODEL, Store(**{**new.model_dump(), "aliases": tuple(new.aliases)})
        )
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"store": view.store_dict(store)}


# --------------------------------------------------------------------------- #
# 接新表
# --------------------------------------------------------------------------- #


@app.get("/api/onboard/{sha}")
def onboard_draft(sha: str, sheet: str = "", header_row: int | None = None, source: str = "") -> dict:
    """给一张没认出来的表出一份映射草案。

    `header_row` 让人能纠正表头位置。这一项必须能改：表头在第几行是所有解析参数里
    最容易错、错了之后表现最离谱的一个——猜错一行，第一行数据会被当成表头，
    于是每列都认不出来，而报出来的现象只是「没见过这种表头」。
    """
    model = _model()
    try:
        draft, table = onboard.draft_for(
            workspace(), model, sha, sheet=sheet, header_row=header_row, source_hint=source,
        )
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**view.draft_dict(draft, table, model), "model_revision": model_revision(DEFAULT_MODEL)}


@app.get("/api/onboard/{sha}/assist")
def onboard_assist(sha: str, sheet: str = "", header_row: int | None = None, source: str = "") -> dict:
    """再出一份草案，这次带上模型的意见。

    单独一个端点、界面上是第二次请求，不是把模型塞进上面那个。理由有两条，
    都不是性能：

    一、人先看到的必须是确定性那份。规则草案零点几秒出屏，模型要等几秒；合在一起
        的话整个向导都得等模型，而模型是可以关掉、可以超时的东西——不该由它决定
        向导打不打得开。

    二、屏幕上要能看出「模型动了哪里」。先渲染规则那份，模型的意见再叠上去标注，
        人看到的是一次可对照的变化，而不是一份分不清谁提的混合结果。
    """
    model = _model()
    try:
        draft, table = onboard.draft_for(
            workspace(), model, sha, sheet=sheet, header_row=header_row, source_hint=source,
        )
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    assisted = onboard.advise(draft, model)
    return {
        **view.draft_dict(draft, table, model),
        "assist": view.assist_dict(assisted),
        "model_revision": model_revision(DEFAULT_MODEL),
    }


class OnboardCommit(BaseModel):
    """人确认之后提交的那份映射。

    `roles` 是列序号到角色，空角色表示这列不要。以草案为默认值，但落库的是这一份——
    草案只是提议，不能当结论。

    键是列序号而不是列名：列名会重复（有的表两列都叫「推广主体ID」），
    用列名当键，界面上就没法把两列分别设成不同角色。JSON 的对象键只能是字符串，
    所以这里收字符串，落库前转成整数。
    """

    sha: str
    sheet: str = ""
    header_row: int | None = None
    template_id: str
    name: str = ""
    source: str
    roles: dict[int, str]
    match_columns: list[str] = []
    time_slots: dict[str, str] = {}
    total_row_marker: str | None = None
    #: 顺带登记一个新数据源。已有数据源就不给。
    new_source: dict[str, Any] | None = None
    model_revision: str


def _build(commit: OnboardCommit, model: Model) -> tuple[Template, Any]:
    """按提交的映射拼出模板对象和要解析的那张表。"""
    draft, table = onboard.draft_for(
        workspace(), model, commit.sha, sheet=commit.sheet, header_row=commit.header_row,
        source_hint=commit.source,
    )
    if commit.time_slots:
        draft.time_slots = dict(commit.time_slots)
    if commit.total_row_marker is not None:
        draft.total_row_marker = commit.total_row_marker or None
    template = draft.template(
        commit.template_id,
        source=commit.source,
        name=commit.name,
        roles=commit.roles,
        match_columns=tuple(commit.match_columns),
    )
    return template, table


@app.post("/api/onboard/try")
def onboard_try(commit: OnboardCommit) -> dict:
    """用提交的映射真解析一遍，不写任何东西。

    这一步不是「预览一下更放心」，它是落库的验收标准。只看列名点确认，人确认的是
    一份纸面映射：表头行差一行、金额列里混着 `-`、表底那行合计没丢掉，全都在纸面上
    看不见，但会实打实地让金额错掉——合计行不丢，每一列金额刚好翻倍。
    """
    model = _model()
    try:
        template, table = _build(commit, model)
    except (ModelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return view.dryrun_dict(onboard.dry_run(table, template, model))


@app.post("/api/onboard")
def onboard_commit(commit: OnboardCommit, request: Request) -> dict:
    """确认落库：写进模型，然后把用得上它的店重算。

    先试跑一遍，没过就不写。也会在写完之后验证引擎还能算完账，算不出就退回去——
    模型能加载不等于引擎能算完，脊柱少一列分摊比例，校验一路绿灯而引擎会抛异常。
    """
    model = _model()
    try:
        template, table = _build(commit, model)
        result = onboard.dry_run(table, template, model)
        if not result.ok:
            raise HTTPException(400, "试跑没过，没有落库：" + "；".join(result.errors))
        source = SourceContract(**commit.new_source) if commit.new_source else None
        landed = onboard.land(
            DEFAULT_MODEL, workspace(), template, source=source,
            by=request.state.principal.name,
            expected_revision=commit.model_revision,
        )
    except (ModelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "template_id": landed.template_id,
        "source_id": landed.source_id,
        "stores": landed.stores,
        "periods": landed.periods,
    }


@app.get("/api/roles")
def roles(source: str = "") -> dict:
    """某个数据源用得到的字段角色，带上它在别处叫什么、供给哪些指标。

    界面上的下拉框从这里出选项。要带证据：光给一串英文角色名，人没法判断
    `base_order_id` 和 `order_id` 该选哪个。
    """
    model = _model()
    facts = propose.role_facts(model, source)
    return {
        "roles": [
            {"role": f.role, "kind": f.kind, "hint": f.hint,
             "columns": list(f.columns), "metrics": list(f.metrics)}
            for f in facts.values()
        ],
        "sources": [
            {"id": s.id, "name": s.name, "is_spine": s.is_spine,
             "metrics": [m.name or m.id for m in model.metrics_of(s.id)]}
            for s in model.sources
        ],
    }
