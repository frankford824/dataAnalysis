"""Forbidden-word vocabulary and scanner for the human-facing UI.

The CI gate scans all of ``harness/web/src`` so technical jargon cannot ship
into the operator's screen. Jargon and code are separated by intent, not by
directory:

* Chinese jargon has no reason to exist anywhere in the frontend, so it is
  banned in every scanned file.
* English tokens like ``partial`` or ``reason_code`` are legitimate field and
  type names, so they are only banned where the user can read them: JSX text
  and string literals that carry Chinese copy.
"""

from __future__ import annotations

import re

FORBIDDEN_UI_TERMS: dict[str, str | None] = {
    "不变量": "检查项",
    "未决差额": "对不上的账",
    "残差": "对不上的账",
    "影子运行": "试算",
    "反事实": "试算",
    "规则晋升": "以后同类我自己处理",
    "trust_tier": "敢认到什么程度",
    "trust tier": "敢认到什么程度",
    "partial": "只敢认一部分",
    "blocked": "这个月我不敢出数",
    "certified": "已认证",
    "归一化": "整理后的数据",
    "快照": "原始文件存档",
    "指纹": None,
    "语料": None,
    "posting_target": None,
    "reason_code": None,
    "participation": None,
}

# Longest first so a violation is reported against the most specific term.
FORBIDDEN_SCAN_TOKENS: tuple[str, ...] = tuple(
    sorted(FORBIDDEN_UI_TERMS, key=len, reverse=True)
)

SCANNED_SUFFIXES = frozenset({".ts", ".tsx", ".css"})

_CJK = re.compile(r"[\u4e00-\u9fff]")
_STRING_LITERAL = re.compile(
    r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`",
    re.DOTALL,
)
_JSX_TEXT = re.compile(r">([^<>{}]*[\u4e00-\u9fff][^<>{}]*)<")


def suggestion(term: str) -> str:
    replacement = FORBIDDEN_UI_TERMS.get(term)
    return f"改用「{replacement}」" if replacement else "不得出现在界面上"


def copy_candidates(text: str) -> list[str]:
    """Strings a user could end up reading: literals and JSX text nodes."""
    candidates: list[str] = []
    for match in _STRING_LITERAL.finditer(text):
        candidates.append(next(group for group in match.groups() if group is not None))
    candidates.extend(match.group(1) for match in _JSX_TEXT.finditer(text))
    return candidates


def scan_source(text: str) -> list[str]:
    """Return one message per forbidden term found in user-facing copy."""
    violations: list[str] = []
    candidates = copy_candidates(text)
    for term in FORBIDDEN_SCAN_TOKENS:
        if not term.isascii():
            if term in text:
                violations.append(f"含技术黑话 '{term}'（{suggestion(term)}）")
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
        for candidate in candidates:
            if _CJK.search(candidate) and pattern.search(candidate):
                violations.append(
                    f"界面文案 {candidate.strip()!r} 含 '{term}'（{suggestion(term)}）"
                )
                break
    return violations
