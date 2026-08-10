"""看一眼真实 DAX 的形态，为转译器的文法定型。"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "assets/pbix-reverse/index.json"


def main() -> int:
    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    measures = [(r, m) for r, d in idx.items() for m in d["measures"]]
    print(f"报表 {len(idx)} 个，度量值 {len(measures)} 个\n")

    fn = collections.Counter()
    for _, m in measures:
        for f in re.findall(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", m.get("dax") or ""):
            fn[f.upper()] += 1
    print("函数用量：", dict(fn.most_common(20)))

    empty = sum(1 for _, m in measures if not (m.get("dax") or "").strip())
    print(f"空表达式：{empty} 个（现有 BI 的既有错误之一：空度量值被引用）\n")

    names = collections.Counter(m["name"] for _, m in measures)
    print("最常见度量值名：")
    for n, c in names.most_common(24):
        print(f"   {n:<18} {c}")

    wanted = sys.argv[1:] or [
        "订单成本", "广告费", "交易收款", "店铺利润", "利润率", "发货运费",
        "本金佣金", "软件服务费", "代购代发", "补发成本", "营销费用",
        "交易退款", "交易赔付", "收支", "分配率",
    ]
    for name in wanted:
        cands = [m for _, m in measures if m["name"] == name]
        print("\n" + "=" * 78)
        if not cands:
            print(f"[{name}] 没有这个度量值")
            continue
        print(f"[{name}] 共 {len(cands)} 处，表={cands[0]['table']}")
        seen = set()
        for m in cands:
            dax = (m.get("dax") or "").strip()
            key = re.sub(r'"[^"]*"', '"…"', dax)
            if key in seen:
                continue
            seen.add(key)
            print("   " + dax.replace("\n", "\n   ")[:600])
            print("   " + "-" * 60)
            if len(seen) >= 3:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
