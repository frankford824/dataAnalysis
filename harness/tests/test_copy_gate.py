"""The copy gate must catch jargon in copy without banning field names."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from commerce_harness.copy_gate import scan_source

HARNESS_ROOT = Path(__file__).resolve().parents[1]


def test_chinese_jargon_is_rejected_anywhere() -> None:
    assert scan_source('const label = "未决差额合计"') != []
    assert scan_source("<p>这是快照</p>") != []


def test_english_field_names_stay_allowed_in_code() -> None:
    code = (
        "type Cell = { trust_tier: 'certified' | 'partial' | 'blocked' }\n"
        "const isBlocked = (cell: Cell) => cell.trust_tier === 'blocked'\n"
    )
    assert scan_source(code) == []


def test_english_jargon_inside_chinese_copy_is_rejected() -> None:
    assert scan_source('const text = "这个月是 blocked 状态"') != []
    assert scan_source("<span>当前 trust_tier 为已认证</span>") != []


def test_repository_ui_passes_the_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS_ROOT / "scripts" / "check_copy_gate.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
