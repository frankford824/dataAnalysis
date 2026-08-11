"""比对：finance-win 上算出来的账，和本机的回放基线是不是同一个数。

同一份输入在两台机器上必须得出同一个结果，否则"部署"这件事本身就在改账。
两边差异的来源现实存在：Windows 默认 cp936 编码、python-calamine 在不同平台上
读同一个 xlsx 的浮点、Decimal 的舍入路径。这三样都可能只差一分钱，而一分钱
在对账上就是一整天的排查。

用法：python3 crosscheck.py <基线json> <线上overview的json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def flat(statement: list[dict]) -> dict[str, float | None]:
    return {n["id"]: n.get("value") for n in statement}


def load_baseline(path: Path) -> tuple[str, dict[tuple[str, str], dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict] = {}
    for store_id, periods in raw["stores"].items():
        for period, body in periods.items():
            out[(store_id, period)] = flat(body.get("statement", []))
    return raw.get("engine_version", "?"), out


def load_live(paths: list[Path]) -> dict[tuple[str, str], dict]:
    """线上的每个店期一个文件，来自 /api/stores/{id}/{period}。"""
    out: dict[tuple[str, str], dict] = {}
    for p in paths:
        raw = json.loads(p.read_text(encoding="utf-8"))
        d = raw.get("detail", raw)
        out[(d["store_id"], d["period"])] = flat(d.get("statement", []))
    return out


def main() -> int:
    engine, base = load_baseline(Path(sys.argv[1]))
    live = load_live([Path(p) for p in sys.argv[2:]])

    print(f"基线录于引擎 {engine}；基线 {len(base)} 个店期，线上 {len(live)} 个店期")

    only_base = sorted(set(base) - set(live))
    only_live = sorted(set(live) - set(base))
    for k in only_base:
        print(f"  只在基线里：{k[0]} {k[1]}")
    for k in only_live:
        print(f"  只在线上有：{k[0]} {k[1]}")

    bad = 0
    for key in sorted(set(base) & set(live)):
        b, l = base[key], live[key]
        diffs = []
        for node in sorted(set(b) | set(l)):
            bv, lv = b.get(node), l.get(node)
            if bv is None and lv is None:
                continue
            if bv is None or lv is None:
                diffs.append(f"{node}: 基线 {bv} / 线上 {lv}")
            elif abs(float(bv) - float(lv)) > 1e-9:
                diffs.append(f"{node}: 基线 {bv} / 线上 {lv}  差 {float(lv) - float(bv):+.4f}")
        if diffs:
            bad += 1
            print(f"\n  {key[0]} {key[1]} 有 {len(diffs)} 处不一致：")
            for d in diffs:
                print(f"      {d}")
        else:
            print(f"  {key[0]} {key[1]}：{len(b)} 个科目全部一致")

    if bad or only_base or only_live:
        print("\n不一致。部署不能就这样上。")
        return 1
    print("\n两台机器算出来是同一份账。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
