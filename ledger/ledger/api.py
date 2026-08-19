"""HTTP 接口。

四组端点，对应界面上四件事：交表、看账、查数、配置。

上传时必须保留原始文件名。店铺归属、数据源识别都靠文件名——交上来的文件名形如
「聚水潭成本-淘宝喜必顺.xlsx」，破折号前是类别、后面是店铺。换成随机名存盘，
这两件事立刻全瞎。

接口一律返回中文名和人话消息。前端不该拿着 `order_detail` 这种 id 去猜中文，
更不该自己拼错误话术——同一件事在终端、界面、接口里必须是同一句话。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import assist, fees as fees_mod, gaps, onboard, overhead, ownership, progress, service, view
from . import search as search_mod
from .model import propose
from .model.config import (
    COMMISSION_COLUMNS,
    COMMISSION_HEADERS,
    EDITABLE,
    csv_cell,
    add_store,
    commission_column,
    replace_commission,
    replace_fee_rules,
    update_store,
)
from .model.loader import ModelError, load_model
from .model.schema import FeeRule, Model, SourceContract, Store, Template
from .model.transaction import model_revision
from .money import decimal_amount, money_float
from .web import STATIC, page
from .workspace import Workspace, WorkspaceError, default_root

app = FastAPI(title="记账", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

#: 仓库自带的模型。
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"

#: 从网页做的操作记不到人头上。登录撤掉之后浏览器不带身份，与其编一个「本机操作员」
#: 让留痕看起来有据，不如留空——界面上空的显示成破折号，一眼看得出这条没人签字。
#: 需要签字的场合走命令行，`ledger submit --by` 还在。
ANONYMOUS = ""

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
def bootstrap() -> dict:
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
        # 提成基数的下拉选项。由模型说哪几行能选，界面不写死节点 id——换一家公司
        # 换一套损益表，这个下拉自己就跟着变。
        "commission_bases": [{"id": n.id, "name": n.name} for n in model.commission_bases()],
        "accepts": sorted(service.SUFFIXES),
        "model_revision": model_revision(DEFAULT_MODEL),
    }


# --------------------------------------------------------------------------- #
# 交表
# --------------------------------------------------------------------------- #


@app.get("/api/upload/progress/{token}")
def upload_progress(token: str) -> dict:
    """这次交表干到哪儿了。

    交表是同步请求，这个接口只是同一件事的旁白。号是客户端自己生成的：服务端发号
    就得先有一次往返，而进度要从上传的第一个字节就开始报。
    """
    return progress.read(token) or {"phase": "", "finished": True, "unknown": True}


@app.post("/api/upload")
def upload(files: Annotated[list[UploadFile], File()], token: str = "") -> dict:
    """收一批表，留档，把受影响的店重算。

    重算整家店而不是这一批文件：损益要靠订单明细做脊柱，单独一张运费表算不出账。
    留档的价值就在这儿——上周交的订单明细还在，这周补张运费表就能出完整结果。

    这个函数必须是 `def` 而不是 `async def`。`intake` 要解析几十万行、跑完整店重算，
    是实打实的阻塞活；写成 `async def` 它就跑在事件循环上，一个人交表的这几分钟里
    整台服务器不响应任何请求——别人打不开界面，自己也看不到进度。同步函数由
    starlette 丢进线程池，解析归线程、事件循环继续收请求，而 polars 的计算在 Rust
    里放掉 GIL，该吃满的核照样吃满。
    """
    model = _model()
    ws = workspace()
    uploads = [(Path(f.filename or "").name, f.file) for f in files if f.filename]
    if not uploads:
        raise HTTPException(400, "没有文件")
    progress.open(token)
    try:
        result = service.intake(
            ws, model, uploads, by=ANONYMOUS, report=progress.Reporter(token),
        )
    except Exception as exc:
        progress.close(token, "出错了")
        raise HTTPException(500, f"交表算账时出错：{exc}") from exc
    progress.close(token)
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
    # 同一家店按账期排，让每个账期都能和它前一个比——「上个月有、这个月成了 0」
    # 只能这样看出来。
    states = sorted(ws.overview(), key=lambda st: (st.store_id, st.period))
    before: dict[str, dict] = {}
    for st in states:
        store = by_id.get(st.store_id)
        payload = st.result or {}
        prev = before.get(st.store_id)
        if payload:
            before[st.store_id] = payload
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
            # 这一格有几处不对。总览摆不下清单本身，但摆得下这个数——没有它，
            # 人得逐店逐月点进去才知道哪个月要看。
            "gaps": gaps.summary(gaps.gaps(payload, model, prev)) if payload else None,
        })
    periods = sorted({c["period"] for c in cells}, reverse=True)
    return {
        "cells": cells,
        "periods": periods,
        "default_period": working_period(periods, cells),
        "stores": [
            view.store_dict(s) for s in model.stores
            if not s.archived or any(c["store_id"] == s.id for c in cells)
        ],
        "totals": _totals(cells),
    }


_YM = re.compile(r"^\d{4}-\d{2}$")


def working_period(periods: list[str], cells: list[dict]) -> str:
    """总览默认落在哪一个月。

    账期列表是新的在前。前端曾经取最后一个，最后一个经常是「(未知账期)」或者
    最早那个月（往往只有一家店刚交过表）。人选「全部账期」时又退回最新一个月，
    于是同一页会在一家店和全部店之间来回跳。

    规则：优先有 `YYYY-MM` 的月份；在这些月份里取店数最多的，并列取更新的。
    只有认不出月份的格子时，才退回那一格。
    """
    if not periods:
        return ""
    real = [p for p in periods if _YM.match(p or "")]
    pool = real or list(periods)
    counts = Counter(c.get("period") for c in cells if c.get("period") in pool)
    return max(pool, key=lambda p: (counts[p], p))


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


#: 逐月对比一次最多摆几个账期。再多横着看不过来，也不是这一页要回答的问题。
TREND_PERIODS = 12


@app.get("/api/trend")
def trend(store_id: str = "", platform: str = "", periods: int = TREND_PERIODS) -> dict:
    """损益表逐月：行是利润项，列是账期。

    总览那三个数（收入、利润、利润率）只够回答「这个月怎么样」。真正要解释的是
    「为什么这个月比上个月少了八万」，而答案永远在某一个费项里——推广涨了、退款
    多了、代发成本翻倍了。所以这里把整张损益表按月摊开，每一项都跟得到月。

    多家店合并时逐项相加。比率行不能加（三家店的利润率加起来没有意义），除了利润率
    ——它由合并后的利润除以合并后的收入现算，这个数是对的。
    """
    model = _model()
    ws = workspace()
    keep = {
        s.id for s in model.stores
        if (not store_id or s.id == store_id) and (not platform or s.platform == platform)
    }
    snaps = [
        st for st in ws.overview()
        if st.store_id in keep and st.result
    ]
    months = sorted({st.period for st in snaps}, reverse=True)[:max(periods, 1)]
    by_period: dict[str, list[dict]] = {p: [] for p in months}
    for st in snaps:
        if st.period in by_period:
            by_period[st.period].append(st.result or {})

    headline = {n.headline: n.id for n in model.statement if n.headline}
    rows = _trend_rows(model, months, by_period, headline)
    return {
        "periods": months,
        "stores": {p: len(v) for p, v in by_period.items()},
        "rows": rows,
        "scope": (
            _store(model, store_id).name if store_id
            else next((p.name for p in model.platforms if p.id == platform), "")
            or "全公司"
        ),
    }


def _trend_rows(
    model: Model,
    months: list[str],
    by_period: dict[str, list[dict]],
    headline: dict[str, str],
) -> list[dict]:
    """每个损益表节点在各个账期的数。

    有的店没有这一项（1688 没有软件服务费），有的店这个月还没算出数。两种情况都不能
    按 0 加进去——合并出来的数会显得完整，而「看起来完整的错数」比空着危险。所以
    每一格都带上它是几家店加出来的，界面上凑不齐的那格自己会说。
    """
    order = view.statement_order(model)
    out: list[dict] = []
    for node in order:
        cells: dict[str, dict] = {}
        for period in months:
            payloads = by_period.get(period, [])
            found = [
                r for pay in payloads for r in pay.get("statement", [])
                if r.get("id") == node.id and r.get("available")
            ]
            if node.display == "percent":
                cells[period] = _trend_ratio(node, payloads, headline, found)
                continue
            if not found:
                cells[period] = {"value": None, "stores": 0}
                continue
            total = decimal_amount(0)
            for r in found:
                total += decimal_amount(r.get("value") or 0)
            cells[period] = {"value": money_float(total), "stores": len(found)}
        if all(c["value"] is None for c in cells.values()):
            continue
        out.append({
            "id": node.id,
            "name": node.name,
            "level": node.level,
            "display": node.display,
            "is_total": node.is_total,
            "headline": node.headline,
            "cells": cells,
        })
    return out


def _trend_ratio(
    node: Any, payloads: list[dict], headline: dict[str, str], found: list[dict],
) -> dict:
    """比率行。单店直接取；多店合并只有利润率能现算，别的给空。"""
    if len(payloads) == 1 and found:
        return {"value": found[0].get("value"), "stores": 1}
    if node.headline != "margin":
        return {"value": None, "stores": len(found)}
    revenue = sum(
        decimal_amount(r.get("value") or 0) for pay in payloads
        for r in pay.get("statement", [])
        if r.get("id") == headline.get("revenue") and r.get("available")
    )
    profit = sum(
        decimal_amount(r.get("value") or 0) for pay in payloads
        for r in pay.get("statement", [])
        if r.get("id") == headline.get("profit") and r.get("available")
    )
    if not revenue:
        return {"value": None, "stores": len(payloads)}
    return {"value": float(profit / revenue), "stores": len(payloads)}


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
        "gaps": gaps.gaps(st.result, model, _previous(store_id, period)),
        **_period_payload(st.result, model),
    }


def _period_payload(result: dict, model) -> dict:
    """快照加上自检的落点。落点按当前模型现算，已结账的老快照也能点进去。"""
    payload = view.reorder_statement(result, model)
    payload["findings"] = [
        view.finding_action(f, model, payload) for f in payload.get("findings") or []
    ]
    return payload


def _previous(store_id: str, period: str) -> dict | None:
    """这家店上一个算过账的账期的快照。给「上个月有、这个月成了 0」那条用。

    取的是「比它早的里最近的一个」，不是「上一个自然月」：中间断月的时候，
    拿不存在的那个月去比等于这条永远不响。
    """
    ws = workspace()
    earlier = sorted(
        (st.period for st in ws.overview()
         if st.store_id == store_id and st.period < period and st.result),
        reverse=True,
    )
    if not earlier:
        return None
    st = ws.state(store_id, earlier[0])
    return st.result if st else None


@app.get("/api/gaps")
def all_gaps(platform: str = "", store_id: str = "", period: str = "") -> dict:
    """所有店 × 所有账期的缺口清单。

    做成一个接口而不是让前端逐个账期去问：十几家店三个月是几百次请求，而这一页
    要回答的问题恰恰是「哪个店哪个月有问题」——得先全都拿到才能回答。
    """
    model = _model()
    ws = workspace()
    by_id = {s.id: s for s in model.stores}
    # 同一家店的账期按时间排，好让每个账期都能拿到它前一个账期做比对。
    states = sorted(
        (st for st in ws.overview() if st.result),
        key=lambda st: (st.store_id, st.period),
    )
    out = []
    seen_before: dict[str, dict] = {}
    for st in states:
        store = by_id.get(st.store_id)
        before = seen_before.get(st.store_id)
        seen_before[st.store_id] = st.result or {}
        if platform and (not store or store.platform != platform):
            continue
        if store_id and st.store_id != store_id:
            continue
        if period and st.period != period:
            continue
        rows = gaps.gaps(st.result or {}, model, before)
        out.append({
            "store_id": st.store_id,
            "store": store.name if store else st.store_id,
            "platform": store.platform if store else "",
            "period": st.period,
            "state": st.state,
            **gaps.summary(rows),
            "gaps": rows,
        })
    # 有问题的排前面，重的更前面。
    out.sort(key=lambda c: (
        gaps.SEVERITY_ORDER.get(c["worst"], 9), -c["count"], c["store"], c["period"],
    ))
    return {"cells": out}


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
def close_period(store_id: str, period: str, action: PeriodAction) -> dict:
    """结账。自检层不放行就结不了，这是整套东西存在的意义。"""
    _store(_model(), store_id)
    try:
        st = workspace().close_period(store_id, period, by=ANONYMOUS, note=action.note)
    except WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": st.state, "at": st.at, "run_id": st.run_id}


@app.post("/api/stores/{store_id}/periods/{period}/reopen")
def reopen_period(store_id: str, period: str, action: PeriodAction) -> dict:
    """反结账。为什么反必须留痕——理由这一栏是必填的。"""
    _store(_model(), store_id)
    try:
        st = workspace().reopen_period(store_id, period, by=ANONYMOUS, note=action.note)
    except WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": st.state, "note": st.note}


# --------------------------------------------------------------------------- #
# 查数
# --------------------------------------------------------------------------- #


@app.get("/api/search")
def search(q: str, store_id: str = "", period: str = "", platform: str = "",
           limit: int = search_mod.LIMIT) -> dict:
    """按订单号、金额、科目名找到具体是哪个文件第几行。

    对不上账的时候人手里只有一个数或者一个订单号。在这之前，答案要靠在几十兆的
    工作簿里按 Ctrl+F 一张表一张表翻——所以实际上没人查，对不上就手改一个数。
    """
    if not q.strip():
        raise HTTPException(400, "要给一个订单号、金额或者科目名")
    model = _model()
    by_id = {s.id: s for s in model.stores}
    states = [
        st for st in workspace().overview()
        if (not store_id or st.store_id == store_id)
        and (not period or st.period == period)
        and (not platform or getattr(by_id.get(st.store_id), "platform", "") == platform)
        and st.run_id
    ]
    # 从新到旧翻。查的多半是最近的账，而翻到上限就停——这个顺序决定了那句
    #「还有 N 个店期没翻」出现时，没翻的是最旧的那几个。
    states.sort(key=lambda s: (s.period, s.store_id), reverse=True)
    res = search_mod.search(
        states, lambda rid: _facts_path(rid), model, q, limit=min(limit, 1000)
    )
    return search_mod.to_dict(res)


def _facts_path(run_id: int) -> Path | None:
    path = workspace().facts_path(run_id)
    return path if path.exists() else None


@app.get("/api/runs/{run_id}/drill/{node_id}")
def drill(run_id: int, node_id: str, limit: int = view.DRILL_LIMIT,
          offset: int = 0, subject: str = "", file: str = "", q: str = "",
          order: str = "amount", only: str = "counted") -> dict:
    """一个报表数字是怎么来的：按科目、按文件、以及带行号的原始明细。

    只报总数不给行号的话，对不上账时没人查得动，整套系统就退化成又一个看不懂的报表。

    明细可以按科目、来源文件、关键词收窄，并翻页。淘宝那家店一个月的推广扣费就有
    六千多行，只给头 200 行等于没给。
    """
    facts = service.facts_of(workspace(), run_id)
    if facts is None:
        raise HTTPException(404, "这次算账没留明细，重算一次就有了")
    return view.drill(facts, _model(), node_id, limit=min(limit, 2000),
                      value=_node_value(run_id, node_id), offset=offset,
                      subject=subject or None, file=file or None,
                      q=q or None, order=order, only=only)


def _node_value(run_id: int, node_id: str) -> float | None:
    """报表上那个数。从快照里取，而不是让下钻自己再算一遍。

    自己算一遍就会有两个「平台服务费」，而它们必然会在某天分叉。分叉的那天没人
    会发现，因为两个数都长得像对的。
    """
    state = next((s for s in workspace().overview() if s.run_id == run_id), None)
    for line in ((state.result or {}).get("statement") or []) if state else []:
        if line.get("id") == node_id:
            return line.get("value")
    return None


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
        # 提成基数的下拉选项。由模型说哪几行能选，界面不写死节点 id——换一家公司
        # 换一套损益表，这个下拉自己就变了。
        "commission_bases": [{"id": n.id, "name": n.name} for n in model.commission_bases()],
    }


class StorePatch(BaseModel):
    """能改的就这几项。id 和 name 不在里面——见 config.EDITABLE 的说明。"""

    entity: str | None = None
    entity_tax_id: str | None = None
    archived: bool | None = None
    aliases: list[str] | None = None
    #: 提成按损益表哪一行算，以及亏损订单倒扣还是不算。两者都逐店配——
    #: 实测同一家公司三家店就是两套政策。
    commission_base: str | None = None
    commission_on_loss: Literal["deduct", "skip"] | None = None
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
# 提成
# --------------------------------------------------------------------------- #


@app.get("/api/commission")
def commission_summary(period: str = "") -> dict:
    """一个账期全部店铺的提成，按人汇总。这就是「这个月要发多少」那张表。

    数从各店账期的快照里取，不重算：快照存的就是当时那份数据配那份规则算出来的
    结果，账期一结账它就冻住了。汇总页只是把它们并起来，不会出现「总览上是一个数、
    点进单店是另一个数」这种事。
    """
    model = _model()
    ws = workspace()
    by_id = {s.id: s for s in model.stores}

    states = list(ws.overview())
    periods = sorted({st.period for st in states}, reverse=True)
    chosen = period or (periods[0] if periods else "")

    # 兼职费用要在这里摊，不能在单店的快照里摊：一家店摊到多少取决于别家店这个月
    # 卖了多少。放进快照的话，别家店重算一次，这家店已经冻住的数就悄悄地不对了。
    # 这一页是「这个月要发多少」，所有店都在手上，正是摊它的地方。
    here = [st for st in states if st.period == chosen]
    revenue_node = next((n.id for n in model.statement if n.headline == "revenue"), "")
    spread = overhead.allocate(
        chosen,
        model.overhead(chosen),
        [(st.store_id, _node(st.result or {}, revenue_node) or 0.0) for st in here],
    )

    people: dict[str, dict[str, Any]] = {}
    stores: list[dict[str, Any]] = []
    for st in here:
        payload = st.result or {}
        c = payload.get("commission") or {}
        store = by_id.get(st.store_id)
        # 摊到的兼职从提成基数里减掉，各人金额按同一个比例缩。业务的口径是
        # 提成利润 = 店铺利润 − 兼职，提成按提成利润算；每人按自己那份等比例缩，
        # 等价于先减后分，而且各人加起来仍然等于缩过的合计。
        cut = spread.of(st.store_id)
        base_total = c.get("base_total", 0.0)
        after = money_float(decimal_amount(base_total) - decimal_amount(cut))
        keep = (after / base_total) if base_total else 1.0
        stores.append({
            "store_id": st.store_id,
            "store": store.name if store else st.store_id,
            "platform": store.platform if store else "",
            "state": st.state,
            "stale": st.stale,
            # 快照里根本没有提成这一段，说明它是加提成功能之前算的。这和「算过、
            # 结果是 0」必须分开显示：都摆成 0.00 的话，一家还没算过的店看起来
            # 就像一家没赚到钱的店。
            "computed": "commission" in payload,
            # 逐店记基数名，因为口径可以逐店配。整页共用一个表头的话，一家按毛利
            # 提、一家按利润提的时候，其中一列的标题就是错的。
            "base_name": c.get("base_name") or "",
            "base_total": base_total,
            # 摊到的兼职、扣完之后的基数、扣完之后要发的钱。三个数都给出来，
            # 因为「为什么这个月比店铺利润算出来的少」这句话只有并排摆着才回答得了。
            "overhead": cut,
            "base_after": after,
            "total": money_float(decimal_amount(c.get("total", 0.0)) * decimal_amount(keep)),
            "total_before": c.get("total", 0.0),
            "unassigned_base": c.get("unassigned_base", 0.0),
            "fallback_base": c.get("fallback_base", 0.0),
            "negative_orders": c.get("negative_orders", 0),
            "negative_base": c.get("negative_base", 0.0),
            "on_loss": c.get("on_loss", "deduct"),
            "skipped_loss_base": c.get("skipped_loss_base", 0.0),
            "configured": bool(c.get("configured")),
            "notes": c.get("notes") or [],
            "people": [
                p | {"amount": money_float(decimal_amount(p.get("amount") or 0.0)
                                           * decimal_amount(keep))}
                for p in c.get("people") or []
            ],
        })
        for p in stores[-1]["people"]:
            slot = people.setdefault(p["person"], {"person": p["person"], "amount": 0.0,
                                                   "base": 0.0, "products": 0, "stores": []})
            slot["amount"] += p.get("amount") or 0.0
            slot["base"] += p.get("base") or 0.0
            # 商品是逐店的，跨店直接相加不会重复计数。
            slot["products"] += int(p.get("products") or 0)
            slot["stores"].append({
                "store": store.name if store else st.store_id,
                "store_id": st.store_id,
                "amount": p.get("amount") or 0.0,
            })

    ranked = sorted(people.values(), key=lambda p: -p["amount"])
    for p in ranked:
        p["amount"] = money_float(p["amount"])
        p["base"] = money_float(p["base"])

    # 各店口径不一致时不给整页表头编一个名字。「毛利合计」这个标题底下加着几家
    # 按利润算的钱，是那种看一年都看不出来的错。
    names = sorted({s["base_name"] for s in stores if s["base_name"]})
    return {
        "period": chosen,
        "periods": periods,
        "base_name": names[0] if len(names) == 1 else _base_name(model),
        "base_mixed": len(names) > 1,
        "people": ranked,
        "stores": sorted(stores, key=lambda s: -s["total"]),
        "total": money_float(sum(p["amount"] for p in ranked)),
        "unassigned_base": money_float(sum(s["unassigned_base"] for s in stores)),
        "rules": len(model.commission),
        # 兼职费用怎么摊的。摊了多少、按什么摊、有没有摊出来，都要写在页面上：
        # 这一步会让每个人到手的钱变少，不说清楚的话没人对得上账。
        "overhead": {
            "name": "兼职费用",
            "total": spread.total,
            "settled": spread.settled,
            "basis_name": _base_label(model, "revenue") or "交易收款",
            "basis_total": spread.basis_total,
            "shares": [
                {"store_id": s.store_id, "store": by_id[s.store_id].name
                 if s.store_id in by_id else s.store_id,
                 "basis": s.basis, "amount": s.amount}
                for s in spread.shares
            ],
            "notes": list(spread.notes),
        },
    }


def _base_label(model: Model, headline: str) -> str:
    return next((n.name for n in model.statement if n.headline == headline), "")


def _base_name(model: Model, store_id: str = "") -> str:
    node = model.commission_base_node(store_id)
    return node.name if node else ""


@app.get("/api/commission/config")
def commission_config(store_id: str = "") -> dict:
    """当前的提成配置。界面上照原样列出来，也能导出去改。"""
    model = _model()
    if store_id:
        _store(model, store_id)
    return {
        "columns": list(COMMISSION_COLUMNS),
        "headers": dict(COMMISSION_HEADERS),
        "rules": view.commission_rules(model, store_id),
        "stores": [
            {"id": s.id, "name": s.name, "platform": s.platform}
            for s in model.active_stores()
        ],
    }


@app.get("/api/commission/config.csv")
def commission_export() -> PlainTextResponse:
    """把配置导成 CSV，拿去 Excel 里改。

    表头用英文列名而不是中文：导出来的这份改完要能原样传回来，两边同一套列名
    才不会在中英之间来回翻译时丢字段。上传那头两种表头都认，所以人手写中文表头
    也能传，但系统吐出来的永远是这一份。
    """
    rows = view.commission_rules(_model())
    lines = [",".join(COMMISSION_COLUMNS)]
    for r in rows:
        lines.append(",".join(csv_cell(r.get(c, "")) for c in COMMISSION_COLUMNS))
    # BOM 是给 Excel 的：没有它，双击打开中文全是乱码，人会以为系统坏了。
    return PlainTextResponse(
        "\ufeff" + "\n".join(lines) + "\n",
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="commission.csv"'},
    )


@app.get("/api/commission/products.csv")
def commission_products(period: str = "", store_id: str = "", limit: int = 5000) -> PlainTextResponse:
    """把这个账期出现过的商品导成一份待配表：商品和毛利已经填好，只等填人和比例。

    这一个端点决定了提成功能能不能真正被用起来。系统这边已经知道每个商品今年
    卖了多少、赚了多少；不给这份表，人就得自己去别处凑出商品清单，或者对着
    七百多个商品 ID 手打——那样的话，配置提成这件事永远排不上队。

    按毛利从大到小排。真要精细配的就是最上面那几十个，剩下的长尾丢给店铺兜底，
    这份排序直接告诉人该在哪儿停手。
    """
    ws = workspace()
    model = _model()
    states = [st for st in ws.overview()
              if (not period or st.period == period)
              and (not store_id or st.store_id == store_id)]
    if not states:
        raise HTTPException(404, "这个账期还没有算过的店")

    # 末尾两列不是配置项，是参考数据。加载器按列名取值，多出来的列它不看，
    # 所以这份表填完能原样传回来，不用先删列——多一步就会有人漏做。
    lines = [",".join((*COMMISSION_COLUMNS, "本期毛利", "本期子订单数"))]
    items: list[tuple[float, str]] = []
    for st in sorted(states, key=lambda s: s.store_id):
        c = (st.result or {}).get("commission") or {}
        # 生效日期给一个能直接用的默认值：这个账期的第一天。留空的话每一行都得
        # 手填一遍日期，几百行里漏填一行，整份表就传不上去。
        first_day = f"{st.period}-01" if len(st.period) == 7 else ""
        for p in c.get("products") or []:
            pid = p.get("product_id") or ""
            base = float(p.get("base") or 0.0)
            items.append((base, ",".join((
                first_day, st.store_id,
                # 商品 ID 是十二三位纯数字。Excel 会按数字读，再存回 CSV 时写成
                # 1.04783E+12——一个被截过的 ID 传回来匹配不上任何订单，而且不报错，
                # 只表现为「这个商品怎么没提成」。前导等号逼 Excel 当文本读，
                # 上传那头再把它剥掉。
                csv_cell(f'="{pid}"' if pid else ""),
                csv_cell(p.get("product_name") or ""),
                "", "", "",
                csv_cell(f"{base:.2f}"),
                csv_cell(str(p.get("sub_orders", 0))),
            ))))
    lines.extend(line for _base, line in sorted(items, key=lambda x: -x[0])[:limit])
    return PlainTextResponse(
        "\ufeff" + "\n".join(lines) + "\n",
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="commission-products.csv"'},
    )


@app.get("/api/commission/products")
def commission_product_list(period: str = "", store_id: str = "") -> dict:
    """这个账期卖过的商品，每个带上毛利和「系统猜它归谁」。

    这一个端点决定了提成配置是一张空表还是一张填好八成的表。运营归属那份历史
    数据里有五万三千个商品的负责人，淘宝那家店当期七百多个商品能对上八成七。
    让人对着几百个商品 ID 手打人名，这件事永远排不上队；把猜测摆出来让人否决，
    十分钟就配完了。

    猜测只是猜测：它不进计算，进计算的永远是 commission.csv。这里返回的
    `suggest_person` 旁边一定带 `suggest_since`（这条归属记于哪个月），
    因为归属数据可能比当前账期旧好几个月，人有权知道自己在沿用多久前的安排。
    """
    ws = workspace()
    model = _model()
    states = [st for st in ws.overview()
              if (not period or st.period == period)
              and (not store_id or st.store_id == store_id)]
    if not states:
        raise HTTPException(404, "这个账期还没有算过的店")
    chosen = period or max(st.period for st in states)
    states = [st for st in states if st.period == chosen]

    by_id = {s.id: s for s in model.stores}
    products: list[dict[str, Any]] = []
    stores: list[dict[str, Any]] = []
    for st in sorted(states, key=lambda s: s.store_id):
        c = (st.result or {}).get("commission") or {}
        items = c.get("products") or []
        ids = [str(p.get("product_id") or "") for p in items]
        owners = ownership.owners_at(DEFAULT_MODEL, chosen, ids)
        store = by_id.get(st.store_id)
        per_person: dict[str, dict[str, Any]] = {}
        for p in items:
            pid = str(p.get("product_id") or "")
            owner = owners.get(pid)
            base = float(p.get("base") or 0.0)
            products.append({
                "store_id": st.store_id,
                "store": store.name if store else st.store_id,
                "product_id": pid,
                "product_name": p.get("product_name") or "",
                "base": base,
                "sub_orders": p.get("sub_orders", 0),
                "amount": p.get("amount", 0.0),
                "total_rate": p.get("total_rate", 0.0),
                "configured": not (p.get("fallback") or p.get("unassigned")),
                "fallback": bool(p.get("fallback")),
                "suggest_person": owner.person if owner else "",
                "suggest_since": owner.since if owner else "",
                "suggest_store": owner.store if owner else "",
            })
            key = owner.person if owner else ""
            slot = per_person.setdefault(key, {"person": key, "products": 0, "base": 0.0})
            slot["products"] += 1
            slot["base"] += base
        for slot in per_person.values():
            slot["base"] = money_float(slot["base"])
        stores.append({
            "store_id": st.store_id,
            "store": store.name if store else st.store_id,
            "platform": store.platform if store else "",
            "products": len(items),
            "base_total": c.get("base_total", 0.0),
            "owners": sorted(per_person.values(), key=lambda d: -d["base"]),
        })

    return {
        "period": chosen,
        "stores": stores,
        "products": sorted(products, key=lambda d: -d["base"]),
        # 归属数据截止到哪个月。比当前账期旧就说明界面上那些建议是沿用来的。
        "ownership_latest": ownership.coverage(DEFAULT_MODEL, chosen, [])["latest_period"],
    }


class RatePlan(BaseModel):
    """一个商品的总提成率怎么分给几个人，由系统展开成逐商品的配置。

    分法只有两种角色，因为实际的表里就只有这两种：

    运营
        谁管这个商品谁拿这一格。管哪些商品是系统已经知道的（历史归属），人只要
        回答「这个人几个点」。反过来让人逐商品填，七百行里填错一行没人看得出来。
    固定分成
        主管、助理这类，不看商品归谁，每个商品都分一份。淘宝那家店的李秋雨就是
        这种：运营 3.5、她 1.5，加起来是这家店的总提成率 5。

    一个商品的总提成率就是这两部分之和。这也是为什么不能只给「兜底一个人」的
    字段——那样表达不了「五个点分给两个人」，界面把现状显示成一个人，人一保存
    另一个人的点数就没了，而这件事要到发工资那天才有人发现。
    """

    store_id: str
    period: str = ""
    effective_from: str = ""
    #: 运营 → 他管的商品里他拿几个点。写 0.03 或 3 都按 3% 理解不了，所以这里
    #: 只收小数，界面上负责把「3%」翻成 0.03——翻译放在离人最近的地方。
    rates: dict[str, float] = {}
    #: 商品 → 运营，改掉系统猜的归属。历史归属数据认不出新来的人，也认不出这个月
    #: 刚交接的商品；没有这个口子，新人只能靠手改 CSV 才能绑上，那这一页就等于
    #: 只对老人有用。写 "-" 表示这个商品没人管，运营那一格空着。
    owners: dict[str, str] = {}
    #: 每个商品都分一份的人 → 几个点。不看商品归谁。
    fixed: dict[str, float] = {}
    #: 没有归属的商品，运营那一格归谁。留空就是这一格不给人，只剩固定分成。
    fallback_owner: str = ""
    #: 单人写法。老调用方和命令行还在用，收到就当成「兜底运营 + 他的点数」。
    fallback_person: str = ""
    fallback_rate: float = 0.0
    note: str = ""

    def operator_rates(self) -> dict[str, float]:
        return {**self.rates, **(
            {self.fallback_person: self.fallback_rate}
            if self.fallback_person and self.fallback_rate
            and self.fallback_person not in self.rates else {}
        )}

    def default_owner(self) -> str:
        return self.fallback_owner or self.fallback_person

    def split_for(self, owner: str) -> dict[str, float]:
        """这个商品的提成分给谁、各几个点。运营那一格空着就只剩固定分成。"""
        out = {p: r for p, r in self.fixed.items() if p and r}
        rate = self.operator_rates().get(owner, 0.0) if owner else 0.0
        if rate:
            out[owner] = round(out.get(owner, 0.0) + rate, 10)
        return out


@app.post("/api/commission/plan")
def commission_plan(plan: RatePlan, apply: bool = False) -> dict:
    """把「总提成率怎么分」展开成提成配置。`apply=false` 只预览，不落盘。

    展开的时候会做一次压缩：分法和店铺默认那一份一样的商品不单独出行，交给店铺
    那一组盖住。淘宝那家店七百多个商品的运营和点数都一样，压缩前是七百多行配置，
    压缩后是两行——两者算出来的钱一分不差，但后者是人看得懂的。

    同一个（生效日期，店铺）重复展开会覆盖上一次的结果，不会叠加。别的生效日期
    原样留着：提成是生效制的，改配置就是往表里加一个新版本，旧版本得留下来，
    不然上个月的账重算一遍会变成这个月的规则。
    """
    model = _model()
    store = _store(model, plan.store_id)
    effective = plan.effective_from or (f"{plan.period}-01" if len(plan.period) == 7 else "")
    if not effective:
        raise HTTPException(400, "要给生效日期，不然不知道这份配置从哪天开始算")

    listing = commission_product_list(period=plan.period, store_id=plan.store_id)
    items = [p for p in listing["products"] if p["store_id"] == plan.store_id]

    generated: list[dict[str, str]] = []
    covered = {"by_product": 0, "by_store": 0, "nobody": 0}
    store_level = plan.split_for(plan.default_owner())

    def rows(product_id: str, product_name: str, split: dict[str, float],
             note: str) -> list[dict[str, str]]:
        # 同一组里每条写一样的总数，加载时校验组内相加等于它。几个人分一个商品
        # 时，总数就是几个人加起来。
        total = round(sum(split.values()), 10)
        return [{
            "effective_from": effective, "store": store.id,
            "product_id": product_id, "product_name": product_name,
            "person": who, "share": str(share), "total_rate": str(total),
            "note": plan.note or note,
        } for who, share in split.items()]

    for p in items:
        override = plan.owners.get(p["product_id"], "")
        # 谁都没指、归属也认不出的商品，落到兜底运营身上——这就是第四步那句
        # 「没人管的商品运营那一格归谁」。只有明写 "-" 才真的空着。
        owner = "" if override == "-" else (
            override or p["suggest_person"] or plan.default_owner()
        )
        split = plan.split_for(owner)
        if not split:
            # 谁都不分的商品没法单独表达：店铺那一组不写商品号，引擎眼里它盖住
            # 所有没单独配的商品。所以这里只能如实说——有店铺那一组就是走它，
            # 没有才是真的谁都不给。
            covered["by_store" if store_level else "nobody"] += 1
            continue
        # 这个商品的分法和店铺那一份一样，单独写只是同一笔钱的两种写法。淘宝那家
        # 店 627 个商品的运营和点数都跟店铺那份一致，压掉之后配置从 629 行变成 2 行。
        if split == store_level:
            covered["by_store"] += 1
            continue
        covered["by_product"] += 1
        generated += rows(
            p["product_id"], p["product_name"], split,
            "人工指定" if override else f"按运营归属展开（{p['suggest_since'] or '无出处'}）",
        )
    generated += rows("", "", store_level, "店铺默认分法：没有单独配的商品")

    kept = [r for r in view.commission_rules(model)
            if not (r["store"] == store.id and r["effective_from"] == effective)]
    merged = [{k: str(r.get(k, "")) for k in COMMISSION_COLUMNS} for r in kept] + generated

    result = {
        "effective_from": effective,
        "store_id": store.id,
        "generated": len(generated),
        "kept": len(kept),
        "coverage": covered,
        "preview": generated[:50],
        "applied": False,
        "periods": [],
    }
    if not apply:
        return result

    try:
        replace_commission(DEFAULT_MODEL, merged)
    except (ModelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    result["applied"] = True
    result["periods"] = service.recompute(workspace(), _model(), store).periods
    return result


@app.post("/api/commission/config")
def commission_upload(
    file: Annotated[UploadFile, File()],
    recompute_stores: bool = True,
) -> dict:
    """传一份新的提成配置，整份替换。

    整份替换而不是逐条改，因为提成配置的正确性是整份的：同一版里几个人的比例
    加起来必须等于总提成率，单条改动没法校验这件事。传上来先整体校验，
    有一条不对就整份退回，一个字节都不落盘——存进一份自相矛盾的配置，
    下次加载模型会直接失败，整套系统起不来。

    校验过了默认把涉及的店重算一遍。不重算的话，界面上配置已经变了、
    账期里的提成数字还是旧的，两个数并排放着而人不知道该信哪个。
    """
    raw = file.file.read()
    name = Path(file.filename or "").name
    try:
        rows = commission_rows(name, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not rows:
        raise HTTPException(400, "这份表里没有数据行")

    before = {r["store"] for r in view.commission_rules(_model())}
    try:
        count = replace_commission(DEFAULT_MODEL, rows)
    except (ModelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    model = _model()
    touched = sorted((before | {r.get("store", "") for r in rows}) & {s.id for s in model.stores})
    periods: list[dict] = []
    if recompute_stores:
        ws = workspace()
        for sid in touched:
            periods.extend(service.recompute(ws, model, model.store(sid)).periods)
    return {"count": count, "stores": touched, "periods": periods}


def commission_rows(name: str, raw: bytes) -> list[dict[str, str]]:
    """把上传的 CSV 或 Excel 读成配置行。

    两种格式都收，因为业务维护提成用的是 Excel，而系统导出的是 CSV。
    只收一种的话，等于要求他们每次改完都另存一次——多一步就会有人跳过，
    跳过就会有人直接去改服务器上的文件。
    """
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls", ".xlsb"):
        table = _excel_rows(raw, name)
    elif suffix == ".csv" or not suffix:
        table = _csv_rows(raw)
    else:
        raise ValueError(f"读不了 {suffix} 这种文件，请传 CSV 或 Excel")
    if not table:
        raise ValueError("这份表是空的")

    header, *body = table
    mapping = {i: commission_column(h) for i, h in enumerate(header)}
    known = {c for c in mapping.values() if c}
    missing = [c for c in ("effective_from", "store", "person", "share", "total_rate")
               if c not in known]
    if missing:
        raise ValueError(
            "缺这几列：" + "、".join(COMMISSION_HEADERS[c] for c in missing) +
            "。这份表的表头是：" + "、".join(h for h in header if h)
        )

    out: list[dict[str, str]] = []
    for row in body:
        rec = {mapping[i]: _unguard(str(v)) for i, v in enumerate(row)
               if i in mapping and mapping[i] and v is not None}
        if not any(rec.get(c) for c in ("store", "person")):
            continue
        out.append(rec)
    return out


def _unguard(cell: str) -> str:
    """剥掉导出时给 Excel 加的 `="..."` 外壳。

    直接在 Excel 里打开填完、另存为 xlsx 的话，calamine 读到的是公式算完的文本，
    壳自己就没了。但有人会用文本编辑器改完 CSV 直接传回来，那时壳还在——
    带着壳的商品 ID 匹配不上任何订单，而且不报错。
    """
    s = cell.strip()
    if s.startswith('="') and s.endswith('"') and len(s) > 3:
        return s[2:-1].strip()
    return s


def _csv_rows(raw: bytes) -> list[list[str]]:
    import csv as _csv
    import io as _io
    text = raw.decode("utf-8-sig", errors="replace")
    return [r for r in _csv.reader(_io.StringIO(text))]


def _excel_rows(raw: bytes, name: str) -> list[list[str]]:
    import io as _io

    from python_calamine import CalamineWorkbook
    try:
        wb = CalamineWorkbook.from_filelike(_io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name} 打不开：{exc}") from exc
    if not wb.sheet_names:
        raise ValueError(f"{name} 里没有工作表")
    rows = wb.get_sheet_by_name(wb.sheet_names[0]).to_python()
    out = []
    for r in rows:
        cells = ["" if c is None else str(c).strip() for c in r]
        # 日期被 Excel 存成 datetime，str() 出来带时分秒，规整成 2026-05-01
        cells = [c.split(" ")[0] if _LOOKS_LIKE_TIMESTAMP.match(c) else c for c in cells]
        out.append(cells)
    return out


_LOOKS_LIKE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


# --------------------------------------------------------------------------- #
# 费项规则
# --------------------------------------------------------------------------- #


class FeeRuleIn(BaseModel):
    platform: str = "*"
    field: str = "subject"
    how: str = "exact"
    value: str = ""
    major: str = ""
    minor: str = ""
    exclude: bool = False
    count_without_order: bool = False
    stage: str = "after"
    note: str = ""
    by: str = ""
    at: str = ""


class FeeRulesBody(BaseModel):
    rules: list[FeeRuleIn]
    note: str = ""
    store_id: str = ""
    recompute: bool = True


class FeeSuggestIn(BaseModel):
    label: str
    field: str = "subject"


@app.get("/api/fees")
def fees_catalog() -> dict:
    """费项台账：引擎认识什么、这个月认不出什么、界面上配了哪些规则。"""
    model = _model()
    known = fees_mod.known_fees(model)
    return {
        "majors": fees_mod.major_options(model),
        "fields": [{"id": i, "name": n} for i, n in fees_mod.FEE_FIELDS],
        "hows": [{"id": i, "name": n} for i, n in fees_mod.FEE_HOWS],
        "stages": [{"id": i, "name": n} for i, n in fees_mod.FEE_STAGES],
        "platforms": view.platform_options(model),
        "rules": [fees_mod.rule_dict(r) for r in model.fee_rules],
        "known": [
            {
                "key": f.key, "major": f.major, "platform": f.platform,
                "origin": f.origin, "origin_name": f.origin_name,
                "how": f.how, "field": f.field,
                "excluded": f.excluded,
            }
            for f in known if f.origin != "fee-rules"
        ],
        "platform_aliases": fees_mod.platform_aliases(model),
        "unmatched": fees_mod.unmatched_from(workspace()),
        "log": workspace().config_history("fee-rules"),
        "model_revision": model_revision(DEFAULT_MODEL),
    }


@app.post("/api/fees/suggest")
def fees_suggest(body: FeeSuggestIn) -> dict:
    """模型给一条未归类科目的建议。建议不是配置，人确认后才落库。"""
    model = _model()
    return assist.suggest_fee(
        body.label, fees_mod.major_options(model), body.field,
        root=workspace().root,
    )


@app.post("/api/fees/preview")
def fees_preview(body: FeeRulesBody) -> dict:
    """用提交的规则真算一家店，不写任何东西。"""
    if not body.store_id:
        raise HTTPException(400, "试算必须指定一家店。全公司重算只在确认落库时做。")
    base = _model()
    store = _store(base, body.store_id)
    try:
        patched = _model_with_fee_rules(base, body.rules)
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    slices = service.simulate(workspace(), patched, store)
    return {
        "store_id": store.id,
        "store": store.name,
        "periods": [
            {
                "period": sl["period"],
                "diff": fees_mod.payload_diff(sl["before"], sl["after"]),
                "unclassified_before": len((sl["before"] or {}).get("unclassified") or []),
                "unclassified_after": len((sl["after"] or {}).get("unclassified") or []),
            }
            for sl in slices
        ],
    }


@app.post("/api/fees")
def fees_apply(body: FeeRulesBody) -> dict:
    """把规则写进 fee-rules.csv，然后把有表的店重算。"""
    base = _model()
    before = [fees_mod.rule_dict(r) for r in base.fee_rules]
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for row in body.rules:
        if not row.at:
            row.at = stamp
    try:
        rules = [_fee_rule(r) for r in body.rules]
        count = replace_fee_rules(DEFAULT_MODEL, rules)
    except (ModelError, ValidationError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    after = [fees_mod.rule_dict(r) for r in _model().fee_rules]
    workspace().log_config(
        "fee-rules",
        body.note.strip() or f"费项规则改为 {count} 条",
        by=ANONYMOUS,
        before=before,
        after=after,
    )
    periods: list[dict] = []
    failures: list[dict] = []
    if body.recompute:
        model = _model()
        ws = workspace()
        for store in model.stores:
            if store.archived or not ws.active_files(store.id):
                continue
            done = service.recompute(ws, model, store)
            periods.extend(done.periods)
            if done.failure:
                failures.append(done.failure)
    return {"count": count, "periods": periods, "failures": failures}


def _model_with_fee_rules(model: Model, rows: list[FeeRuleIn]) -> Model:
    """内存里叠一份规则，用来试算。不落盘。"""
    try:
        return model.model_copy(update={"fee_rules": tuple(_fee_rule(r) for r in rows)})
    except ValidationError as exc:
        raise ModelError(str(exc)) from exc


def _fee_rule(row: FeeRuleIn) -> FeeRule:
    try:
        return FeeRule(
            platform=row.platform.strip() or "*",
            field=row.field.strip() or "subject",
            how=row.how.strip() or "exact",  # type: ignore[arg-type]
            value=row.value,
            major=row.major.strip(),
            minor=row.minor.strip(),
            exclude=row.exclude,
            count_without_order=row.count_without_order,
            stage=row.stage.strip() or "after",  # type: ignore[arg-type]
            note=row.note.strip(),
            by=row.by.strip(),
            at=row.at.strip(),
        )
    except ValidationError as exc:
        loc = "、".join(str(e["msg"]) for e in exc.errors())
        raise ModelError(f"规则「{row.value}」有问题：{loc}") from exc


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
        payload = view.draft_dict(draft, table, model)
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**payload, "model_revision": model_revision(DEFAULT_MODEL)}


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
        payload = view.draft_dict(draft, table, model)
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    assisted = onboard.advise(draft, model)
    return {
        **payload,
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
def onboard_commit(commit: OnboardCommit) -> dict:
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
            by=ANONYMOUS, expected_revision=commit.model_revision,
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
