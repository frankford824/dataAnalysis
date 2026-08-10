"""把数据集画像读成人能看的报告。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SIGN_LABEL = {
    "all_positive": "全正",
    "all_negative": "全负",
    "mixed": "正负混存",
    "all_zero": "全零",
}


def fmt_column(c: dict) -> str:
    kind = c.get("kind")
    fill = f"{c['fill_rate']:.0%}"
    head = f"    {c['name'][:26]:<28} {kind:<9} 填充{fill:>5}"
    if kind in ("money", "quantity"):
        sign = SIGN_LABEL.get(c.get("sign", ""), c.get("sign", ""))
        extra = (
            f" 正{c['positive']} 负{c['negative']} 零{c['zero']}  {sign:<5}"
            f" [{c['min']:,.2f} … {c['max']:,.2f}]  合计 {c['total']:,.2f}"
        )
        if c.get("unparsed"):
            extra += f"  解不出 {c['unparsed']}"
        return head + extra
    if kind == "date":
        out = f" {c['min']} → {c['max']}"
        if c.get("unparsed"):
            out += f"  解不出 {c['unparsed']}"
        return head + out
    if kind == "key":
        out = f" 唯一值{c['distinct']:,}"
        out += "  行级唯一" if c.get("unique") else f"  重复 {c.get('dupes', 0):,}"
        if c.get("length_dist"):
            out += f"  长度 {c['length_dist']}"
        return head + out
    if kind == "empty":
        return head + "  整列为空"
    top = "、".join(f"{k}({v})" for k, v in (c.get("top") or [])[:4])
    return head + f" 唯一值{c['distinct']:,}  常见: {top[:90]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("probe", default="/tmp/probe-platform.json", nargs="?")
    ap.add_argument("--category", default="")
    ap.add_argument("--min-rows", type=int, default=1)
    ap.add_argument("--kinds", default="", help="只显示这些 kind，逗号分隔")
    args = ap.parse_args()

    data = json.loads(Path(args.probe).read_text(encoding="utf-8"))
    kinds = {k for k in args.kinds.split(",") if k}

    by_cat: dict[str, list[dict]] = {}
    for rec in data:
        by_cat.setdefault(rec.get("category", "?"), []).append(rec)

    for cat, recs in by_cat.items():
        if args.category and args.category not in cat:
            continue
        print(f"\n{'#' * 92}\n# {cat}   {len(recs)} 张表\n{'#' * 92}")
        for rec in recs:
            if rec.get("error"):
                print(f"\n  {rec['file']}  读取失败：{rec['error']}")
                continue
            if rec["rows"] < args.min_rows:
                print(f"\n  {rec['file']} · {rec['sheet']}   {rec['rows']} 行（跳过）")
                continue
            print(f"\n  {rec['file']} · 工作表 {rec['sheet']}"
                  f"   {rec['rows']:,} 行 × {rec['width']} 列")
            if rec.get("duplicate_columns"):
                print(f"    ⚠ 重复列名: {rec['duplicate_columns']}")
            for note in rec.get("notes", []):
                print(f"    note: {note}")
            for c in rec["columns"]:
                if kinds and c.get("kind") not in kinds:
                    continue
                print(fmt_column(c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
