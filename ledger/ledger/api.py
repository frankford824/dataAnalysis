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

from .cli import DEFAULT_MODEL, SUFFIXES, _as_dict, group_by_store
from .engine.runtime import ingest, run
from .model.loader import ModelError, load_model
from .model.schema import guess_platform
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


@app.get("/api/stores")
def stores() -> dict:
    model = _model()
    return {
        "stores": [
            {
                "id": s.id, "name": s.name, "platform": s.platform,
                "entity": s.entity, "archived": s.archived,
            }
            for s in model.stores
        ]
    }


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
