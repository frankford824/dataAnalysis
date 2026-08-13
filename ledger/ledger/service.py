"""编排层：交表 → 留档 → 自动算账 → 存快照。

这一层的存在理由是「自动计算」这四个字。店长的动作只有一个：把表拖进去。
剩下的——这是哪家店的、顶掉了哪一版旧表、要重算哪几个账期、结果存哪、
已结账的月份要不要动——都在这里决定，不该让界面或者人去操心。

一条规则贯穿全篇：**认不出归属的文件绝不塞进某家店凑数**。那会把一家店的钱记到
另一家头上，而且事后极难发现。宁可拦下来问人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable

import polars as pl

from . import commission as comm
from . import progress
from .engine.runtime import Ingestion, RunResult, Slice, ingest, run
from .model.schema import Model, Store
from .view import commission_dict, slice_dict
from .workspace import Kept, Workspace

#: 能解析的文件后缀。别的一律不碰，也不假装能读。
SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb", ".csv", ".zip"}


@dataclass
class Rejected:
    """没能进账的一个文件，以及下一步该怎么办。"""

    file: str
    why: str
    #: 认不出归属时的登记建议。只是提示，不参与计算。
    suggest: dict[str, str] = field(default_factory=dict)


@dataclass
class Intake:
    """一次交表的结果。界面上传完就渲染这个。"""

    kept: list[Kept] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    #: 受影响的店。这些店会被重算。
    stores: list[str] = field(default_factory=list)
    #: 重算出来的账期快照。
    periods: list[dict[str, Any]] = field(default_factory=list)
    #: 算不出结果的店，带原因。
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: 认得出是表、但没有模板认识它。接表向导的入口。
    unknown_tables: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"收下 {len(self.kept)} 份表"]
        changed = [k for k in self.kept if not k.unchanged]
        if len(changed) != len(self.kept):
            parts.append(f"{len(self.kept) - len(changed)} 份和上次一样")
        if self.periods:
            ok = sum(1 for p in self.periods if p.get("can_close"))
            parts.append(f"算了 {len(self.periods)} 个账期，{ok} 个可以结账")
        if self.rejected:
            parts.append(f"{len(self.rejected)} 份没能进账")
        return "，".join(parts)


def intake(
    ws: Workspace,
    model: Model,
    uploads: Iterable[tuple[str, IO[bytes] | Path]],
    by: str = "",
    report: progress.Reporter = progress.SILENT,
) -> Intake:
    """收一批文件，留档，然后把受影响的店重算一遍。

    重算的是**整家店**而不是这一批文件：损益要靠订单明细做脊柱，单独拿一张运费表
    是算不出账的。留档的意义就在这里——上周交的订单明细还在，这周补一张运费表就能
    立刻出完整结果。

    `report` 是给界面看的旁白。这件事在服务端要跑十几秒到几分钟，不报的话人只能
    看着一个转圈猜它死没死。
    """
    out = Intake()
    touched: list[str] = []
    files = list(uploads)

    for i, (name, src) in enumerate(files, 1):
        report("留档", i, len(files))
        name = Path(name).name
        if not name:
            continue
        if Path(name).suffix.lower() not in SUFFIXES:
            out.rejected.append(Rejected(
                file=name, why="不是能解析的表格。支持 " + "、".join(sorted(SUFFIXES)),
            ))
            continue
        store = model.store_of(name)
        if store is None:
            out.rejected.append(Rejected(
                file=name,
                why="认不出是哪家店的，没进账",
                suggest=suggest_store(name, model),
            ))
            continue
        out.kept.append(ws.keep(name, src, store.id, by=by))
        if store.id not in touched:
            touched.append(store.id)

    out.stores = touched
    for i, store_id in enumerate(touched, 1):
        store = model.store(store_id)
        # 报店名而不是「第 2 家店」：交表的人认得店名，认不得序号。
        done = recompute(
            ws, model, store,
            report=report, note=f"{store.name}（{i}/{len(touched)} 家店）",
        )
        out.periods.extend(done.periods)
        out.unknown_tables.extend(done.unknown_tables)
        if done.failure:
            out.failures.append(done.failure)
    return out


@dataclass
class Recomputed:
    """一家店重算一次的结果。"""

    store_id: str
    periods: list[dict[str, Any]] = field(default_factory=list)
    unknown_tables: list[dict[str, Any]] = field(default_factory=list)
    #: 一个账期都算不出来时的原因。正常情况是 None。
    failure: dict[str, Any] | None = None


def _commission(result: RunResult, model: Model, store: Store, period: str) -> dict[str, Any]:
    """提成算完跟着损益一起进快照。

    为什么存快照而不是每次现算：现算要重新解析这家店全部文件，三十秒起步，
    点开一个页面等半分钟没人会用。更要紧的是，存进快照它就跟着账期一起冻结——
    账报出去之后，提成数字不会因为有人后来改了一条配置就悄悄变了。
    改了配置想让新数生效，走重算，和改字典、改模板是同一条路。

    算不出来不能让整个重算失败：提成是这套账的附加视图，配置写错了该说清楚
    是提成配置写错了，而不是让这家店连损益表都出不来。
    """
    try:
        return commission_dict(comm.compute(result, model, store.id, period))
    except Exception as exc:  # noqa: BLE001 — 什么都不该让重算倒下
        return {
            "base_node": "", "base_name": "", "base_total": 0.0, "total": 0.0,
            "configured": False, "unassigned_base": 0.0, "fallback_base": 0.0,
            "negative_orders": 0, "negative_base": 0.0,
            "people": [], "products": [],
            "notes": [f"提成算不出来：{exc}"],
        }


def recompute(
    ws: Workspace,
    model: Model,
    store: Store,
    report: progress.Reporter = progress.SILENT,
    note: str = "",
) -> Recomputed:
    """拿这家店当前生效的全部文件重算，把每个账期的结果存成快照。

    已结账的账期不会被覆盖：`Workspace.record` 只追加，展示时仍然给结账那一版。
    账已经报出去了，系统不能因为字典补了一条就把数字悄悄改掉。
    """
    out = Recomputed(store_id=store.id)
    files = ws.active_files(store.id)
    if not files:
        out.failure = {"store": store.name, "why": "这家店还没有任何数据"}
        return out

    where = note or store.name
    report(f"读表 · {where}", 0, len(files))
    ing = ingest(
        files, model, [store.name],
        each=lambda done, total: report(f"读表 · {where}", done, total),
    )
    out.unknown_tables = unknown_tables(ing, store)
    # 这一段说不出份数：挂钩、归类、核算是把全店的行放在一起算的，没有「第几份」
    # 可报。硬报个 0/9 会让人以为它卡在第零份上。
    report(f"归类核算 · {where}")
    result = run(ing, store.platform)

    if not result.slices:
        out.failure = {
            "store": store.name,
            "why": f"{len(files)} 份表都没算出结果",
            "reasons": [
                f"{i.ref.label()}：{i.error or i.recognition.reason}" for i in ing.unknown
            ],
        }
        return out

    shas = [i.ref.sha256 for i in ing.items]
    slices = sorted(result.slices.items(), key=lambda kv: (kv[0][1] or ""))
    for i, ((_s, _p), sl) in enumerate(slices, 1):
        report(f"存账期 · {where}", i, len(slices))
        payload = slice_dict(sl, store, model)
        payload["commission"] = _commission(result, model, store, sl.period)
        run_id = ws.record(store.id, sl.period, payload, shas, evidence_ready=False)
        _keep_facts(ws, run_id, sl)
        state = ws.state(store.id, sl.period)
        shown = state.result if state and state.result else payload
        out.periods.append({
            **shown,
            "run_id": run_id,
            "state": state.state if state else "open",
            "stale": bool(state and state.stale),
        })
    return out


def _keep_facts(ws: Workspace, run_id: int, sl: Slice) -> None:
    """把事实行落一份；失败会把本次快照降级为不可结账。"""
    if sl.facts.is_empty():
        ws.mark_evidence(run_id, ready=True)
        return
    path = ws.facts_path(run_id)
    try:
        sl.facts.write_parquet(path)
        ws.mark_evidence(run_id, ready=True)
    except Exception as exc:  # 磁盘满、权限之类必须显式拦住结账
        path.unlink(missing_ok=True)
        ws.mark_evidence(run_id, ready=False, error=str(exc))


def facts_of(ws: Workspace, run_id: int) -> pl.DataFrame | None:
    """取回某次算账的事实行。没留档就返回 None，界面提示重算一次。"""
    path = ws.facts_path(run_id)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except Exception:  # pragma: no cover
        return None


def unknown_tables(ing: Ingestion, store: Store) -> list[dict[str, Any]]:
    """没有模板认识的表。接表向导从这里起步。

    两类东西要挡在外面，否则这份清单会没人看：

    人工加工产物。那是汇总表，本来就不该有模板，让人去给它配字段映射是把人往坑里带。

    空工作表。店长交上来的工作簿里普遍留着空白的 Sheet1/Sheet2/Sheet3——实测一次
    交 9 份表，报出来 13 张「没见过的表」，其中 11 张是空 Sheet，真正需要人接的
    两张（微信支付宝汇总、改版后的推广表）就埋在里面了。空表不是「没见过」，
    它就是空的：没有表头可映射，也没有数据可进账。
    """
    out = []
    for item in ing.unknown:
        if item.derivative is not None:
            continue
        r = item.recognition
        if r.header_count == 0:
            continue
        out.append({
            "store_id": store.id,
            "file": r.ref.filename,
            "sheet": r.ref.sheet or "",
            "sha": r.ref.sha256,
            "signature": r.signature,
            "header_count": r.header_count,
            "reason": item.error or r.reason,
            "near_misses": [
                {"template": tid, "missing": list(missing)} for tid, missing in r.near_misses
            ],
        })
    return out


def suggest_store(filename: str, model: Model) -> dict[str, str]:
    """认不出归属时给个登记建议。

    文件名形如「类别-店铺名.xlsx」，破折号后面那截就是店名。猜错没关系，反正要人确认；
    完全不猜的话，人得自己去想「这个店该登记成什么 id」。
    """
    stem = Path(filename).stem
    for sep in ("-", "—", "_"):
        if sep in stem:
            name = stem.rsplit(sep, 1)[-1].strip()
            return {"store": name, "platform": model.guess_platform(name)}
    return {"store": "", "platform": ""}


__all__ = [
    "SUFFIXES",
    "Intake",
    "Recomputed",
    "Rejected",
    "facts_of",
    "intake",
    "recompute",
    "suggest_store",
    "unknown_tables",
]
