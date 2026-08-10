"""把我导入的科目字典和他们表里自己算出来的『费项』列逐行比对。

对账表里的 费项 列是他们用 XLOOKUP 查运营链接算出来的，等于一份现成的答案。
逐行比对能立刻定位：是我的字典导漏了，还是他们的数据有问题，还是两边口径不同。
"""

from __future__ import annotations

import collections
import io
import math
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.model import load_model  # noqa: E402
from ledger.engine.normalize import to_number  # noqa: E402

ROOT = Path("/home/wsfwk/data/platform")
MODEL = Path(__file__).resolve().parents[2] / "models/cn-ecommerce"


def load(rel: str, sheet: str, header_row: int = 1):
    wb = openpyxl.load_workbook(io.BytesIO((ROOT / rel).read_bytes()), read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        ws.reset_dimensions = True
        headers, rows = [], []
        for i, values in enumerate(ws.iter_rows(values_only=True)):
            if i == header_row:
                headers = [str(v).strip() if v is not None else "" for v in values]
                continue
            if i < header_row or not any(v not in (None, "") for v in values):
                continue
            rows.append(list(values))
        return headers, rows
    finally:
        wb.close()


def col(headers, rows, name, occurrence=0):
    idxs = [i for i, h in enumerate(headers) if h == name]
    if len(idxs) <= occurrence:
        return None
    i = idxs[occurrence]
    return [r[i] if i < len(r) else None for r in rows]


def main() -> int:
    model = load_model(MODEL)
    print(f"字典 {len(model.dictionary)} 条\n")

    for sheet, desc_col, item_col, item2_col, inc, outp in (
        ("支付宝", "业务描述", "费项", "费项2", "收入金额（+元）", "支出金额（-元）"),
        ("微信", "业务描述", "费项", None, "收入金额(元)", "支出金额(元)"),
    ):
        headers, rows = load("对账/对账-淘宝喜必顺.xlsx", sheet)
        descs = col(headers, rows, desc_col)
        theirs = col(headers, rows, item_col)
        theirs2 = col(headers, rows, item2_col) if item2_col else None
        income = [to_number(v) or 0.0 for v in (col(headers, rows, inc) or [])]
        outgo = [to_number(v) or 0.0 for v in (col(headers, rows, outp) or [])]
        remarks = col(headers, rows, "备注")
        btype = col(headers, rows, "业务类型") or col(headers, rows, "入帐类型")

        print("=" * 96)
        print(f"{sheet}  {len(rows):,} 行")
        print("=" * 96)

        labels = {v: k for k, v in (model_major_labels() or {}).items()}
        agree = disagree = 0
        mine_missing: dict[str, tuple[int, float]] = {}
        theirs_missing = 0
        mismatch: dict[tuple[str, str], tuple[int, float]] = {}
        empty_desc: dict[str, tuple[int, float]] = {}

        for i in range(len(rows)):
            raw = str(descs[i] or "").strip()
            amount = income[i] + outgo[i]
            their = str(theirs[i] or "").strip() if theirs else ""
            their2 = str(theirs2[i] or "").strip() if theirs2 else ""
            effective = their2 or their

            if not raw:
                # 业务描述为空的行：看他们靠什么归类
                key = f"业务类型={btype[i] if btype else ''} 费项={effective or '(空)'}"
                c, a = empty_desc.get(key, (0, 0.0))
                empty_desc[key] = (c + 1, a + amount)
                continue

            entry = model.lookup("taobao", raw)
            mine = labels.get(entry.major, entry.major) if entry else ""

            if not effective or effective == "0":
                theirs_missing += 1
                continue
            if not mine:
                c, a = mine_missing.get(raw, (0, 0.0))
                mine_missing[raw] = (c + 1, a + amount)
                continue
            if mine == effective:
                agree += 1
            else:
                disagree += 1
                k = (raw[:40], f"我={mine} 他们={effective}")
                c, a = mismatch.get(k, (0, 0.0))
                mismatch[k] = (c + 1, a + amount)

        print(f"  一致 {agree:,}  不一致 {disagree:,}  我查不到 "
              f"{sum(c for c, _ in mine_missing.values()):,}  他们也空 {theirs_missing:,}  "
              f"业务描述为空 {sum(c for c, _ in empty_desc.values()):,}")

        if mine_missing:
            print(f"\n  我的字典查不到（{len(mine_missing)} 种）：")
            for raw, (c, a) in sorted(mine_missing.items(), key=lambda kv: -abs(kv[1][1]))[:10]:
                # 他们给这个科目归的类
                theirs_for = collections.Counter(
                    str(theirs[i] or "").strip() for i in range(len(rows))
                    if str(descs[i] or "").strip() == raw
                )
                print(f"    {raw[:48]:<50} {c:>6} 行 {a:>12,.2f}   他们归为 {dict(theirs_for)}")

        if mismatch:
            print(f"\n  归类不一致（{len(mismatch)} 种）：")
            for (raw, how), (c, a) in sorted(mismatch.items(), key=lambda kv: -abs(kv[1][1]))[:10]:
                print(f"    {raw:<42} {how:<34} {c:>6} 行 {a:>12,.2f}")

        if empty_desc:
            print(f"\n  业务描述为空的行（{len(empty_desc)} 种组合）：")
            for key, (c, a) in sorted(empty_desc.items(), key=lambda kv: -abs(kv[1][1]))[:10]:
                print(f"    {key[:62]:<64} {c:>6} 行 {a:>12,.2f}")
        print()
    return 0


def model_major_labels() -> dict[str, str]:
    import yaml

    rules = yaml.safe_load((MODEL / "asset-import.yaml").read_text(encoding="utf-8"))
    return rules.get("major_labels", {})


if __name__ == "__main__":
    raise SystemExit(main())
