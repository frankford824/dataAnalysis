from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from commerce_harness.judgment.gateway import (
    GatewayConfig,
    OpenAICompatibleGateway,
)
from commerce_harness.llm_runtime import (
    LlmRuntimeError,
    RuntimeLlmStore,
    discover_models,
    validate_base_url,
)


def json_response(payload: Mapping[str, Any], *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class CapturingCompletionTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        if url.endswith("/messages"):
            return {"content": [{"type": "text", "text": '{"decision":"suggestion"}'}]}
        return {"choices": [{"message": {"content": '{"decision":"suggestion"}'}}]}


def test_no_runtime_configuration_is_a_safe_disabled_state(tmp_path: Path) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    status = store.public_status()
    transport = CapturingCompletionTransport()
    gateway = OpenAICompatibleGateway(runtime_store=store, transport=transport)

    result = gateway.complete_json(
        purpose="residual",
        model="caller-default",
        messages=[{"role": "user", "content": "{}"}],
    )

    assert not status.configured
    assert not status.enabled
    assert not status.key_configured
    assert result.status == "disabled"
    assert "确定性流程" in (result.reason or "")
    assert transport.calls == []


def test_openai_compatible_discovery_parses_models_and_authenticates() -> None:
    key = "openai-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://gateway.example/v1/models")
        assert request.headers["Authorization"] == f"Bearer {key}"
        assert "x-api-key" not in request.headers
        return json_response(
            {"data": [{"id": "gpt-test-b"}, {"id": "gpt-test-a"}, {"id": "gpt-test-a"}]}
        )

    result = discover_models(
        protocol="openai_compatible",
        base_url="https://gateway.example/v1/",
        api_key=key,
        transport=httpx.MockTransport(handler),
    )

    assert result.protocol == "openai_compatible"
    assert result.models == ("gpt-test-a", "gpt-test-b")
    assert result.completion_supported


def test_anthropic_discovery_parses_models_and_supports_completion() -> None:
    key = "anthropic-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.anthropic.com/v1/models")
        assert request.headers["x-api-key"] == key
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in request.headers
        return json_response({"data": [{"id": "claude-test-sonnet"}]})

    result = discover_models(
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        api_key=key,
        transport=httpx.MockTransport(handler),
    )

    assert result.protocol == "anthropic"
    assert result.models == ("claude-test-sonnet",)
    assert result.completion_supported


def test_auto_discovery_recognizes_openai_compatible_service() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Authorization", request.headers.get("x-api-key", "")))
        if request.headers.get("Authorization") == "Bearer auto-openai-key":
            return json_response({"data": [{"id": "openai-auto-model"}]})
        return json_response({"error": {"message": "unauthorized"}}, status=401)

    result = discover_models(
        protocol="auto",
        base_url="https://models.example/v1",
        api_key="auto-openai-key",
        transport=httpx.MockTransport(handler),
    )

    assert result.protocol == "openai_compatible"
    assert result.models == ("openai-auto-model",)
    assert attempts == ["Bearer auto-openai-key"]


def test_auto_discovery_recognizes_anthropic_after_safe_fallback() -> None:
    schemes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "Authorization" in request.headers:
            schemes.append("openai_compatible")
            return json_response({"error": {"message": "wrong protocol"}}, status=401)
        schemes.append("anthropic")
        assert request.headers["x-api-key"] == "auto-anthropic-key"
        return json_response({"data": [{"id": "claude-auto-model"}]})

    result = discover_models(
        protocol="auto",
        base_url="https://private-model-gateway.example",
        api_key="auto-anthropic-key",
        transport=httpx.MockTransport(handler),
    )

    assert result.protocol == "anthropic"
    assert result.models == ("claude-auto-model",)
    assert schemes == ["openai_compatible", "anthropic"]


