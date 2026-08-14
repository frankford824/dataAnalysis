"""把五家店 2026-05 的损益表和质量列摘成人能看的一页。"""
import json
from pathlib import Path

STORES = [
    ("taobao_xibishun", "淘宝喜必顺"),
    ("douyin_qianhuajian", "抖音浅花涧"),
    ("jd_huanglishi", "京东皇莉诗"),
    ("pdd_kuailejieqing", "pdd快乐节庆"),
    ("alibaba1688_xingze", "1688星泽"),
]

for sid, label in STORES:
    p = Path(f"/tmp/pd_{sid}.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    print("=" * 100)
    print(f"{label}  ({sid})  账期 {d.get('period')}  状态 {d.get('state')}")

    stmt = d.get("statement") or []
    blanks, zeros = [], []
    print("  -- 损益表")
    for row in stmt:
        avail = row.get("available")
        val = row.get("value")
        mark = ""
        if not avail:
            mark = "  <- 空值"
            blanks.append(row.get("name"))
        elif val == 0:
            mark = "  <- 0.00"
            zeros.append(row.get("name"))
        shown = "" if val is None else format(val, ",.2f")
        print(f"     L{row.get('level')} {str(row.get('name')):<16} {shown:>16}{mark}")
    print(f"  空值项 {len(blanks)}：{'、'.join(blanks) or '（无）'}")
    print(f"  零值项 {len(zeros)}：{'、'.join(zeros) or '（无）'}")

    print("  -- 质量")
    for q in d.get("quality") or []:
        cov = q.get("coverage")
        hit = q.get("hit_rate")
        print(
            f"     {str(q.get('name')):<16}"
            f" 覆盖 {('-' if cov is None else format(cov, '.1%')):>7}"
            f"  命中 {('-' if hit is None else format(hit, '.1%')):>7}"
            f"  行 {q.get('total_rows', 0):>8,}"
            f"  {'公司级' if q.get('company_wide') else ''}"
        )
