"""Model-generated review questions in plain business language.

Replaces the hard-coded if-else copy table. Every number cited in the
generated copy must pass cite_guard against the evidence ledger.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from commerce_harness.judgment.cite_guard import CiteGuard, EvidenceLedger
from commerce_harness.judgment.gateway import OpenAICompatibleGateway
from commerce_harness.judgment.models import EvidenceCitation

_NUMBER_IN_TEXT = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class ReviewOption:
    code: str
    label: str
    recommended: bool
    consequence_hint: str


@dataclass(frozen=True, slots=True)
class ReviewQuestion:
    question_id: str
    what: str
    why: str
    options: tuple[ReviewOption, ...]
    citations: tuple[EvidenceCitation, ...]
    evidence_guard: str
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "questionId": self.question_id,
            "what": self.what,
            "why": self.why,
            "options": [
                {
                    "code": option.code,
                    "label": option.label,
                    "recommended": option.recommended,
                    "consequenceHint": option.consequence_hint,
                }
                for option in self.options
            ],
            "evidenceGuard": self.evidence_guard,
            "fallback": self.fallback,
        }


_FALLBACK_BY_REASON: dict[str, tuple[str, str, tuple[tuple[str, str, bool], ...]]] = {
    "missing_side": (
        "有一笔钱在一边找得到、另一边找不到。",
        "可能是平台直接扣的费用，本来就不该有对应订单；也可能是文件缺了。",
        (
            ("legal_single_sided_fee", "是平台服务费，直接算成本", True),
            ("keep_open", "不是，这些应该有订单，先放着别动", False),
            ("defer", "我不确定，标记下来下个月再看", False),
        ),
    ),
    "amount_mismatch": (
        "同一笔业务两边金额对不上。",
        "可能是退款、优惠或跨月结算造成的差额。",
        (
            ("explain_timing", "是跨月到账，按实际到账月算", True),
            ("keep_open", "金额有问题，先放着查", False),
            ("defer", "我不确定，标记下来下个月再看", False),
        ),
    ),
}


def _fallback_question(
    *,
    reason_code: str,
    amount: Decimal,
    count: int,
) -> ReviewQuestion:
    what_base, why, options = _FALLBACK_BY_REASON.get(
        reason_code,
        (
            "有一笔账对不上。",
            "需要你看一下这是什么情况。",
            (
                ("keep_open", "先放着别动", True),
                ("defer", "我不确定，标记下来下个月再看", False),
            ),
        ),
    )
    what = f"{what_base}共 {count} 笔，合计 {format(abs(amount), 'f')} 元。"
    return ReviewQuestion(
        question_id=f"question_{uuid.uuid4().hex}",
        what=what,
        why=why,
        options=tuple(
            ReviewOption(
                code=code,
                label=label,
                recommended=recommended,
                consequence_hint="",
            )
            for code, label, recommended in options
        ),
        citations=(),
        evidence_guard="fallback",
        fallback=True,
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _numbers_in_context(value: Any, into: set[Decimal]) -> None:
    """Collect every number the model was actually given."""
    if isinstance(value, Mapping):
        for item in value.values():
            _numbers_in_context(item, into)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _numbers_in_context(item, into)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, Decimal)):
        into.add(Decimal(str(value)))
        return
    if isinstance(value, str):
        for token in _NUMBER_IN_TEXT.findall(value):
            parsed = _decimal_or_none(token)
            if parsed is not None:
                into.add(parsed)


def ungrounded_numbers(text: str, allowed: set[Decimal]) -> list[str]:
    """Numbers in generated copy that were not present in the context.

    A number the model invented is worse than no answer, so the caller falls
    back to deterministic copy when this is non-empty.
    """
    invented: list[str] = []
    for token in _NUMBER_IN_TEXT.findall(text or ""):
        parsed = _decimal_or_none(token)
        if parsed is None or parsed not in allowed:
            invented.append(token)
    return invented


def generate_review_question(
    gateway: OpenAICompatibleGateway,
    *,
    model: str,
    reason_code: str,
    amount: Decimal,
    count: int,
    explanation: Mapping[str, Any] | None,
    ledger: EvidenceLedger | None,
    citations: Sequence[EvidenceCitation] = (),
) -> ReviewQuestion:
    context = {
        "reason_code": reason_code,
        "amount": format(amount, "f"),
        "count": count,
        "explanation": dict(explanation or {}),
        "style": {
            "language": "zh-CN",
            "audience": "财务专员",
            "forbidden_words": [
                "不变量",
                "未决差额",
                "残差",
                "影子运行",
                "反事实",
                "规则晋升",
                "trust tier",
                "归一化",
                "快照",
                "指纹",
                "语料",
            ],
            "required_shape": {
                "what": "一句话说明发生了什么，含笔数和金额",
                "why": "一句话说明为什么要问她",
                "options": "最多三个完整句子选项，含一个推荐",
            },
        },
    }
    result = gateway.complete_json(
        purpose="review_question",
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "只输出 JSON，字段 what/why/options。"
                    "options 是数组，每项含 code/label/recommended。"
                    "用财务专员能听懂的话，禁止技术黑话。"
                    "提到的每个数字必须来自给定上下文。"
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
        return _fallback_question(
            reason_code=reason_code, amount=amount, count=count
        )

    content = result.content
    raw_options = content.get("options") or []
    options: list[ReviewOption] = []
    if isinstance(raw_options, list):
        for item in raw_options[:3]:
            if not isinstance(item, dict):
                continue
            options.append(
                ReviewOption(
                    code=str(item.get("code") or f"opt_{len(options)}"),
                    label=str(item.get("label") or ""),
                    recommended=bool(item.get("recommended")),
                    consequence_hint=str(
                        item.get("consequence_hint")
                        or item.get("consequenceHint")
                        or ""
                    ),
                )
            )
    if not options:
        return _fallback_question(
            reason_code=reason_code, amount=amount, count=count
        )

    what = str(content.get("what") or "")
    why = str(content.get("why") or "")

    # Numeric grounding runs on every generation, with or without a ledger:
    # the copy may only repeat numbers it was given.
    allowed: set[Decimal] = {amount, abs(amount), Decimal(count)}
    _numbers_in_context(context["explanation"], allowed)
    for citation in citations:
        allowed.add(Decimal(str(citation.value)))
    generated_text = " ".join(
        [what, why, *(option.label for option in options),
         *(option.consequence_hint for option in options)]
    )
    if ungrounded_numbers(generated_text, allowed):
        return _fallback_question(
            reason_code=reason_code, amount=amount, count=count
        )

    guard_status = "numbers_grounded"
    if ledger is not None and citations:
        guard_result = CiteGuard().verify(
            _CitationCarrier(tuple(citations)), ledger
        )
        if not guard_result.valid:
            return _fallback_question(
                reason_code=reason_code, amount=amount, count=count
            )
        guard_status = "cited"

    return ReviewQuestion(
        question_id=f"question_{uuid.uuid4().hex}",
        what=what,
        why=why,
        options=tuple(options),
        citations=tuple(citations),
        evidence_guard=guard_status,
        fallback=False,
    )


@dataclass(frozen=True, slots=True)
class _CitationCarrier:
    """Minimal shape CiteGuard needs, so review copy reuses the same guard."""

    citations: tuple[EvidenceCitation, ...]