def test_auto_discovery_prioritizes_anthropic_on_official_domain() -> None:
    schemes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        schemes.append("anthropic" if "x-api-key" in request.headers else "openai_compatible")
        return json_response({"data": [{"id": "claude-official"}]})

    result = discover_models(
        protocol="auto",
        base_url="https://api.anthropic.com",
        api_key="official-key",
        transport=httpx.MockTransport(handler),
    )

    assert result.protocol == "anthropic"
    assert schemes == ["anthropic"]


def test_runtime_store_is_atomic_private_and_never_exposes_key(tmp_path: Path) -> None:
    workbench = tmp_path / "outside-source-workbench"
    store = RuntimeLlmStore(workbench)
    key = "never-print-this-key"
    saved = store.save(
        protocol="openai_compatible",
        base_url="http://127.0.0.1:4100/v1",
        api_key=key,
        selected_model="local-model",
    )

    metadata = store.config_path.read_text(encoding="utf-8")
    secret_files = list(store.secrets_dir.iterdir())
    status_text = repr(store.public_status())

    assert saved.api_key == key
    assert key not in metadata
    assert key not in status_text
    assert len(secret_files) == 1
    assert secret_files[0].read_text(encoding="utf-8") == key
    assert stat.S_IMODE(store.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(store.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.secrets_dir.stat().st_mode) == 0o700
    assert key not in repr(saved)

    store.record_activity(
        status="ok",
        purpose="connection_test",
        model="local-model",
        message="模型已实际响应",
        request_id="request-safe",
    )
    activity = store.last_activity()
    assert activity is not None
    assert activity["status"] == "ok"
    assert activity["purpose"] == "connection_test"
    assert activity["model"] == "local-model"
    assert activity["message"] == "模型已实际响应"
    assert activity["request_id"] == "request-safe"
    assert activity["updated_at"]
    assert stat.S_IMODE(store.activity_path.stat().st_mode) == 0o600
    assert key not in store.activity_path.read_text(encoding="utf-8")

    with pytest.raises(LlmRuntimeError):
        store.save(
            protocol="auto",
            base_url="http://127.0.0.1:4100/v1",
            api_key=key,
            selected_model="must-record-actual-protocol",
        )


def test_bad_key_and_provider_error_do_not_leak_secret() -> None:
    key = "wrong-key-must-stay-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").endswith(key) or (
            request.headers.get("x-api-key") == key
        )
        return json_response(
            {"error": {"message": f"provider echoed {key}"}},
            status=401,
        )

    with pytest.raises(LlmRuntimeError) as caught:
        discover_models(
            protocol="auto",
            base_url="https://models.example",
            api_key=key,
            transport=httpx.MockTransport(handler),
        )

    assert key not in str(caught.value)
    assert "provider echoed" not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The read operation timed out", "模型响应超时"),
        ("gateway request failed with HTTP 401", "模型密钥或服务权限校验失败"),
        ("gateway request failed with HTTP 429", "模型服务当前限流，请稍后重试"),
        ("gateway request failed with HTTP 503", "模型服务暂时不可用"),
    ],
)
def test_gateway_provider_errors_are_business_readable(
    raw: str,
    expected: str,
) -> None:
    assert (
        OpenAICompatibleGateway._safe_failure_reason(
            RuntimeError(raw),
            api_key="secret",
        )
        == expected
    )


