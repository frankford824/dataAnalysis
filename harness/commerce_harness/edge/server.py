"""Edge HTTP process: watch/read local inbox and push to core."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from .client import CoreUploadClient


def create_edge_app(
    *,
    core_base_url: str,
    inbox: Path,
    token: str | None = None,
) -> FastAPI:
    """Minimal edge surface: health + push-inbox to core."""

    inbox = inbox.resolve()
    app = FastAPI(title="finance-edge", version="0.1.0")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok\n"

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        try:
            with CoreUploadClient(
                core_base_url, token=token, timeout=2.0
            ) as client:
                remote = client.health()
        except Exception as exc:  # noqa: BLE001 - surface readiness failure
            raise HTTPException(
                status_code=503,
                detail=f"core unreachable: {exc}",
            ) from exc
        return {
            "status": "ready",
            "role": "edge",
            "inbox": str(inbox),
            "core": remote,
            "sharedFilesystem": False,
        }

    @app.get("/api/v1/edge/status")
    def status() -> dict[str, Any]:
        files = sorted(
            path.name
            for path in inbox.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )
        return {
            "role": "edge",
            "inbox": str(inbox),
            "fileCount": len(files),
            "files": files[:200],
            "coreBaseUrl": core_base_url.rstrip("/"),
        }

    @app.post("/api/v1/edge/push")
    def push_inbox() -> dict[str, Any]:
        if not inbox.is_dir():
            raise HTTPException(status_code=400, detail="inbox 目录不存在")
        receipts: list[dict[str, Any]] = []
        with CoreUploadClient(core_base_url, token=token) as client:
            for path in sorted(inbox.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                relative = path.relative_to(inbox).as_posix()
                receipt = client.upload_file(
                    path,
                    original_name=path.name,
                    source_uri=f"edge-inbox://{relative}",
                    metadata={"relative_path": relative},
                )
                receipts.append(
                    {
                        "path": relative,
                        "snapshotId": receipt.snapshot_id,
                        "contentSha256": receipt.content_sha256,
                        "byteSize": receipt.byte_size,
                        "reused": receipt.reused,
                    }
                )
        return {
            "uploaded": len(receipts),
            "receipts": receipts,
            "boundary": "http-only",
        }

    return app


def run_edge(
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    core_base_url: str | None = None,
    inbox: Path | None = None,
    token: str | None = None,
) -> None:
    import uvicorn

    core = core_base_url or os.environ.get("FA_CORE_BASE_URL", "http://core:8765")
    inbox_path = Path(
        inbox or os.environ.get("FA_EDGE_INBOX", "/edge-inbox")
    )
    inbox_path.mkdir(parents=True, exist_ok=True)
    app = create_edge_app(
        core_base_url=core,
        inbox=inbox_path,
        token=token or os.environ.get("FA_EDGE_TOKEN"),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


def dump_boundary_contract() -> str:
    """Machine-readable reminder of the edge/core split."""

    return json.dumps(
        {
            "shared_volumes": False,
            "shared_duckdb": False,
            "crossing": ["http", "object_store"],
            "forbidden": ["bind_mount_workbench_to_edge", "direct_duckdb_path"],
            "migration_acceptance": (
                "move core container; change FA_CORE_BASE_URL only; system still works"
            ),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
