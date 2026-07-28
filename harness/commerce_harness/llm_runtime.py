from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

LlmProtocol = Literal["openai_compatible", "anthropic"]
DiscoveryProtocol = Literal["auto", "openai_compatible", "anthropic"]

_SUPPORTED_PROTOCOLS = frozenset({"openai_compatible", "anthropic"})
_MAX_TIMEOUT_SECONDS = 30.0
_DEFAULT_TIMEOUT_SECONDS = _MAX_TIMEOUT_SECONDS
_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_MAX_TOKENS = 1_024
_MAX_COMPLETION_TOKENS = 4_096


class LlmRuntimeError(RuntimeError):
    """Safe public error whose message never contains credentials or response bodies."""


@dataclass(frozen=True, slots=True)
class RuntimeLlmConfig:
    enabled: bool
    protocol: LlmProtocol
    base_url: str
    selected_model: str
    api_key: str = field(repr=False)
    reviewer_model: str = ""
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = 1
    max_tokens: int = _DEFAULT_MAX_TOKENS

    @property
    def completion_supported(self) -> bool:
        return self.protocol in _SUPPORTED_PROTOCOLS


@dataclass(frozen=True, slots=True)
class PublicLlmStatus:
    configured: bool
    enabled: bool
    protocol: LlmProtocol | None
    base_url: str
    selected_model: str
    reviewer_model: str
    key_configured: bool
    completion_supported: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    protocol: LlmProtocol
    base_url: str
    models: tuple[str, ...]
    completion_supported: bool


def validate_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise LlmRuntimeError("模型服务地址无效") from exc
    if parts.scheme not in {"http", "https"}:
        raise LlmRuntimeError("模型服务地址仅支持 http 或 https")
    if not parts.hostname or parts.username or parts.password:
        raise LlmRuntimeError("模型服务地址不得为空或包含登录凭据")
    if parts.query or parts.fragment:
        raise LlmRuntimeError("模型服务地址不得包含查询参数或片段")
    netloc = parts.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _models_endpoint(base_url: str) -> str:
    return f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"


def _safe_protocol(value: str) -> LlmProtocol:
    if value not in _SUPPORTED_PROTOCOLS:
        raise LlmRuntimeError("不支持的模型协议")
    return cast(LlmProtocol, value)


def _safe_timeout(value: float) -> float:
    if not 0.1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise LlmRuntimeError("模型请求超时必须在 0.1 到 15 秒之间")
    return value


def _safe_max_tokens(value: int) -> int:
    if not 1 <= value <= _MAX_COMPLETION_TOKENS:
        raise LlmRuntimeError("模型最大输出 token 必须在 1 到 4096 之间")
    return value


