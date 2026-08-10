"""HTTP 接口。

只有三个端点，因为店长要做的只有一件事：把表交上来，看结果。

上传时必须保留原始文件名。店铺归属、数据源识别都靠文件名——交上来的文件名形如
「聚水潭成本-淘宝喜必顺.xlsx」，破折号前是类别、后面是店铺。换成随机名存盘，
这两件事立刻全瞎。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .cli import DEFAULT_MODEL, SUFFIXES, _as_dict, group_by_store
from .engine.runtime import ingest, run
from .model.config import EDITABLE, add_store, update_store
from .model.loader import ModelError, load_model
from .model.schema import KNOWN_PLATFORMS, Store, guess_platform
from .web import PAGE

app = FastAPI(title="记账", docs_url="/api/docs")


def _model():
    try:
        return load_model(DEFAULT_MODEL)
    except ModelError as exc:
        raise HTTPException(500, f"模型有问题：{exc}") from exc


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


def _store_dict(s) -> dict:
    return {
        "id": s.id, "name": s.name, "platform": s.platform,
        "entity": s.entity, "entity_tax_id": s.entity_tax_id,
        "archived": s.archived, "aliases": list(s.aliases), "note": s.note,
    }


@app.get("/api/stores")
def stores() -> dict:
    model = _model()
    return {
        "stores": [_store_dict(s) for s in model.stores],
        "editable": list(EDITABLE),
        "platforms": sorted({s.platform for s in model.stores} | set(KNOWN_PLATFORMS)),
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
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "没有要改的字段")
    try:
        store = update_store(DEFAULT_MODEL, store_id, changes)
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"store": _store_dict(store)}


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
        store = add_store(DEFAULT_MODEL, Store(**{**new.model_dump(), "aliases": tuple(new.aliases)}))
    except ModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"store": _store_dict(store)}


@app.post("/api/run")
async def run_upload(files: Annotated[list[UploadFile], File()]) -> dict:
    """收一批文件，按店算账。

    文件存进临时目录，算完就删——这一版不留档。要留档得先想清楚保留多久、
    谁能看，那是另一件事，不该顺手做掉。
    """
    model = _model()
    tmp = Path(tempfile.mkdtemp(prefix="ledger-"))
    try:
        saved: list[Path] = []
        skipped: list[str] = []
        for up in files:
            name = Path(up.filename or "").name
            if not name:
                continue
            if Path(name).suffix.lower() not in SUFFIXES:
                skipped.append(name)
                continue
            target = tmp / name
            with target.open("wb") as fh:
                shutil.copyfileobj(up.file, fh)
            saved.append(target)

        if not saved:
            return {
                "slices": [], "orphans": [], "skipped": skipped,
                "message": "没有能解析的文件。支持 " + "、".join(sorted(SUFFIXES)),
            }

        grouped, orphans = group_by_store(saved, model)
        slices = []
        failures = []
        for store_id, store_files in grouped.items():
            store = model.store(store_id)
            ing = ingest(list(store_files), model, [store.name])
            result = run(ing, store.platform)
            if not result.slices:
                failures.append({
                    "store": store.name,
                    "files": [f.name for f in store_files],
                    "reasons": [
                        f"{i.ref.label()}：{i.error or i.recognition.reason}"
                        for i in ing.unknown
                    ],
                })
                continue
            for (_s, _p), sl in sorted(result.slices.items(), key=lambda kv: (kv[0][1] or "")):
                slices.append(_as_dict(sl, store, model))

        return {
            "slices": slices,
            "orphans": [
                {"file": f.name, "suggest": _suggest(f.name)} for f in orphans
            ],
            "skipped": skipped,
            "failures": failures,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _suggest(filename: str) -> dict:
    """认不出归属时给个登记建议。只是提示，不参与计算。"""
    stem = Path(filename).stem
    for sep in ("-", "—", "_"):
        if sep in stem:
            name = stem.rsplit(sep, 1)[-1].strip()
            return {"store": name, "platform": guess_platform(name)}
    return {"store": "", "platform": ""}
