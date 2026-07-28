#!/usr/bin/env python3
"""Fail CI when harness/web ships forbidden technical jargon."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from commerce_harness.copy_gate import SCANNED_SUFFIXES, scan_source  # noqa: E402


def _is_test(path: Path) -> bool:
    return ".test." in path.name or ".spec." in path.name


def main() -> int:
    web_src = ROOT / "web" / "src"
    if not web_src.is_dir():
        print(f"copy-gate: missing {web_src}", file=sys.stderr)
        return 1
    hits: list[str] = []
    scanned = 0
    for path in sorted(web_src.rglob("*")):
        if path.suffix not in SCANNED_SUFFIXES or _is_test(path):
            continue
        scanned += 1
        for violation in scan_source(path.read_text(encoding="utf-8")):
            hits.append(f"{path.relative_to(ROOT)}: {violation}")
    if hits:
        print("copy-gate FAILED — 界面出现技术黑话：", file=sys.stderr)
        for hit in hits[:50]:
            print(f"  {hit}", file=sys.stderr)
        if len(hits) > 50:
            print(f"  ... 另有 {len(hits) - 50} 处", file=sys.stderr)
        return 1
    print(f"copy-gate OK（已扫描 web/src 下 {scanned} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
