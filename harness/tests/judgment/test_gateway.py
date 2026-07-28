from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from commerce_harness.judgment.gateway import (
    GatewayConfig,
    JsonlCallRecorder,
    OpenAICompatibleGateway,
    ReplayTransport,
)
from commerce_harness.judgment.residual import ResidualJudge


class CapturingTransport:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del url, timeout_seconds
        assert headers["Authorization"] == "Bearer test-key"
        self.calls.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"echo": payload["messages"][0]["content"]},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }


class CandidateTransport:
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del url, headers, timeout_seconds
        context = json.loads(payload["messages"][1]["content"])
        evidence = context["residual"]["evidence"]
        candidate = {
            "suggestion_id": "suggestion-1",
            "residual_id": context["residual"]["residual_id"],
            "kind": "explanation",
            "category": "timing",
            "action": "建议核对跨期到账",
            "rationale": "证据支持跨期假设",
            "confidence": "0.80",
            "citations": [evidence],
        }
        return {"choices": [{"message": {"content": json.dumps({"candidates": [candidate]})}}]}


def configured() -> GatewayConfig:
    return GatewayConfig(base_url="https://gateway.invalid/v1", api_key="test-key")


def test_no_key_is_a_normal_disabled_state_and_never_calls_transport() -> None:
    transport = CapturingTransport()
    gateway = OpenAICompatibleGateway(GatewayConfig(), transport=transport)
    result = gateway.complete_json(
        purpose="test",
        model="model-a",
        messages=[{"role": "user", "content": "{}"}],
    )
    assert result.status == "disabled"
    assert transport.calls == []


def test_gateway_mandatorily_redacts_nested_json_before_transport() -> None:
    transport = CapturingTransport()
    gateway = OpenAICompatibleGateway(configured(), transport=transport)
    sensitive = {
        "shop": "真实店铺",
        "file_id": "sha256-secret",
        "account_id": "account-secret",
    }
    result = gateway.complete_json(
        purpose="test",
        model="model-a",
        messages=[{"role": "user", "content": json.dumps(sensitive, ensure_ascii=False)}],
    )
    assert result.status == "ok"
    wire = json.dumps(transport.calls[0], ensure_ascii=False)
    assert "真实店铺" not in wire
    assert "sha256-secret" not in wire
    assert "account-secret" not in wire
    assert result.content == {"echo": json.dumps(sensitive, ensure_ascii=False)}


def test_recording_can_be_replayed_without_network(tmp_path) -> None:
    recording = tmp_path / "calls.jsonl"
    transport = CapturingTransport()
    gateway = OpenAICompatibleGateway(
        configured(),
        transport=transport,
        recorder=JsonlCallRecorder(recording),
    )
    messages = [{"role": "user", "content": '{"shop":"真实店铺"}'}]
    first = gateway.complete_json(purpose="replay", model="model-a", messages=messages)
    replay = OpenAICompatibleGateway(
        configured(),
        transport=ReplayTransport.from_jsonl(recording),
    )
    second = replay.complete_json(purpose="replay", model="model-a", messages=messages)
    assert first.status == second.status == "ok"
    assert first.content == second.content


def test_residual_judge_only_returns_structured_suggestions() -> None:
    gateway = OpenAICompatibleGateway(configured(), transport=CandidateTransport())
    judge = ResidualJudge(gateway, model="model-a")
    candidates = judge.suggest(
        {
            "residual": {
                "residual_id": "residual-1",
                "evidence": {
                    "file_id": "file-secret",
                    "row_no": 7,
                    "metric": "settlement",
                    "period": "2026-05",
                    "shop": "真实店铺",
                    "value": "12.34",
                    "definition_id": "settlement-v1",
                },
            }
        }
    )
    assert len(candidates) == 1
    assert candidates[0].citations[0].file_id == "file-secret"
    assert candidates[0].citations[0].shop == "真实店铺"
    assert candidates[0].requires_human_review
    assert not candidates[0].may_write_ledger
