from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

_SENSITIVE_KEYS = {
    "shop",
    "shop_id",
    "shop_name",
    "account",
    "account_id",
    "account_name",
    "counterparty",
    "counterparty_id",
    "order_id",
    "transaction_id",
    "file_id",
    "source_path",
    "user_id",
}


@dataclass(frozen=True, slots=True)
class RedactionEnvelope:
    payload: Any
    aliases: dict[str, str]

    def restore(self, value: Any) -> Any:
        reverse = {alias: original for original, alias in self.aliases.items()}

        def visit(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, str):
                restored = item
                for alias, original in sorted(reverse.items(), key=lambda pair: -len(pair[0])):
                    restored = restored.replace(alias, original)
                return restored
            return item

        return visit(value)


class MandatoryRedactor:
    """网关不可绕过的脱敏边界；有意不提供 enabled 开关。"""

    def __init__(self, *, salt: str = "commerce-harness-redaction-v1") -> None:
        self._salt = salt

    def redact(self, payload: Any) -> RedactionEnvelope:
        aliases: dict[str, str] = {}

        def collect(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.lower() in _SENSITIVE_KEYS and isinstance(child, (str, int)):
                        original = str(child)
                        if original:
                            aliases.setdefault(original, self._alias(key, original))
                    collect(child)
            elif isinstance(item, list):
                for child in item:
                    collect(child)
            elif isinstance(item, str):
                stripped = item.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        nested = json.loads(stripped)
                    except json.JSONDecodeError:
                        return
                    collect(nested)

        collect(payload)

        def visit(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, str):
                redacted = item
                for original, alias in sorted(aliases.items(), key=lambda pair: -len(pair[0])):
                    redacted = redacted.replace(original, alias)
                return redacted
            return item

        detached = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        return RedactionEnvelope(payload=visit(detached), aliases=aliases)

    def _alias(self, key: str, value: str) -> str:
        digest = hashlib.sha256(f"{self._salt}:{key}:{value}".encode()).hexdigest()[:12]
        prefix = key.upper().replace("_ID", "").replace("_NAME", "")
        return f"<{prefix}_{digest}>"
