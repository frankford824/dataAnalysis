from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AuthSession, Enterprise, Store, UserAccount, utcnow


ROLES = {"platform_admin", "admin", "implementer", "analyst", "viewer"}
WRITE_ROLES = {"platform_admin", "admin", "implementer"}
APPROVE_ROLES = {"platform_admin", "admin"}
USER_ADMIN_ROLES = {"platform_admin", "admin"}
DATA_CONFIG_ROLES = {"platform_admin", "admin", "implementer"}
PROBLEM_ROLES = {"platform_admin", "admin", "implementer"}
IDENTITY_HEADERS = {"x-enterprise-id", "x-user-id", "x-role", "x-store-ids"}
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
SESSION_COOKIE = "commerce_session"


@dataclass(frozen=True)
class RequestContext:
    enterprise_id: str | None
    user_id: str
    role: str
    store_ids: frozenset[str] | None = None
    session_id: str | None = None

    def require(self, allowed: set[str]) -> None:
        if self.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")

    def allows_store(self, store_id: str | None) -> bool:
        return store_id is None or self.store_ids is None or store_id in self.store_ids


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 256:
        raise HTTPException(status_code=422, detail="password must contain 12 to 256 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: UserAccount, *, commit: bool = True) -> tuple[str, AuthSession]:
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    session = AuthSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        expires_at=utcnow() + timedelta(minutes=settings.session_ttl_minutes),
    )
    user.last_login_at = utcnow()
    db.add(session)
    if commit:
        db.commit()
        db.refresh(session)
    else:
        db.flush()
    return token, session


def revoke_session(db: Session, session_id: str | None) -> None:
    if not session_id:
        return
    session = db.get(AuthSession, session_id)
    if session and session.revoked_at is None:
        session.revoked_at = utcnow()
        db.commit()


def _trusted_proxy_user(request: Request, db: Session) -> UserAccount | None:
    settings = get_settings()
    proxy_headers = [name for name in request.headers if name.lower().startswith("x-trusted-proxy-")]
    if not proxy_headers:
        return None
    if not settings.trusted_proxy_enabled:
        raise HTTPException(status_code=400, detail="trusted proxy authentication is disabled")
    if not settings.trusted_proxy_secret or len(settings.trusted_proxy_secret) < 32:
        raise HTTPException(status_code=503, detail="trusted proxy authentication is not securely configured")
    client_ip = request.client.host if request.client else ""
    allowlist = {value.strip() for value in settings.trusted_proxy_ips.split(",") if value.strip()}
    if client_ip not in allowlist:
        raise HTTPException(status_code=403, detail="untrusted proxy address")
    user_id = request.headers.get("X-Trusted-Proxy-User", "")
    timestamp = request.headers.get("X-Trusted-Proxy-Timestamp", "")
    signature = request.headers.get("X-Trusted-Proxy-Signature", "")
    try:
        if abs(time.time() - int(timestamp)) > 60:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="expired trusted proxy assertion") from exc
    message = f"{user_id}\n{timestamp}".encode()
    expected = hmac.new(settings.trusted_proxy_secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid trusted proxy signature")
    return db.get(UserAccount, user_id)


def _current_user(request: Request, db: Session) -> tuple[UserAccount, AuthSession | None]:
    direct_headers = IDENTITY_HEADERS.intersection({name.lower() for name in request.headers})
    if direct_headers:
        raise HTTPException(status_code=400, detail="client-supplied identity headers are not accepted")
    proxy_user = _trusted_proxy_user(request, db)
    if proxy_user:
        return proxy_user, None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    now = utcnow()
    if not session or session.revoked_at is not None or _aware(session.expires_at) <= now:
        raise HTTPException(status_code=401, detail="session is invalid or expired")
    user = db.get(UserAccount, session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="account no longer exists")
    session.last_seen_at = now
    return user, session


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_context(request: Request, db: Session = Depends(get_db)) -> RequestContext:
    user, session = _current_user(request, db)
    now = utcnow()
    if user.status not in {"active", "approved", "published"} or user.archived_at:
        raise HTTPException(status_code=403, detail="account is inactive")
    if user.effective_from and _aware(user.effective_from) > now:
        raise HTTPException(status_code=403, detail="account is not yet effective")
    if user.effective_to and _aware(user.effective_to) <= now:
        raise HTTPException(status_code=403, detail="account is no longer effective")
    if user.must_change_password and request.url.path not in {
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
        "/api/v1/auth/change-password",
    }:
        raise HTTPException(status_code=403, detail={"code": "password_change_required", "message": "首次登录必须先设置新密码"})
    enterprise_id = user.enterprise_id
    act_as = request.headers.get("X-Act-As-Enterprise-ID")
    if act_as:
        if user.role != "platform_admin":
            raise HTTPException(status_code=403, detail="only a platform administrator can change enterprise context")
        if not db.get(Enterprise, act_as):
            raise HTTPException(status_code=404, detail="enterprise not found")
        enterprise_id = act_as
    scopes = frozenset(user.store_ids) if user.store_ids and not act_as else None
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('app.current_enterprise_id', :enterprise_id, true)"), {"enterprise_id": enterprise_id})
    return RequestContext(enterprise_id, user.id, user.role, scopes, session.id if session else None)


def scoped_store_ids(db: Session, ctx: RequestContext, requested: list[str] | set[str] | tuple[str, ...] | None = None) -> set[str]:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    stores = db.scalars(select(Store).where(Store.enterprise_id == ctx.enterprise_id, Store.archived_at.is_(None))).all()
    available = {store.id for store in stores}
    permitted_logical = {store.logical_id for store in stores} if ctx.store_ids is None else {store.logical_id for store in stores if store.id in ctx.store_ids or store.logical_id in ctx.store_ids}
    permitted = {store.id for store in stores if store.logical_id in permitted_logical}
    if requested:
        wanted = set(requested)
        requested_logical = {store.logical_id for store in stores if store.id in wanted or store.logical_id in wanted}
        recognized = {value for value in wanted if any(store.id == value or store.logical_id == value for store in stores)}
        if recognized != wanted or not requested_logical.issubset(permitted_logical):
            raise HTTPException(status_code=403, detail="requested store is outside the account scope")
        return {store.id for store in stores if store.logical_id in requested_logical}
    return permitted


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
