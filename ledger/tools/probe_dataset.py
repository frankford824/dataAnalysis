"""深挖一个数据集：表头、行数、重复列、取值分布、符号约定、键唯一性。

写模板之前必须先知道真实数据长什么样。这个脚本回答的问题正好是模板要声明的东西：
哪些列是金额、金额的符号约定是什么、哪个列能当关联键、键唯一不唯一、日期落在哪个区间。

它同时是解析原语的实战检验。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.normalize import to_date, to_number  # noqa: E402
from ledger.engine.parse import ParseError, parse  # noqa: E402
from ledger.model.schema import normalize_header  # noqa: E402

#: 判定一列是不是金额列：列名里带这些词，或者九成以上的值能解成数。
MONEY_HINT = re.compile(r"(金额|费用|费$|价|款|收入|支出|支付|花费|成本|佣金|返|扣|额$|服务费|运费|余额)")
QTY_HINT = re.compile(r"(数量|件数|个数|笔数|次数|库存)")
KEY_HINT = re.compile(r"(订单|单号|编号|流水|交易号|商品ID|商品id|宝贝|SKU|编码|货号)")
DATE_HINT = re.compile(r"(时间|日期|日$|月$)")


def profile_column(name: str, values: list) -> dict:
    """一列的画像。空值不参与统计。"""
    nonnull = [v for v in values if v not in (None, "")]
    out: dict = {
        "name": name,
        "nonnull": len(nonnull),
        "fill_rate": round(len(nonnull) / len(values), 4) if values else 0.0,
        "distinct": len({str(v) for v in nonnull}),
    }
    if not nonnull:
        out["kind"] = "empty"
        return out

    sample = nonnull[:5000]
    nums = [to_number(v) for v in sample]
    numeric = [n for n in nums if n is not None]
    numeric_ratio = len(numeric) / len(sample)

    dates = [to_date(v) for v in sample[:2000]]
    date_ok = [d for d in dates if d is not None]
    date_ratio = len(date_ok) / max(len(dates), 1)

    if DATE_HINT.search(name) and date_ratio > 0.8:
        out["kind"] = "date"
        out["min"] = str(min(date_ok))
        out["max"] = str(max(date_ok))
        out["unparsed"] = len(dates) - len(date_ok)
        return out

    if numeric_ratio > 0.9 and (MONEY_HINT.search(name) or QTY_HINT.search(name)):
        pos = sum(1 for n in numeric if n > 0)
        neg = sum(1 for n in numeric if n < 0)
        zero = sum(1 for n in numeric if n == 0)
        out.update(
            kind="money" if MONEY_HINT.search(name) else "quantity",
            positive=pos, negative=neg, zero=zero,
            # 符号约定：全正、全负还是正负混存。混存意味着符号不能从数据推断。
            sign=("all_positive" if neg == 0 and pos else
                  "all_negative" if pos == 0 and neg else
                  "mixed" if pos and neg else "all_zero"),
            min=round(min(numeric), 4), max=round(max(numeric), 4),
            total=round(sum(numeric), 2),
            unparsed=len(sample) - len(numeric),
        )
        return out

    out["kind"] = "key" if KEY_HINT.search(name) else "text"
    out["unique"] = out["distinct"] == len(nonnull)
    counts = collections.Counter(str(v) for v in nonnull)
    out["top"] = [[k, c] for k, c in counts.most_common(6)]
    if out["kind"] == "key":
        out["dupes"] = len(nonnull) - out["distinct"]
        widths = collections.Counter(len(str(v)) for v in nonnull)
        out["length_dist"] = dict(sorted(widths.items())[:6])
    return out


def probe_file(path: Path, max_rows: int) -> list[dict]:
    try:
        tables = parse(path)
    except ParseError as exc:
        return [{"file": path.name, "error": str(exc)}]

    out = []
    for table in tables:
        headers = table.headers
        norm = [normalize_header(h) for h in headers]
        dupes = [h for h, c in collections.Counter(n for n in norm if n).items() if c > 1]
        rows = table.rows[:max_rows] if max_rows else table.rows
        columns = []
        for i, h in enumerate(headers):
            if not normalize_header(h):
                continue
            values = [r.cells[i] if i < len(r.cells) else None for r in rows]
            columns.append(profile_column(h, values))
        out.append(
            {
                "file": path.name,
                "sheet": table.ref.sheet,
                "rows": len(table.rows),
                "width": len(headers),
                "duplicate_columns": dupes,
                "notes": table.notes,
                "columns": columns,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out", default="/tmp/dataset-probe.json")
    ap.add_argument("--max-rows", type=int, default=0, help="每表最多画像多少行，0 为全量")
    ap.add_argument("--filter", default="", help="只看文件名含这个词的")
    args = ap.parse_args()

    root = Path(args.root)
    files = sorted(p for p in root.rglob("*") if p.is_file() and args.filter in p.name)
    results = []
    for p in files:
        print(f"·· {p.relative_to(root)}", flush=True)
        for rec in probe_file(p, args.max_rows):
            rec["category"] = p.parent.name
            rec["path"] = str(p.relative_to(root))
            results.append(rec)

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(files)} 个文件 → {len(results)} 张表，结果写到 {args.out}")

    errs = [r for r in results if r.get("error")]
    if errs:
        print(f"\n读不出来 {len(errs)} 个：")
        for r in errs:
            print(f"  {r['file']}: {r['error'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
