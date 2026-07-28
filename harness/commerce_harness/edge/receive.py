"""Core-side endpoints that accept edge uploads over HTTP only."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.snapshot.reader import Reader, ReaderMetadata, SourceAvailability
from commerce_harness.snapshot.store import SnapshotStore
from commerce_harness.workbench import WorkbenchPaths

from .auth import anonymous_allowed, configured_token, require_edge_token

MAX_UPLOAD_BYTES_ENV = "FA_EDGE_MAX_UPLOAD_BYTES"
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024


def _max_upload_bytes() -> int:
    raw = os.environ.get(MAX_UPLOAD_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


class _StagedUploadReader(Reader):
    """Reader over an already-received upload staged on core's own disk.

    The reported uri stays the customer-side source uri so the snapshot keeps
    pointing at where the file really came from, not at core's temp file.
    """

    def __init__(self, path: Path, *, uri: str, size: int) -> None:
        self._path = path
        self._metadata = ReaderMetadata(
            uri=uri,
            size=size,
            modified_ns=None,
            availability=SourceAvailability.ONLINE,
            stable=True,
        )

    def stat(self) -> ReaderMetadata:
        return self._metadata

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        with self._path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk


def build_edge_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/edge", tags=["edge"])

    @router.get("/health")
    def edge_health() -> dict[str, Any]:
        return {
            "role": "core",
            "acceptsUploads": configured_token() is not None or anonymous_allowed(),
            "requiresToken": configured_token() is not None,
            "sharedFilesystem": False,
            "maxUploadBytes": _max_upload_bytes(),
            "workbench": str(workbench.root),
        }

    @router.get("/snapshots/{content_sha256}", dependencies=[Depends(require_edge_token)])
    def lookup_snapshot(content_sha256: str) -> dict[str, Any]:
        digest = content_sha256.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise HTTPException(status_code=400, detail="非法内容哈希")
        store = SnapshotStore(workbench.snapshots)
        object_path = store.root / "objects" / "sha256" / digest[:2] / digest
        if not object_path.is_file():
            raise HTTPException(status_code=404, detail="快照不存在")
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            row = database.execute(
                """
                SELECT snapshot_id, content_sha256, byte_size, original_name
                FROM source_snapshot
                WHERE content_sha256 = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                [digest],
            ).fetchone()
        if row is None:
            # Object exists but is not yet registered — treat as missing so
            # edge re-uploads and registration completes.
            raise HTTPException(status_code=404, detail="快照未登记")
        return {
            "snapshotId": row[0],
            "contentSha256": row[1],
            "byteSize": int(row[2]),
            "originalName": row[3],
            "reused": True,
        }

    @router.post("/snapshots", dependencies=[Depends(require_edge_token)])
    async def receive_snapshot(
        # FastAPI reads the form contract from these call-in-default markers.
        file: UploadFile = File(...),  # noqa: B008
        content_sha256: str = Form(...),
        original_name: str = Form(...),
        source_uri: str = Form(...),
        media_type: str = Form("application/octet-stream"),
        metadata_json: str | None = Form(None),
    ) -> dict[str, Any]:
        digest = content_sha256.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise HTTPException(status_code=400, detail="非法内容哈希")
        metadata = _parse_metadata(metadata_json)

        staging_dir = workbench.root / "edge-uploads"
        staging_dir.mkdir(parents=True, exist_ok=True)
        limit = _max_upload_bytes()
        # Stream to disk instead of buffering the whole upload in memory: one
        # large file must not be able to take core down.
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed manually
            dir=staging_dir, prefix="upload_", suffix=".part", delete=False
        )
        staged = Path(handle.name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with handle:
                while chunk := await file.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单个上传超过上限 {limit} 字节",
                        )
                    hasher.update(chunk)
                    handle.write(chunk)
            if hasher.hexdigest() != digest:
                raise HTTPException(
                    status_code=400,
                    detail="上传内容与声明哈希不一致",
                )

            store = SnapshotStore(workbench.snapshots)
            object_path = store.root / "objects" / "sha256" / digest[:2] / digest
            reused = object_path.is_file()
            manifest = store.capture(
                _StagedUploadReader(staged, uri=source_uri, size=size),
                original_name=original_name,
                media_type=media_type,
            )
        finally:
            staged.unlink(missing_ok=True)

        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            already = database.execute(
                """
                SELECT snapshot_id FROM source_snapshot
                WHERE content_sha256 = ?
                LIMIT 1
                """,
                [manifest.content_sha256],
            ).fetchone()
            if already is None:
                database.register_snapshot(manifest)
                snapshot_id = manifest.snapshot_id
            else:
                snapshot_id = str(already[0])
                reused = True
        return {
            "snapshotId": snapshot_id,
            "contentSha256": manifest.content_sha256,
            "byteSize": manifest.byte_size,
            "reused": reused,
            "boundary": "http-only",
            "edgeMetadata": metadata,
            "uploadId": f"edge_{uuid.uuid4().hex}",
        }

    return router


def _parse_metadata(metadata_json: str | None) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        loaded = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="metadata_json 非法") from exc
    if not isinstance(loaded, dict):
        raise HTTPException(status_code=400, detail="metadata 必须是对象")
    return loaded


def assert_no_shared_workbench(edge_root: Path, core_root: Path) -> None:
    """Migration guard: edge and core must not share a workbench path."""

    if edge_root.resolve() == core_root.resolve():
        raise RuntimeError(
            "edge 与 core 不得共享 workbench 路径；"
            "跨边界只允许 HTTP / 对象存储"
        )