def discover_models(
    *,
    protocol: DiscoveryProtocol | str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> ModelDiscoveryResult:
    safe_base_url = validate_base_url(base_url)
    if str(protocol) == "auto":
        hostname = (urlsplit(safe_base_url).hostname or "").lower()
        order: tuple[LlmProtocol, LlmProtocol] = (
            ("anthropic", "openai_compatible")
            if hostname == "anthropic.com" or hostname.endswith(".anthropic.com")
            else ("openai_compatible", "anthropic")
        )
        for candidate in order:
            try:
                return discover_models(
                    protocol=candidate,
                    base_url=safe_base_url,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                    transport=transport,
                )
            except LlmRuntimeError:
                continue
        raise LlmRuntimeError("无法自动识别模型服务协议，请核对地址、密钥和服务状态")

    safe_protocol = _safe_protocol(str(protocol))
    timeout = _safe_timeout(timeout_seconds)
    if not api_key:
        raise LlmRuntimeError("需要提供模型服务密钥")
    if not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES:
        raise LlmRuntimeError("模型列表响应上限无效")

    if safe_protocol == "openai_compatible":
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    else:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }

    timeout_config = httpx.Timeout(timeout)
    try:
        with (
            httpx.Client(
                timeout=timeout_config,
                follow_redirects=False,
                transport=transport,
            ) as client,
            client.stream(
                "GET",
                _models_endpoint(safe_base_url),
                headers=headers,
            ) as response,
        ):
            if response.is_redirect:
                raise LlmRuntimeError("模型服务返回了重定向，已拒绝跟随")
            if response.status_code >= 400:
                raise LlmRuntimeError(f"模型服务鉴权或请求失败（HTTP {response.status_code}）")
            advertised_size = response.headers.get("content-length")
            if advertised_size and int(advertised_size) > max_response_bytes:
                raise LlmRuntimeError("模型列表响应超过安全上限")
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > max_response_bytes:
                    raise LlmRuntimeError("模型列表响应超过安全上限")
                chunks.append(chunk)
    except LlmRuntimeError:
        raise
    except Exception as exc:
        raise LlmRuntimeError("无法连接模型服务或解析其响应") from exc

    try:
        payload = json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LlmRuntimeError("模型服务未返回有效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise LlmRuntimeError("模型服务返回的模型列表格式不受支持")
    models = tuple(
        sorted(
            {
                str(item["id"]).strip()
                for item in payload["data"]
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
        )
    )
    if not models:
        raise LlmRuntimeError("模型服务未返回可用模型")
    return ModelDiscoveryResult(
        protocol=safe_protocol,
        base_url=safe_base_url,
        models=models,
        completion_supported=True,
    )


class RuntimeLlmStore:
    """Stores runtime provider metadata and secrets outside the source tree.

    A versioned secret file is written before the metadata pointer is atomically
    replaced. Readers therefore observe either the complete old configuration or
    the complete new one, never a half-written key/configuration pair.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.runtime_dir = self.root / "runtime"
        self.secrets_dir = self.root / "secrets"
        self.config_path = self.runtime_dir / "llm-provider.json"
        self.activity_path = self.runtime_dir / "llm-last-activity.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RuntimeLlmStore:
        source = os.environ if env is None else env
        root = source.get("FA_LLM_RUNTIME_ROOT") or source.get("FA_WORKBENCH_ROOT")
        return cls(root or Path("~/fa-workbench"))

    def save(
        self,
        *,
        protocol: LlmProtocol | str,
        base_url: str,
        api_key: str,
        selected_model: str,
        reviewer_model: str = "",
        enabled: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 1,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> RuntimeLlmConfig:
        safe_protocol = _safe_protocol(str(protocol))
        safe_base_url = validate_base_url(base_url)
        timeout = _safe_timeout(timeout_seconds)
        safe_max_tokens = _safe_max_tokens(max_tokens)
        model = selected_model.strip()
        reviewer = reviewer_model.strip()
        if not api_key:
            raise LlmRuntimeError("需要提供模型服务密钥")
        if not model:
            raise LlmRuntimeError("需要选择模型")
        if len(reviewer) > 300:
            raise LlmRuntimeError("复核模型名称超过安全上限")
        if not 1 <= max_attempts <= 3:
            raise LlmRuntimeError("模型请求重试次数必须在 1 到 3 之间")

        self._ensure_directories()
        secret_name = f"llm-api-key-{uuid.uuid4().hex}"
        secret_path = self.secrets_dir / secret_name
        self._atomic_write(secret_path, api_key.encode("utf-8"), mode=0o600)
        metadata = {
            "schema_version": 2,
            "enabled": bool(enabled),
            "protocol": safe_protocol,
            "base_url": safe_base_url,
            "selected_model": model,
            "reviewer_model": reviewer,
            "timeout_seconds": timeout,
            "max_attempts": max_attempts,
            "max_tokens": safe_max_tokens,
            "secret_ref": secret_name,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        previous_ref = self._read_secret_ref()
        try:
            self._atomic_write(
                self.config_path,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                mode=0o600,
            )
        except Exception:
            secret_path.unlink(missing_ok=True)
            raise
        if previous_ref and previous_ref != secret_name:
            self._safe_secret_path(previous_ref).unlink(missing_ok=True)
        return RuntimeLlmConfig(
            enabled=bool(enabled),
            protocol=safe_protocol,
            base_url=safe_base_url,
            selected_model=model,
            api_key=api_key,
            reviewer_model=reviewer,
            timeout_seconds=timeout,
            max_attempts=max_attempts,
            max_tokens=safe_max_tokens,
        )

    def load(self) -> RuntimeLlmConfig | None:
        if not self.config_path.exists():
            return None
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
                raise ValueError("invalid runtime schema")
            protocol = _safe_protocol(str(payload["protocol"]))
            base_url = validate_base_url(str(payload["base_url"]))
            selected_model = str(payload["selected_model"]).strip()
            reviewer_model = str(payload.get("reviewer_model", "")).strip()
            timeout = _safe_timeout(float(payload.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)))
            max_attempts = int(payload.get("max_attempts", 1))
            max_tokens = _safe_max_tokens(
                int(payload.get("max_tokens", _DEFAULT_MAX_TOKENS))
            )
            if (
                not selected_model
                or len(reviewer_model) > 300
                or not 1 <= max_attempts <= 3
            ):
                raise ValueError("invalid runtime model configuration")
            secret_path = self._safe_secret_path(str(payload["secret_ref"]))
            api_key = secret_path.read_text(encoding="utf-8")
            if not api_key:
                raise ValueError("empty runtime secret")
            return RuntimeLlmConfig(
                enabled=bool(payload.get("enabled", False)),
                protocol=protocol,
                base_url=base_url,
                selected_model=selected_model,
                api_key=api_key,
                reviewer_model=reviewer_model,
                timeout_seconds=timeout,
                max_attempts=max_attempts,
                max_tokens=max_tokens,
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise LlmRuntimeError("运行时模型配置不可用") from exc

    def public_status(self) -> PublicLlmStatus:
        if not self.config_path.exists():
            return PublicLlmStatus(
                configured=False,
                enabled=False,
                protocol=None,
                base_url="",
                selected_model="",
                reviewer_model="",
                key_configured=False,
                completion_supported=False,
                detail="未配置模型服务；确定性流程正常可用",
            )
        try:
            config = self.load()
        except LlmRuntimeError:
            return PublicLlmStatus(
                configured=False,
                enabled=False,
                protocol=None,
                base_url="",
                selected_model="",
                reviewer_model="",
                key_configured=False,
                completion_supported=False,
                detail="模型配置不可用；已安全降级，确定性流程不受影响",
            )
        if config is None:
            raise AssertionError("runtime configuration disappeared while reading")
        detail = (
            "已启用"
            if config.enabled and config.completion_supported
            else "模型服务已禁用；确定性流程正常可用"
        )
        return PublicLlmStatus(
            configured=True,
            enabled=config.enabled,
            protocol=config.protocol,
            base_url=config.base_url,
            selected_model=config.selected_model,
            reviewer_model=config.reviewer_model,
            key_configured=True,
            completion_supported=config.completion_supported,
            detail=detail,
        )

    def disable(self) -> None:
        config = self.load()
        if config is None:
            return
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["enabled"] = False
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._atomic_write(
            self.config_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            mode=0o600,
        )

    def record_activity(
        self,
        *,
        status: Literal["pending", "ok", "error", "disabled"],
        purpose: str,
        model: str,
        message: str,
        request_id: str | None = None,
    ) -> None:
        safe_purpose = purpose.strip()[:80]
        safe_model = model.strip()[:300]
        safe_message = " ".join(message.strip().split())[:500]
        if not safe_purpose or not safe_message:
            raise LlmRuntimeError("模型运行状态记录无效")
        self._ensure_directories()
        payload = {
            "schema_version": 1,
            "status": status,
            "purpose": safe_purpose,
            "model": safe_model,
            "message": safe_message,
            "request_id": request_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._atomic_write(
            self.activity_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            mode=0o600,
        )

    def last_activity(self) -> dict[str, str | None] | None:
        if not self.activity_path.exists():
            return None
        try:
            payload = json.loads(self.activity_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                return None
            status = str(payload.get("status", ""))
            if status not in {"pending", "ok", "error", "disabled"}:
                return None
            return {
                "status": status,
                "purpose": str(payload.get("purpose", "")) or None,
                "model": str(payload.get("model", "")) or None,
                "message": str(payload.get("message", "")) or None,
                "request_id": str(payload.get("request_id", "")) or None,
                "updated_at": str(payload.get("updated_at", "")) or None,
            }
        except (OSError, ValueError):
            return None

    def _ensure_directories(self) -> None:
        for directory in (self.runtime_dir, self.secrets_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    def _read_secret_ref(self) -> str | None:
        if not self.config_path.exists():
            return None
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            value = payload.get("secret_ref")
            return str(value) if value else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _safe_secret_path(self, secret_ref: str) -> Path:
        if not secret_ref or Path(secret_ref).name != secret_ref:
            raise LlmRuntimeError("运行时模型配置不可用")
        path = (self.secrets_dir / secret_ref).resolve()
        if path.parent != self.secrets_dir:
            raise LlmRuntimeError("运行时模型配置不可用")
        return path

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(content)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary_path, path)
            path.chmod(mode)
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
            raise
