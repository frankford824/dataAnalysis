from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from commerce_harness.llm_runtime import (
    LlmProtocol,
    LlmRuntimeError,
    RuntimeLlmStore,
    validate_base_url,
)

from .models import GatewayResult
from .redaction import MandatoryRedactor

_MAX_GATEWAY_RESPONSE_BYTES = 1_048_576
_MAX_COMPLETION_TOKENS = 4_096
_MAX_GATEWAY_TIMEOUT_SECONDS = 30.0


def business_failure_reason(error: Exception, *, api_key: str = "") -> str:
    message = str(error).strip() or error.__class__.__name__
    if api_key:
        message = message.replace(f"Bearer {api_key}", "[redacted]")
        message = message.replace(api_key, "[redacted]")
    normalized = message.casefold()
    if "timed out" in normalized or "timeout" in normalized:
        return "模型响应超时"
    if "http 401" in normalized or "http 403" in normalized:
        return "模型密钥或服务权限校验失败"
    if "http 404" in normalized:
        return "模型调用地址不可用"
    if "http 429" in normalized:
        return "模型服务当前限流，请稍后重试"
    if any(f"http {status}" in normalized for status in range(500, 600)):
        return "模型服务暂时不可用"
    if "gateway request failed" in normalized:
        return "无法连接模型服务"
    return message[:300]


