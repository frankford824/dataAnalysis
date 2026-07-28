from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .gateway import OpenAICompatibleGateway
from .models import GatewayResult, SuggestionCandidate


class ResidualJudge:
    """一次残差、一次无状态调用；只返回结构化候选。"""

    def __init__(self, gateway: OpenAICompatibleGateway, *, model: str) -> None:
        self.gateway = gateway
        self.model = model

    def suggest(self, context: Mapping[str, Any]) -> tuple[SuggestionCandidate, ...]:
        result: GatewayResult = self.gateway.complete_json(
            purpose="residual_suggestion",
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "只输出 JSON。你只能生成分类、关联或差异解释建议；"
                        "不得修改账本、不得生成正式金额、不得省略证据引用。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, sort_keys=True, default=str),
                },
            ],
        )
        if result.status == "disabled":
            return ()
        if result.status != "ok" or result.content is None:
            raise RuntimeError(result.reason or "judgment gateway failed")
        raw_candidates = result.content.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("response must contain a candidates list")
        return tuple(
            SuggestionCandidate.from_mapping(item, source_model=self.model)
            for item in raw_candidates
        )