def test_gateway_reads_runtime_store_and_switches_protocol_immediately(
    tmp_path: Path,
) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    transport = CapturingCompletionTransport()
    gateway = OpenAICompatibleGateway(runtime_store=store, transport=transport)
    store.save(
        protocol="openai_compatible",
        base_url="https://first.example/v1",
        api_key="first-secret",
        selected_model="first-model",
    )

    first = gateway.complete_json(
        purpose="first",
        model="stale-caller-model",
        messages=[{"role": "user", "content": "{}"}],
    )
    store.save(
        protocol="anthropic",
        base_url="https://api.anthropic.example",
        api_key="second-secret",
        selected_model="claude-second",
        max_tokens=2_048,
    )
    second = gateway.complete_json(
        purpose="second",
        model="stale-caller-model",
        messages=[
            {"role": "system", "content": "你是受控核对助手"},
            {"role": "user", "content": '{"shop":"真实店铺","question":"解释差异"}'},
        ],
    )

    assert first.status == second.status == "ok"
    assert first.model == "first-model"
    assert second.model == "claude-second"
    assert transport.calls[0]["url"] == "https://first.example/v1/chat/completions"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer first-secret"
    assert transport.calls[0]["payload"]["model"] == "first-model"
    assert transport.calls[1]["url"] == "https://api.anthropic.example/v1/messages"
    assert transport.calls[1]["headers"]["x-api-key"] == "second-secret"
    assert transport.calls[1]["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in transport.calls[1]["headers"]
    assert transport.calls[1]["payload"]["model"] == "claude-second"
    assert transport.calls[1]["payload"]["max_tokens"] == 2_048
    assert transport.calls[1]["payload"]["max_tokens"] <= 4_096
    assert "response_format" not in transport.calls[1]["payload"]
    assert all(
        message["role"] != "system"
        for message in transport.calls[1]["payload"]["messages"]
    )
    wire = json.dumps(transport.calls[1]["payload"], ensure_ascii=False)
    assert "真实店铺" not in wire
    assert second.content == {"decision": "suggestion"}
    assert len(list(store.secrets_dir.iterdir())) == 1


def test_runtime_gateway_routes_proposer_and_reviewer_to_distinct_models(
    tmp_path: Path,
) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    store.save(
        protocol="openai_compatible",
        base_url="https://models.example/v1",
        api_key="shared-provider-secret",
        selected_model="proposer-model",
        reviewer_model="reviewer-model",
    )
    transport = CapturingCompletionTransport()
    gateway = OpenAICompatibleGateway(runtime_store=store, transport=transport)

    proposer = gateway.complete_json(
        purpose="proposal",
        model="runtime-configured-model",
        messages=[{"role": "user", "content": "{}"}],
    )
    reviewer = gateway.complete_json(
        purpose="review",
        model="runtime-reviewer-model",
        messages=[{"role": "user", "content": "{}"}],
    )

    assert proposer.model == "proposer-model"
    assert reviewer.model == "reviewer-model"
    assert [call["payload"]["model"] for call in transport.calls] == [
        "proposer-model",
        "reviewer-model",
    ]
    assert store.public_status().reviewer_model == "reviewer-model"


def test_gateway_failure_and_corrupt_runtime_config_fail_closed_without_key(
    tmp_path: Path,
) -> None:
    class LeakingFailureTransport:
        def post_json(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            payload: Mapping[str, Any],
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            del url, payload, timeout_seconds
            raise RuntimeError(f"provider leaked {headers['Authorization']}")

    store = RuntimeLlmStore(tmp_path / "workbench")
    key = "completion-secret"
    store.save(
        protocol="openai_compatible",
        base_url="https://models.example/v1",
        api_key=key,
        selected_model="test-model",
        max_attempts=1,
    )
    gateway = OpenAICompatibleGateway(
        runtime_store=store,
        transport=LeakingFailureTransport(),
    )
    failed = gateway.complete_json(
        purpose="safe-failure",
        model="ignored",
        messages=[{"role": "user", "content": "{}"}],
    )
    assert failed.status == "error"
    assert key not in (failed.reason or "")

    store.config_path.write_text("{broken-json", encoding="utf-8")
    corrupt = gateway.complete_json(
        purpose="corrupt",
        model="caller-model",
        messages=[{"role": "user", "content": "{}"}],
    )
    assert corrupt.status == "disabled"
    assert key not in (corrupt.reason or "")
    assert "确定性流程" in (corrupt.reason or "")


def test_anthropic_runtime_applies_messages_api_immediately(tmp_path: Path) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    store.save(
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        api_key="anthropic-key",
        selected_model="claude-test",
    )
    transport = CapturingCompletionTransport()
    gateway = OpenAICompatibleGateway(runtime_store=store, transport=transport)

    result = gateway.complete_json(
        purpose="anthropic-json",
        model="ignored",
        messages=[
            {"role": "developer", "content": "仅提出解释"},
            {"role": "user", "content": {"residual_id": "residual-secret"}},
        ],
    )

    assert result.status == "ok"
    assert result.model == "claude-test"
    assert result.content == {"decision": "suggestion"}
    assert transport.calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert "仅提出解释" not in transport.calls[0]["payload"]["messages"]
    assert "仅提出解释" in transport.calls[0]["payload"]["system"]
    assert transport.calls[0]["payload"]["max_tokens"] == 1_024
    assert store.public_status().completion_supported
    assert store.public_status().detail == "已启用"


def test_anthropic_audit_is_redacted_and_malformed_json_fails_safely(
    tmp_path: Path,
) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    store.save(
        protocol="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key="anthropic-audit-key",
        selected_model="claude-audit",
    )
    recording = store.root / "llm_logs" / "gateway-calls.jsonl"
    transport = CapturingCompletionTransport()
    gateway = OpenAICompatibleGateway(
        runtime_store=store,
        transport=transport,
    )
    result = gateway.complete_json(
        purpose="anthropic-audit",
        model="ignored",
        messages=[{"role": "user", "content": '{"file_id":"private-file-id"}'}],
    )

    audit = recording.read_text(encoding="utf-8")
    assert result.status == "ok"
    assert '"protocol": "anthropic"' in audit
    assert "private-file-id" not in audit
    assert "anthropic-audit-key" not in audit
    assert stat.S_IMODE(recording.stat().st_mode) == 0o600

    class MalformedAnthropicTransport:
        def post_json(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            payload: Mapping[str, Any],
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            del url, payload, timeout_seconds
            assert headers["x-api-key"] == "anthropic-audit-key"
            return {"content": [{"type": "text", "text": "not-json"}]}

    malformed = OpenAICompatibleGateway(
        runtime_store=store,
        transport=MalformedAnthropicTransport(),
    ).complete_json(
        purpose="malformed",
        model="ignored",
        messages=[{"role": "user", "content": "{}"}],
    )
    assert malformed.status == "error"
    assert "anthropic-audit-key" not in (malformed.reason or "")
    assert "确定性流程不受影响" in (malformed.reason or "")


def test_anthropic_invalid_message_or_token_limit_fails_before_transport() -> None:
    transport = CapturingCompletionTransport()
    invalid_role = OpenAICompatibleGateway(
        GatewayConfig(
            base_url="https://api.anthropic.com/v1",
            api_key="key",
            protocol="anthropic",
        ),
        transport=transport,
    ).complete_json(
        purpose="invalid-role",
        model="claude-test",
        messages=[{"role": "tool", "content": "{}"}],
    )
    invalid_limit = OpenAICompatibleGateway(
        GatewayConfig(
            base_url="https://api.anthropic.com/v1",
            api_key="key",
            protocol="anthropic",
            max_tokens=4_097,
        ),
        transport=transport,
    ).complete_json(
        purpose="invalid-limit",
        model="claude-test",
        messages=[{"role": "user", "content": "{}"}],
    )

    assert invalid_role.status == invalid_limit.status == "error"
    assert all(
        "确定性流程不受影响" in (item.reason or "")
        for item in (invalid_role, invalid_limit)
    )
    assert transport.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/models",
        "ftp://models.example/v1",
        "https://user:password@models.example/v1",
        "https://models.example/v1?key=secret",
    ],
)
def test_discovery_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(LlmRuntimeError) as caught:
        discover_models(protocol="auto", base_url=url, api_key="secret")
    assert "secret" not in str(caught.value)


def test_discovery_rejects_redirects_and_oversized_responses() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(307, headers={"Location": "https://elsewhere.example/v1/models"})

    with pytest.raises(LlmRuntimeError, match="重定向"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            transport=httpx.MockTransport(redirect),
        )

    def oversized(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"x" * 65)

    with pytest.raises(LlmRuntimeError, match="安全上限"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            max_response_bytes=64,
            transport=httpx.MockTransport(oversized),
        )


def test_runtime_status_disable_and_environment_root(tmp_path: Path) -> None:
    workbench = tmp_path / "runtime-root"
    store = RuntimeLlmStore.from_env({"FA_LLM_RUNTIME_ROOT": str(workbench)})
    store.save(
        protocol="openai_compatible",
        base_url="https://models.example/v1",
        api_key="status-key",
        selected_model="status-model",
    )
    enabled = store.public_status()
    assert enabled.configured
    assert enabled.enabled
    assert enabled.detail == "已启用"

    store.disable()
    disabled = store.public_status()
    assert disabled.configured
    assert not disabled.enabled
    assert "已禁用" in disabled.detail

    empty_store = RuntimeLlmStore(tmp_path / "empty")
    empty_store.disable()
    assert empty_store.load() is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"not-json", "有效 JSON"),
        (json_response({"models": []}), "格式不受支持"),
        (json_response({"data": [{"name": "missing-id"}]}), "未返回可用模型"),
    ],
)
def test_discovery_rejects_invalid_model_responses(
    payload: bytes | httpx.Response,
    expected: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload) if isinstance(payload, bytes) else payload

    with pytest.raises(LlmRuntimeError, match=expected):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )


