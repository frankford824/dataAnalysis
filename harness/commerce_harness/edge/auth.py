"""Shared-secret guard for the edge → core boundary.

The upload endpoints write into the content-addressed store and the DuckDB
memory, so they are the one place where an unauthenticated caller could inject
evidence. There is no ambient trust here: without a configured token core
refuses uploads instead of accepting them from anyone who can reach the port.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

TOKEN_ENV = "FA_EDGE_TOKEN"
ALLOW_ANONYMOUS_ENV = "FA_EDGE_ALLOW_ANONYMOUS"
_TRUTHY = {"1", "true", "yes", "on"}


def configured_token() -> str | None:
    return os.environ.get(TOKEN_ENV, "").strip() or None


def anonymous_allowed() -> bool:
    return os.environ.get(ALLOW_ANONYMOUS_ENV, "").strip().lower() in _TRUTHY


def _presented(authorization: str | None, header_token: str | None) -> str | None:
    if header_token and header_token.strip():
        return header_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def require_edge_token(
    authorization: str | None = Header(default=None),
    x_fa_edge_token: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: reject unauthenticated boundary calls."""
    expected = configured_token()
    if expected is None:
        if anonymous_allowed():
            return
        raise HTTPException(
            status_code=503,
            detail=(
                f"核心尚未配置边界令牌 {TOKEN_ENV}，已拒绝上传。"
                f"单机可信部署可显式设置 {ALLOW_ANONYMOUS_ENV}=1。"
            ),
        )
    presented = _presented(authorization, x_fa_edge_token)
    if presented is None or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="边界令牌不正确")
