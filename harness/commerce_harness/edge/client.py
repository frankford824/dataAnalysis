"""HTTP client that edge uses to push snapshots into core."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    snapshot_id: str
    content_sha256: str
    byte_size: int
    reused: bool


class CoreUploadClient:
    """Push content-addressed blobs to core over HTTP only."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        token: str | None = None,
    ) -> None:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CoreUploadClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        response = self._client.get("api/v1/edge/health")
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def upload_file(
        self,
        path: Path,
        *,
        original_name: str | None = None,
        source_uri: str | None = None,
        media_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UploadReceipt:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        content_sha256 = digest.hexdigest()
        probe = self._client.get(f"api/v1/edge/snapshots/{content_sha256}")
        if probe.status_code == 200:
            body = probe.json()
            return UploadReceipt(
                snapshot_id=str(body["snapshotId"]),
                content_sha256=content_sha256,
                byte_size=int(body["byteSize"]),
                reused=True,
            )
        with path.open("rb") as handle:
            files = {
                "file": (
                    original_name or path.name,
                    handle,
                    media_type or "application/octet-stream",
                )
            }
            data = {
                "content_sha256": content_sha256,
                "original_name": original_name or path.name,
                "source_uri": source_uri or f"edge://{path.name}",
                "media_type": media_type or "application/octet-stream",
            }
            if metadata:
                import json

                data["metadata_json"] = json.dumps(
                    metadata, ensure_ascii=False, sort_keys=True
                )
            response = self._client.post(
                "api/v1/edge/snapshots",
                data=data,
                files=files,
            )
            response.raise_for_status()
            body = response.json()
            return UploadReceipt(
                snapshot_id=str(body["snapshotId"]),
                content_sha256=content_sha256,
                byte_size=int(body["byteSize"]),
                reused=bool(body.get("reused", False)),
            )

    def upload_bytes(
        self,
        content: bytes,
        *,
        original_name: str,
        source_uri: str,
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> UploadReceipt:
        content_sha256 = hashlib.sha256(content).hexdigest()
        probe = self._client.get(f"api/v1/edge/snapshots/{content_sha256}")
        if probe.status_code == 200:
            body = probe.json()
            return UploadReceipt(
                snapshot_id=str(body["snapshotId"]),
                content_sha256=content_sha256,
                byte_size=int(body["byteSize"]),
                reused=True,
            )
        import json

        files = {"file": (original_name, content, media_type)}
        data = {
            "content_sha256": content_sha256,
            "original_name": original_name,
            "source_uri": source_uri,
            "media_type": media_type,
        }
        if metadata:
            data["metadata_json"] = json.dumps(
                metadata, ensure_ascii=False, sort_keys=True
            )
        response = self._client.post(
            "api/v1/edge/snapshots",
            data=data,
            files=files,
        )
        response.raise_for_status()
        body = response.json()
        return UploadReceipt(
            snapshot_id=str(body["snapshotId"]),
            content_sha256=content_sha256,
            byte_size=int(body["byteSize"]),
            reused=bool(body.get("reused", False)),
        )

    def absolute(self, path: str) -> str:
        return urljoin(str(self._client.base_url), path.lstrip("/"))