def test_discovery_sanitizes_transport_errors_and_content_length() -> None:
    def connection_failure(request: httpx.Request) -> httpx.Response:
        del request
        raise RuntimeError("transport internals must not be public")

    with pytest.raises(LlmRuntimeError, match="无法连接") as caught:
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="private-key",
            transport=httpx.MockTransport(connection_failure),
        )
    assert "transport internals" not in str(caught.value)

    def advertised_too_large(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Length": "100"},
            content=b'{"data":[]}',
        )

    with pytest.raises(LlmRuntimeError, match="安全上限"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            max_response_bytes=64,
            transport=httpx.MockTransport(advertised_too_large),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": "", "selected_model": "model"},
        {"api_key": "key", "selected_model": "  "},
        {"api_key": "key", "selected_model": "model", "max_attempts": 4},
        {"api_key": "key", "selected_model": "model", "timeout_seconds": 31.0},
        {"api_key": "key", "selected_model": "model", "max_tokens": 4_097},
    ],
)
def test_runtime_store_rejects_invalid_configuration(
    tmp_path: Path,
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(LlmRuntimeError):
        RuntimeLlmStore(tmp_path / "workbench").save(
            protocol="openai_compatible",
            base_url="https://models.example",
            **kwargs,
        )


def test_discovery_rejects_empty_key_invalid_limits_and_invalid_port() -> None:
    with pytest.raises(LlmRuntimeError, match="密钥"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="",
        )
    with pytest.raises(LlmRuntimeError, match="上限无效"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            max_response_bytes=0,
        )
    with pytest.raises(LlmRuntimeError, match="超时"):
        discover_models(
            protocol="openai_compatible",
            base_url="https://models.example",
            api_key="key",
            timeout_seconds=31,
        )
    with pytest.raises(LlmRuntimeError, match="地址无效"):
        validate_base_url("https://models.example:invalid")
    assert validate_base_url("http://[::1]:4100/v1/") == "http://[::1]:4100/v1"


def test_public_status_fail_closes_for_invalid_runtime_metadata(tmp_path: Path) -> None:
    store = RuntimeLlmStore(tmp_path / "workbench")
    store.runtime_dir.mkdir(parents=True)
    store.config_path.write_text('{"schema_version":99}', encoding="utf-8")

    status = store.public_status()

    assert not status.configured
    assert not status.enabled
    assert "安全降级" in status.detail
