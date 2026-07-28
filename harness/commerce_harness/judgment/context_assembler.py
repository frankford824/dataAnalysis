from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


class ContextAssembler:
    """无会话状态地装配最小上下文，按固定优先级裁剪。"""

    def __init__(self, *, max_chars: int = 16_000) -> None:
        if max_chars < 1_000:
            raise ValueError("max_chars is too small for a safe structured context")
        self.max_chars = max_chars

    def assemble(
        self,
        *,
        residual: Mapping[str, Any],
        corrections: Iterable[Mapping[str, Any]] = (),
        rules: Iterable[Mapping[str, Any]] = (),
        related_rows: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "residual": dict(residual),
            "corrections": [],
            "rules": [],
            "related_rows": [],
        }
        if self._size(context) > self.max_chars:
            raise ValueError("residual alone exceeds the context budget")
        # 方案优先级：残差 > 错题本 > 规则 > 关联行。
        for section, items in (
            ("corrections", corrections),
            ("rules", rules),
            ("related_rows", related_rows),
        ):
            for item in items:
                candidate = dict(item)
                context[section].append(candidate)
                if self._size(context) > self.max_chars:
                    context[section].pop()
                    break
        return context

    @staticmethod
    def _size(value: Mapping[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))

