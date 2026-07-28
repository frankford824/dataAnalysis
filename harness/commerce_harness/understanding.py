"""Model-proposed file and field understanding, frozen into rule versions.

The model proposes what a file is and how columns map. Once a human
confirms, the proposal is frozen as a hashed rule_version. Later
recomputes replay the frozen artifact — they do not re-ask the model.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from commerce_harness.judgment.gateway import OpenAICompatibleGateway
from commerce_harness.memory.database import DuckDBMemory

ALLOWED_SOURCE_KINDS = (
    "baobei_order",
    "alipay_ledger",
    "wechat_ledger",
    "platform_settlement",
    "product_cost",
    "freight",
    "advertising",
    "unknown",
)
ALLOWED_TARGET_FIELDS = (
    "business_key",
    "amount",
    "event_date",
    "currency",
    "sku",
    "description",
    "store_name",
    "side",
)


@dataclass(frozen=True, slots=True)
class FileUnderstanding:
    proposal_id: str
    source_kind: str
    confidence: float
    field_map: dict[str, str]
    rationale: str
    sample_headers: tuple[str, ...]
    frozen: bool = False
    rule_version_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposalId": self.proposal_id,
            "sourceKind": self.source_kind,
            "confidence": self.confidence,
            "fieldMap": self.field_map,
            "rationale": self.rationale,
            "sampleHeaders": list(self.sample_headers),
            "frozen": self.frozen,
            "ruleVersionId": self.rule_version_id,
        }


def propose_file_understanding(
    gateway: OpenAICompatibleGateway,
    *,
    model: str,
    headers: Sequence[str],
    sample_rows: Sequence[Mapping[str, Any]],
    original_name: str,
    fallback_source_kind: str | None = None,
) -> FileUnderstanding:
    """Ask the model what this file is. Fallback keeps templates as backup."""

    context = {
        "original_name": original_name,
        "headers": list(headers),
        "sample_rows": [dict(row) for row in sample_rows[:5]],
        "allowed_source_kinds": list(ALLOWED_SOURCE_KINDS),
    }
    result = gateway.complete_json(
        purpose="file_understanding",
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "只输出 JSON。根据表头和样例行判断这是什么业务文件，"
                    "并给出字段映射（原始列名→标准字段）。"
                    "标准字段限于："
                    "business_key,amount,event_date,currency,sku,"
                    "description,store_name,side。"
                    "不得编造样例中不存在的数字。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    context, ensure_ascii=False, sort_keys=True, default=str
                ),
            },
        ],
    )
    if result.status != "ok" or result.content is None:
        kind = fallback_source_kind or "unknown"
        return FileUnderstanding(
            proposal_id=f"understand_{uuid.uuid4().hex}",
            source_kind=kind,
            confidence=0.0,
            field_map={},
            rationale=(
                "模型不可用，已退回模板兜底"
                if fallback_source_kind
                else "模型不可用"
            ),
            sample_headers=tuple(headers),
        )
    content = result.content
    field_map = content.get("field_map") or content.get("fieldMap") or {}
    if not isinstance(field_map, dict):
        field_map = {}
    source_kind = str(
        content.get("source_kind") or content.get("sourceKind") or "unknown"
    )
    if source_kind not in ALLOWED_SOURCE_KINDS:
        source_kind = "unknown"
    try:
        confidence = float(content.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return FileUnderstanding(
        proposal_id=f"understand_{uuid.uuid4().hex}",
        source_kind=source_kind,
        confidence=min(max(confidence, 0.0), 1.0),
        # Only mappings onto known headers and known target fields survive; a
        # hallucinated column must not become a frozen rule.
        field_map={
            str(key): str(value)
            for key, value in field_map.items()
            if str(key) in set(headers) and str(value) in ALLOWED_TARGET_FIELDS
        },
        rationale=str(content.get("rationale") or content.get("reason") or ""),
        sample_headers=tuple(headers),
    )


def _definition_payload(understanding: FileUnderstanding) -> tuple[str, str]:
    definition = {
        "action": "map",
        "source_kind": understanding.source_kind,
        "field_map": understanding.field_map,
        "rationale": understanding.rationale,
        "sample_headers": list(understanding.sample_headers),
    }
    payload = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_rule_definition(
    database: DuckDBMemory, understanding: FileUnderstanding, logical_key: str | None
) -> str:
    key = logical_key or f"file_map:{understanding.source_kind}"
    existing = database.execute(
        "SELECT rule_id FROM rule_definition WHERE logical_key = ?",
        [key],
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    rule_id = f"rule_{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    database.execute(
        """
        INSERT INTO rule_definition (
            rule_id, logical_key, rule_kind, title, description
        ) VALUES (?, ?, 'file_understanding', ?, ?)
        """,
        [
            rule_id,
            key,
            f"文件理解：{understanding.source_kind}",
            understanding.rationale[:500],
        ],
    )
    return rule_id


def _next_version(database: DuckDBMemory, rule_id: str) -> int:
    row = database.execute(
        "SELECT coalesce(max(version), 0) + 1 FROM rule_version WHERE rule_id = ?",
        [rule_id],
    ).fetchone()
    return int(row[0]) if row else 1


def _understanding_from_definition(
    definition: Mapping[str, Any],
    *,
    proposal_id: str,
    rule_version_id: str | None,
    frozen: bool,
) -> FileUnderstanding:
    field_map = definition.get("field_map") or {}
    return FileUnderstanding(
        proposal_id=proposal_id,
        source_kind=str(definition.get("source_kind") or "unknown"),
        confidence=float(definition.get("confidence") or 0),
        field_map={str(k): str(v) for k, v in dict(field_map).items()},
        rationale=str(definition.get("rationale") or ""),
        sample_headers=tuple(definition.get("sample_headers") or ()),
        frozen=frozen,
        rule_version_id=rule_version_id,
    )


def record_proposal(
    database: DuckDBMemory,
    understanding: FileUnderstanding,
    *,
    logical_key: str | None = None,
) -> FileUnderstanding:
    """Store the model's proposal as a draft rule version.

    Freezing later reads the stored draft, so the confirming client cannot
    substitute a different mapping than the one it was shown.
    """
    payload, checksum = _definition_payload(understanding)
    rule_id = _ensure_rule_definition(database, understanding, logical_key)
    draft_id = f"rvd_{checksum[:24]}"
    with database.transaction() as connection:
        existing = connection.execute(
            "SELECT rule_version_id FROM rule_version WHERE rule_version_id = ?",
            [draft_id],
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO rule_version (
                    rule_version_id, rule_id, version, effective_from, status,
                    definition_json, checksum_sha256
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                [
                    draft_id,
                    rule_id,
                    _next_version(database, rule_id),
                    datetime.now(UTC).date(),
                    payload,
                    checksum,
                ],
            )
    return FileUnderstanding(
        proposal_id=draft_id,
        source_kind=understanding.source_kind,
        confidence=understanding.confidence,
        field_map=understanding.field_map,
        rationale=understanding.rationale,
        sample_headers=understanding.sample_headers,
    )


def freeze_understanding(
    database: DuckDBMemory,
    proposal_id: str,
    *,
    operator_name: str,
) -> FileUnderstanding:
    """Approve a stored draft understanding as an immutable rule version."""

    operator = (operator_name or "").strip()
    if not operator:
        raise ValueError("冻结文件理解必须填写操作人")
    draft = database.execute(
        """
        SELECT rule_id, status, definition_json, checksum_sha256
        FROM rule_version WHERE rule_version_id = ?
        """,
        [proposal_id],
    ).fetchone()
    if draft is None:
        raise LookupError(f"找不到待确认的文件理解: {proposal_id}")
    rule_id, status, definition_json, checksum = (
        str(draft[0]),
        str(draft[1]),
        str(draft[2]),
        str(draft[3]),
    )
    if status not in {"draft", "approved"}:
        raise ValueError(f"该文件理解已处于 {status} 状态，不能冻结")

    definition = json.loads(definition_json)
    approved_id = f"rv_{checksum[:24]}"
    with database.transaction() as connection:
        already = connection.execute(
            """
            SELECT rule_version_id FROM rule_version
            WHERE checksum_sha256 = ? AND status = 'approved'
            """,
            [checksum],
        ).fetchone()
        if already is None:
            # Append the approved version instead of mutating the draft: the
            # record of what the model first proposed must survive.
            connection.execute(
                """
                INSERT INTO rule_version (
                    rule_version_id, rule_id, version, effective_from, status,
                    definition_json, checksum_sha256, approved_by, approved_at
                ) VALUES (?, ?, ?, ?, 'approved', ?, ?, ?, ?)
                """,
                [
                    approved_id,
                    rule_id,
                    _next_version(database, rule_id),
                    datetime.now(UTC).date(),
                    definition_json,
                    checksum,
                    operator,
                    datetime.now(UTC),
                ],
            )
        else:
            approved_id = str(already[0])
    return _understanding_from_definition(
        definition,
        proposal_id=proposal_id,
        rule_version_id=approved_id,
        frozen=True,
    )