class GatewayTransport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class UrllibTransport:
    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            del req, fp, code, msg, headers, newurl
            return None

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(self._NoRedirectHandler())
            with opener.open(
                request,
                timeout=min(timeout_seconds, _MAX_GATEWAY_TIMEOUT_SECONDS),
            ) as response:
                advertised_size = response.headers.get("Content-Length")
                if advertised_size and int(advertised_size) > _MAX_GATEWAY_RESPONSE_BYTES:
                    raise RuntimeError("gateway response exceeds the safety limit")
                raw = response.read(_MAX_GATEWAY_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_GATEWAY_RESPONSE_BYTES:
                    raise RuntimeError("gateway response exceeds the safety limit")
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise RuntimeError("gateway response must be a JSON object")
                return cast(Mapping[str, Any], decoded)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"gateway request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("gateway request failed") from exc


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = _MAX_GATEWAY_TIMEOUT_SECONDS
    max_attempts: int = 1
    protocol: LlmProtocol = "openai_compatible"
    max_tokens: int = 1_024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        source = os.environ if env is None else env
        protocol_value = source.get("FA_GATEWAY_PROTOCOL", "openai_compatible")
        if protocol_value not in {"openai_compatible", "anthropic"}:
            raise ValueError("FA_GATEWAY_PROTOCOL must be openai_compatible or anthropic")
        return cls(
            base_url=source.get("FA_GATEWAY_BASE_URL", "").rstrip("/"),
            api_key=source.get("FA_GATEWAY_API_KEY", ""),
            timeout_seconds=min(
                _MAX_GATEWAY_TIMEOUT_SECONDS,
                max(0.1, float(source.get("FA_GATEWAY_TIMEOUT_SECONDS", "5"))),
            ),
            max_attempts=max(1, int(source.get("FA_GATEWAY_MAX_ATTEMPTS", "1"))),
            protocol=cast(LlmProtocol, protocol_value),
            max_tokens=min(
                _MAX_COMPLETION_TOKENS,
                max(1, int(source.get("FA_GATEWAY_MAX_TOKENS", "1024"))),
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)


class JsonlCallRecorder:
    """记录已经脱敏的线协议，供审计与离线回放。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(self, entry: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        self.path.chmod(0o600)


class ReplayTransport:
    """只消费录制响应，不具备网络能力。"""

    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        self._records = list(records)
        self._cursor = 0

    @classmethod
    def from_jsonl(cls, path: str | Path) -> ReplayTransport:
        with Path(path).open(encoding="utf-8") as handle:
            return cls([json.loads(line) for line in handle if line.strip()])

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del url, headers, timeout_seconds
        if self._cursor >= len(self._records):
            raise RuntimeError("replay exhausted")
        record = self._records[self._cursor]
        self._cursor += 1
        expected = record.get("request")
        if expected is not None and expected != payload:
            raise RuntimeError("replay request does not match recorded redacted request")
        response = record.get("response")
        if not isinstance(response, Mapping):
            raise RuntimeError("recording has no wire response")
        return response


class OpenAICompatibleGateway:
    """Runtime OpenAI/Anthropic JSON client with deterministic fail-safe behavior."""

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        runtime_store: RuntimeLlmStore | None = None,
        transport: GatewayTransport | None = None,
        recorder: JsonlCallRecorder | None = None,
    ) -> None:
        self.config = config or GatewayConfig.from_env()
        self._runtime_authoritative = runtime_store is not None
        self._runtime_store = (
            runtime_store
            if runtime_store is not None
            else (RuntimeLlmStore.from_env() if config is None else None)
        )
        self._transport = transport or UrllibTransport()
        self._recorder = (
            recorder
            if recorder is not None
            else (
                JsonlCallRecorder(self._runtime_store.root / "llm_logs" / "gateway-calls.jsonl")
                if self._runtime_store is not None
                else None
            )
        )
        self._redactor = MandatoryRedactor()

    def complete_json(
        self,
        *,
        purpose: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
    ) -> GatewayResult:
        config, effective_model, unavailable_reason = self._current_config(model)
        if config is None:
            return GatewayResult(
                status="disabled",
                model=effective_model,
                reason=unavailable_reason,
            )
        request_id = uuid.uuid4().hex
        try:
            raw_payload = self._build_payload(
                protocol=config.protocol,
                model=effective_model,
                messages=messages,
                max_tokens=config.max_tokens,
            )
        except Exception as exc:
            reason = self._safe_failure_reason(exc, api_key=config.api_key)
            return GatewayResult(
                status="error",
                model=effective_model,
                reason=f"模型调用失败：{reason}；确定性流程不受影响",
                request_id=request_id,
            )
        envelope = self._redactor.redact(raw_payload)
        last_error_reason = "unknown gateway failure"
        for attempt in range(1, config.max_attempts + 1):
            started = time.monotonic()
            try:
                wire_response = self._transport.post_json(
                    self._completion_endpoint(config),
                    headers=self._completion_headers(config),
                    payload=envelope.payload,
                    timeout_seconds=config.timeout_seconds,
                )
                content = self._extract_json_content(
                    wire_response,
                    protocol=config.protocol,
                )
                restored = envelope.restore(content)
                self._record(
                    {
                        "request_id": request_id,
                        "purpose": purpose,
                        "protocol": config.protocol,
                        "model": effective_model,
                        "attempt": attempt,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "request": envelope.payload,
                        "response": wire_response,
                    }
                )
                return GatewayResult(
                    status="ok",
                    model=effective_model,
                    content=restored,
                    request_id=request_id,
                )
            except Exception as exc:
                last_error_reason = self._safe_failure_reason(exc, api_key=config.api_key)
        return GatewayResult(
            status="error",
            model=effective_model,
            reason=f"模型调用失败：{last_error_reason}；确定性流程不受影响",
            request_id=request_id,
        )

    def _current_config(self, requested_model: str) -> tuple[GatewayConfig | None, str, str]:
        if self._runtime_store is not None:
            try:
                runtime = self._runtime_store.load()
            except LlmRuntimeError:
                return (
                    None,
                    requested_model,
                    "运行时模型配置不可用；已安全降级，确定性流程不受影响",
                )
            if runtime is not None:
                effective_runtime_model = (
                    runtime.reviewer_model
                    if requested_model == "runtime-reviewer-model"
                    and runtime.reviewer_model
                    else runtime.selected_model
                )
                if not runtime.enabled:
                    return (
                        None,
                        effective_runtime_model,
                        "模型服务已禁用；确定性流程正常可用",
                    )
                return (
                    GatewayConfig(
                        base_url=runtime.base_url,
                        api_key=runtime.api_key,
                        timeout_seconds=runtime.timeout_seconds,
                        max_attempts=runtime.max_attempts,
                        protocol=runtime.protocol,
                        max_tokens=runtime.max_tokens,
                    ),
                    effective_runtime_model,
                    "",
                )
            if self._runtime_authoritative:
                return (
                    None,
                    requested_model,
                    "未配置模型服务；确定性流程正常可用",
                )
        if not self.config.enabled:
            return (
                None,
                requested_model,
                "未配置模型服务；确定性流程正常可用",
            )
        return self.config, requested_model, ""

    @staticmethod
    def _safe_failure_reason(error: Exception, *, api_key: str) -> str:
        return business_failure_reason(error, api_key=api_key)

    @staticmethod
    def _completion_endpoint(config: GatewayConfig) -> str:
        base_url = validate_base_url(config.base_url)
        if config.protocol == "anthropic":
            return f"{base_url}/messages" if base_url.endswith("/v1") else f"{base_url}/v1/messages"
        return f"{base_url}/chat/completions"

    @staticmethod
    def _completion_headers(config: GatewayConfig) -> Mapping[str, str]:
        if config.protocol == "anthropic":
            return {
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        return {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

    @classmethod
    def _build_payload(
        cls,
        *,
        protocol: LlmProtocol,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> dict[str, Any]:
        if not 1 <= max_tokens <= _MAX_COMPLETION_TOKENS:
            raise ValueError("max_tokens must be between 1 and 4096")
        if protocol == "anthropic":
            return cls._build_anthropic_payload(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
            )
        return {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": list(messages),
        }

    @classmethod
    def _build_anthropic_payload(
        cls,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> dict[str, Any]:
        system_parts = [
            "Return exactly one valid JSON object without markdown or surrounding text."
        ]
        converted: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            content = cls._message_text(message.get("content"))
            if role in {"system", "developer"}:
                if content:
                    system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                raise ValueError(f"unsupported Anthropic message role: {role or 'missing'}")
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"] = f"{converted[-1]['content']}\n{content}"
            else:
                converted.append({"role": role, "content": content})
        if not converted:
            raise ValueError(
                "Anthropic Messages API requires at least one user or assistant message"
            )
        return {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": "\n".join(system_parts),
            "messages": converted,
        }

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, Mapping):
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
            parts: list[str] = []
            for block in content:
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(cast(str, block["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    def _record(self, entry: Mapping[str, Any]) -> None:
        if self._recorder is not None:
            self._recorder.record(entry)

    @staticmethod
    def _extract_json_content(
        response: Mapping[str, Any],
        *,
        protocol: LlmProtocol = "openai_compatible",
    ) -> Mapping[str, Any]:
        if protocol == "anthropic":
            content_blocks = response.get("content")
            if not isinstance(content_blocks, list) or not content_blocks:
                raise ValueError("Anthropic response has no content")
            text = "".join(
                str(block["text"])
                for block in content_blocks
                if isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
            if not text:
                raise ValueError("Anthropic response has no text content")
            parsed = json.loads(text)
            if not isinstance(parsed, Mapping):
                raise ValueError("Anthropic JSON content must be an object")
            return parsed
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("gateway response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ValueError("gateway response has no message")
        content = message.get("content")
        if isinstance(content, Mapping):
            return content
        if not isinstance(content, str):
            raise ValueError("gateway message content must be JSON text")
        parsed = json.loads(content)
        if not isinstance(parsed, Mapping):
            raise ValueError("gateway JSON content must be an object")
        return parsed
