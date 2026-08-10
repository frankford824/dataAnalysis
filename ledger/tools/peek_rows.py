"""原样打印一张表最前面几行，用来判断表头到底在第几行。

自动识别表头位置是识别原语的活儿，但写模板之前得人眼确认一次。
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import openpyxl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sheet", default="")
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--cols", type=int, default=0, help="0 为全部列")
    ap.add_argument("--width", type=int, default=70, help="单元格截断宽度")
    args = ap.parse_args()

    path = Path(args.path)
    data = path.read_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = [wb[args.sheet]] if args.sheet else wb.worksheets
    for ws in sheets:
        ws.reset_dimensions = True
        print(f"\n{'=' * 100}\n工作表 {ws.title}\n{'=' * 100}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= args.rows:
                break
            cells = list(row)[: args.cols] if args.cols else list(row)
            print(f"\n--- 第 {i + 1} 行（{len(cells)} 个单元格）---")
            for j, c in enumerate(cells):
                if c is None or str(c).strip() == "":
                    continue
                text = str(c).replace("\n", "⏎")
                print(f"  [{j:>2}] {text[: args.width]}")
    wb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
