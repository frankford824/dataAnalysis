"""核对订单明细表的口径：直接验算文档写的公式，对不上再反解。

订单明细表第一行是人手写的公式说明，不能直接信：列名有漂移（写"服务费"实际列名是
"软件服务费"），抖音那张的利润公式还明显漏了几列。但利润列和各分项列的数值都在表里，
可以逐行验算。

三步：
  1. 直接算文档公式，看逐行残差
  2. 对不上就在分项列上反解系数（排除 ID 列，它们量级 1e18 会毁掉条件数）
  3. 报告哪些列真的参与了、系数是多少

这个脚本不进产品，它是建模阶段的核对工具。
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.normalize import to_number  # noqa: E402

#: 超过这个量级的列当作标识符，不参与金额运算。订单编号是 1e18 量级。
ID_MAGNITUDE = 1e12


def read_table(path: Path, sheet: str | None, header_row: int) -> tuple[list[str], list[list]]:
    wb = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        ws.reset_dimensions = True
        headers: list[str] = []
        rows: list[list] = []
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


def money_columns(headers: list[str], rows: list[list]) -> dict[str, list[float]]:
    """金额列：九成以上能解成数，且量级不像标识符。"""
    out: dict[str, list[float]] = {}
    for i, name in enumerate(headers):
        if not name:
            continue
        nums = [to_number(r[i] if i < len(r) else None) for r in rows]
        ok = [n for n in nums if n is not None]
        if len(ok) / max(len(nums), 1) < 0.9 or not ok:
            continue
        if max(abs(n) for n in ok) > ID_MAGNITUDE:
            continue
        out[name] = [n if n is not None else 0.0 for n in nums]
    return out


def check(label: str, target: list[float], terms: list[tuple[int, str]], cols: dict[str, list[float]]) -> None:
    """验算一条公式。terms 是 (系数, 列名) 列表。"""
    missing = [n for _, n in terms if n not in cols]
    if missing:
        print(f"  {label}: 缺列 {'、'.join(missing)}，无法验算")
        return
    resid = []
    for r in range(len(target)):
        got = math.fsum(c * cols[n][r] for c, n in terms)
        resid.append(target[r] - got)
    off = [abs(x) for x in resid if abs(x) > 0.01]
    worst = max((abs(x) for x in resid), default=0.0)
    expr = " ".join(f"{'+' if c > 0 else '-'} {n}" for c, n in terms).lstrip("+ ")
    verdict = "对得上" if not off else f"对不上 {len(off)}/{len(target)} 行"
    print(f"  {label}: {verdict}，最大残差 {worst:.4f}")
    print(f"      = {expr}")
    if off:
        off.sort(reverse=True)
        total_gap = math.fsum(resid)
        print(f"      残差合计 {total_gap:,.2f}，最大几个 {[round(x, 2) for x in off[:5]]}")


def solve(target: list[float], cands: dict[str, list[float]], names: list[str]) -> None:
    """在给定列上反解系数。系数应落在 {-1, 0, +1}。"""
    import numpy as np

    usable = [n for n in names if n in cands and any(abs(x) > 1e-9 for x in cands[n])]
    if not usable:
        print("      没有可用于反解的非零列")
        return
    A = np.array([cands[n] for n in usable], dtype=float).T
    y = np.array(target, dtype=float)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    snapped = np.round(coef)
    resid = y - A @ snapped
    bad = int(np.sum(np.abs(resid) > 0.01))
    print(f"      反解：吸附后对不上 {bad}/{len(y)} 行，最大残差 {np.max(np.abs(resid)):.4f}")
    for n, c, s in zip(usable, coef, snapped):
        flag = "" if abs(c - s) < 0.02 else f"   ← 非整数系数 {c:+.4f}，说明这一项经过分摊或二次加工"
        print(f"        {int(s):+d}  {n}{flag}")


# --------------------------------------------------------------------------- #
# 三个平台的实际口径。列名取自表格第 2 行，公式取自第 1 行的说明。
# --------------------------------------------------------------------------- #

SPECS = {
    "taobao": {
        "path": "/home/wsfwk/data/platform/订单明细/订单明细-淘宝喜必顺.xlsx",
        "sheet": "5月",
        "header_row": 1,
        "components": [
            "销售收入", "销售退款", "软件服务费", "物流运费", "交易赔付", "营销费用",
            "发货运费", "推广费用", "客服打款", "刷单/本金佣金",
            "代发成本", "聚水潭成本", "补发成本",
        ],
        "formulas": {
            "毛利": [(1, "销售收入"), (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本")],
            # 文档原文：销售收入+销售退款+服务费-物流运费+平台费用+营销支出+小额打款
            #           -发货运费-推广费用-客服打款-刷单/本金佣金-代发成本-聚水潭成本-补发成本
            # "服务费"对应列名"软件服务费"，"平台费用"对应"交易赔付"，"营销支出"对应"营销费用"，
            # "小额打款"与"客服打款"在文档里同时出现且一加一减，按列名只有"客服打款"一列。
            "利润": [
                (1, "销售收入"), (1, "销售退款"), (1, "软件服务费"), (-1, "物流运费"),
                (1, "交易赔付"), (1, "营销费用"), (-1, "发货运费"), (-1, "推广费用"),
                (-1, "客服打款"), (-1, "刷单/本金佣金"),
                (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本"),
            ],
        },
    },
    "alibaba1688": {
        "path": "/home/wsfwk/data/platform/订单明细/订单明细-1688星泽气球派对.xlsx",
        "sheet": "Sheet1",
        "header_row": 1,
        "components": [
            "销售收入", "销售支出", "小额打款", "发货运费", "推广费用",
            "刷单/本金佣金", "代发成本", "聚水潭成本", "补发成本",
        ],
        "formulas": {
            "毛利": [(1, "销售收入"), (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本")],
            "利润": [
                (1, "销售收入"), (-1, "销售支出"), (-1, "小额打款"), (-1, "发货运费"),
                (-1, "推广费用"), (-1, "刷单/本金佣金"),
                (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本"),
            ],
        },
    },
    "douyin": {
        "path": "/home/wsfwk/data/platform/订单明细/订单详情-抖音浅花涧节日装饰.xlsx",
        "sheet": "订单明细",
        "header_row": 1,
        "components": [
            "销售收入", "销售退款", "服务费", "物流服务费", "物流运费", "营销支出",
            "小额打款", "发货运费", "推广费用", "刷单/本金佣金",
            "代发成本", "聚水潭成本", "补发成本",
        ],
        "formulas": {
            "毛利": [(1, "销售收入"), (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本")],
            # 文档原文只写了 8 项：销售收入-小额打款-发货运费-推广费用-刷单/本金佣金
            #                    -代发成本-聚水潭成本-补发成本
            # 表里另有 销售退款/服务费/物流服务费/物流运费/营销支出 五列没进公式，这里按原文验算。
            "利润": [
                (1, "销售收入"), (-1, "小额打款"), (-1, "发货运费"), (-1, "推广费用"),
                (-1, "刷单/本金佣金"), (-1, "代发成本"), (-1, "聚水潭成本"), (-1, "补发成本"),
            ],
        },
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="*", default=list(SPECS))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    for key in args.which:
        spec = SPECS[key]
        path = Path(spec["path"])
        headers, rows = read_table(path, spec.get("sheet"), spec["header_row"])
        cols = money_columns(headers, rows)
        print(f"\n{'=' * 96}\n{key}  {path.name} · {spec.get('sheet')}"
              f"   {len(rows):,} 行，金额列 {len(cols)} 个\n{'=' * 96}")
        if args.list:
            for n, v in cols.items():
                pos = sum(1 for x in v if x > 0); neg = sum(1 for x in v if x < 0)
                print(f"   {n:<20} 正{pos:>6} 负{neg:>6}  合计 {math.fsum(v):>14,.2f}")
            continue

        for target, terms in spec["formulas"].items():
            if target not in cols:
                print(f"  目标列 {target} 不在金额列里")
                continue
            check(target, cols[target], terms, cols)
            solve(cols[target], cols, spec["components"])
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
