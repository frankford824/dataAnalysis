from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce_harness.config import load_config
from commerce_harness.edge.auth import ALLOW_ANONYMOUS_ENV, TOKEN_ENV
from commerce_harness.edge.receive import (
    MAX_UPLOAD_BYTES_ENV,
    assert_no_shared_workbench,
    build_edge_router,
)
from commerce_harness.edge.server import create_edge_app
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize

_TOKEN = "edge-secret-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _boundary_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, _TOKEN)
    monkeypatch.delenv(ALLOW_ANONYMOUS_ENV, raising=False)


def _core_edge_client(tmp_path: Path) -> tuple[TestClient, Path]:
    core_root = tmp_path / "core"
    config = load_config(workspace=core_root)
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
    app = FastAPI()
    app.include_router(build_edge_router(workbench))
    return TestClient(app), workbench.root


def _upload(
    core: TestClient,
    payload: bytes,
    *,
    name: str = "orders.csv",
    headers: dict[str, str] | None = None,
    digest: str | None = None,
):
    return core.post(
        "/api/v1/edge/snapshots",
        data={
            "content_sha256": digest or hashlib.sha256(payload).hexdigest(),
            "original_name": name,
            "source_uri": f"edge-inbox://{name}",
            "media_type": "text/csv",
        },
        files={"file": (name, payload, "text/csv")},
        headers=_AUTH if headers is None else headers,
    )


def test_edge_upload_reaches_core_over_http(tmp_path: Path) -> None:
    edge_inbox = tmp_path / "edge-inbox"
    edge_inbox.mkdir()
    core, core_root = _core_edge_client(tmp_path)
    assert_no_shared_workbench(edge_inbox, core_root)

    sample = edge_inbox / "orders.csv"
    sample.write_text("订单号,金额\nA-1,10.25\n", encoding="utf-8")

    assert core.get("/api/v1/edge/health").json()["sharedFilesystem"] is False
    response = _upload(core, sample.read_bytes())
    assert response.status_code == 200
    body = response.json()
    assert body["byteSize"] > 0
    assert body["boundary"] == "http-only"
    again = core.get(
        f"/api/v1/edge/snapshots/{body['contentSha256']}", headers=_AUTH
    )
    assert again.status_code == 200
    assert again.json()["reused"] is True


def test_upload_without_token_is_rejected(tmp_path: Path) -> None:
    core, _ = _core_edge_client(tmp_path)
    response = _upload(core, b"payload", headers={})
    assert response.status_code == 401
    wrong = _upload(core, b"payload", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


def test_upload_refuses_when_core_has_no_token_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    core, _ = _core_edge_client(tmp_path)
    response = _upload(core, b"payload", headers={})
    assert response.status_code == 503
    assert TOKEN_ENV in response.json()["detail"]


def test_upload_allows_explicit_anonymous_single_machine_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv(ALLOW_ANONYMOUS_ENV, "1")
    core, _ = _core_edge_client(tmp_path)
    assert _upload(core, b"payload", headers={}).status_code == 200


def test_upload_rejects_digest_mismatch(tmp_path: Path) -> None:
    core, _ = _core_edge_client(tmp_path)
    response = _upload(core, b"payload", digest="0" * 64)
    assert response.status_code == 400
    assert "哈希" in response.json()["detail"]


def test_upload_rejects_oversized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_UPLOAD_BYTES_ENV, "16")
    core, core_root = _core_edge_client(tmp_path)
    response = _upload(core, b"x" * 4096)
    assert response.status_code == 413
    staging = core_root / "edge-uploads"
    leftovers = list(staging.glob("*.part")) if staging.is_dir() else []
    assert leftovers == []


def test_edge_readyz_requires_reachable_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    class BoomClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> BoomClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def health(self) -> dict[str, object]:
            raise ConnectionError("core down")

    monkeypatch.setattr(
        "commerce_harness.edge.server.CoreUploadClient",
        BoomClient,
    )
    app = create_edge_app(
        core_base_url="http://core.invalid",
        inbox=inbox,
    )
    client = TestClient(app)
    assert client.get("/healthz").text == "ok\n"
    assert client.get("/readyz").status_code == 503


def test_assert_no_shared_workbench_rejects_same_path(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    with pytest.raises(RuntimeError, match="不得共享"):
        assert_no_shared_workbench(shared, shared)
