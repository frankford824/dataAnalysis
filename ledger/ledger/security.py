"""Authentication and role authorization for the HTTP boundary.

Without an auth file the app remains convenient for its original single-machine
use, but only loopback clients are trusted. Exposing it on a LAN therefore fails
closed until explicit bearer-token identities are configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path


ROLES = {"viewer": 0, "operator": 1, "finance": 2, "admin": 3}
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    role: str


class SecurityError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def auth_file() -> Path | None:
    value = os.environ.get("LEDGER_AUTH_FILE", "").strip()
    return Path(value).expanduser() if value else None


def authenticate(host: str, authorization: str = "") -> Principal:
    path = auth_file()
    if path is None:
        if host in LOCAL_HOSTS:
            return Principal("本机操作员", "admin")
        raise SecurityError(
            403,
            "当前只允许本机访问。要从局域网使用，请配置 LEDGER_AUTH_FILE 和 Bearer token。",
        )

    if not path.is_file():
        raise SecurityError(503, f"鉴权文件不存在：{path}")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise SecurityError(401, "需要 Bearer token")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityError(503, f"鉴权文件读不了：{exc}") from exc

    wanted = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for entry in raw.get("users", []):
        role = str(entry.get("role", ""))
        digest = str(entry.get("token_sha256", ""))
        if role in ROLES and digest and hmac.compare_digest(digest, wanted):
            name = str(entry.get("name", "")).strip()
            if not name:
                raise SecurityError(503, "鉴权文件里的用户缺少 name")
            return Principal(name, role)
    raise SecurityError(401, "token 无效")


def required_role(method: str, path: str) -> str:
    """Central policy; unrecognized writes default to admin, never to public."""
    method = method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "viewer"
    if path.endswith("/close") or path.endswith("/reopen"):
        return "finance"
    if path == "/api/onboard":
        return "admin"
    if path == "/api/stores" or (path.startswith("/api/stores/") and method == "PATCH"):
        return "admin"
    if path == "/api/upload" or path == "/api/onboard/try":
        return "operator"
    if path.endswith("/recompute") or (path.endswith("/files") and method == "DELETE"):
        return "operator"
    return "admin"


def authorize(principal: Principal, method: str, path: str) -> None:
    needed = required_role(method, path)
    if ROLES[principal.role] < ROLES[needed]:
        raise SecurityError(403, f"此操作需要 {needed} 权限；当前身份是 {principal.role}")
