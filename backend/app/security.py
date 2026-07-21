from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet
from fastapi import Header, HTTPException, status

from .config import get_settings


ROLES = {"platform_admin", "admin", "implementer", "analyst", "viewer"}
WRITE_ROLES = {"platform_admin", "admin", "implementer"}
APPROVE_ROLES = {"platform_admin", "admin", "implementer"}


@dataclass(frozen=True)
class RequestContext:
    enterprise_id: str | None
    user_id: str
    role: str

    def require(self, allowed: set[str]) -> None:
        if self.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")


def get_context(
    x_enterprise_id: str | None = Header(default=None, alias="X-Enterprise-ID"),
    x_user_id: str = Header(default="local-admin", alias="X-User-ID"),
    x_role: str = Header(default="admin", alias="X-Role"),
) -> RequestContext:
    if x_role not in ROLES:
        raise HTTPException(status_code=403, detail="unknown role")
    if not x_enterprise_id and x_role != "platform_admin":
        raise HTTPException(status_code=400, detail="X-Enterprise-ID is required")
    return RequestContext(x_enterprise_id, x_user_id, x_role)


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
