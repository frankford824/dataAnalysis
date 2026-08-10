"""从源表重算淘宝订单明细的每个分项列，逐列比对。

这是最硬的验证：订单明细表里的分项列是 Excel SUMIFS 算出来的，如果引擎按同样的
关联键和分摊方式能重算出一样的数，说明模型抓对了；对不上的地方要么是我模型写错，
要么是他们表里有问题——两种都必须查清楚，不允许"差不多就行"。
"""

from __future__ import annotations

import collections
import re
import io
import math
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.link import normalize_key  # noqa: E402
from ledger.engine.normalize import to_number  # noqa: E402

ROOT = Path("/home/wsfwk/data/platform")


def load(rel: str, sheet: str, header_row: int = 1) -> tuple[list[str], list[list]]:
    path = ROOT / rel
    wb = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True)
    try:
        ws = wb[sheet]
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


def drop_total_row(headers: list[str], rows: list[list], key_column: str) -> tuple[list[list], int]:
    """去掉表底的合计行。

    Excel 表底常有一行合计，它会让每一列的总额刚好翻倍。这类行的特征是关联键为空
    但金额列有值——引擎的解析层也需要这条规则，否则所有金额都会算两遍。
    """
    if key_column not in headers:
        return rows, 0
    i = headers.index(key_column)
    kept = []
    dropped = 0
    for r in rows:
        key = normalize_key(r[i] if i < len(r) else None)
        if not key or key in ("合计", "总计", "小计"):
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


class Table:
    """一张表。按列名取值，重复列名按第几个取。"""

    def __init__(self, headers: list[str], rows: list[list]) -> None:
        self.headers = headers
        self.rows = rows
        self._idx: dict[str, list[int]] = collections.defaultdict(list)
        for i, h in enumerate(headers):
            if h:
                self._idx[h].append(i)

    def has(self, name: str) -> bool:
        return name in self._idx

    def col(self, name: str, occurrence: int = 0) -> list:
        i = self._idx[name][occurrence]
        return [r[i] if i < len(r) else None for r in self.rows]

    def keys(self, name: str, occurrence: int = 0) -> list[str]:
        return [normalize_key(v) for v in self.col(name, occurrence)]

    def nums(self, name: str, occurrence: int = 0) -> list[float]:
        return [to_number(v) or 0.0 for v in self.col(name, occurrence)]

    def __len__(self) -> int:
        return len(self.rows)


def sumifs(keys: list[str], values: list[float]) -> dict[str, float]:
    out: dict[str, float] = collections.defaultdict(float)
    for k, v in zip(keys, values):
        if k:
            out[k] += v
    return dict(out)


def compare(label: str, expected: list[float], got: list[float], tol: float = 0.011) -> dict:
    diffs = [e - g for e, g in zip(expected, got)]
    bad = [(i, d) for i, d in enumerate(diffs) if abs(d) > tol]
    total_e, total_g = math.fsum(expected), math.fsum(got)
    print(f"\n  {label}")
    print(f"    表里合计 {total_e:>14,.2f}   重算合计 {total_g:>14,.2f}   差 {total_e - total_g:>12,.2f}")
    if not bad:
        print(f"    逐行完全一致（{len(expected):,} 行）")
    else:
        worst = sorted(bad, key=lambda x: -abs(x[1]))[:5]
        print(f"    对不上 {len(bad):,}/{len(expected):,} 行，"
              f"最大差 {worst[0][1]:,.2f}，样例行号 {[i + 3 for i, _ in worst]}")
    return {"label": label, "bad": len(bad), "total_expected": total_e, "total_got": total_g}


#: 备注里订单号的三种格式：花括号、圆括号、直接拼接。
#: 实测同一列里 `订单{4502194995097067439}打款` 与 `基础软件服务费(2701829496150011052)扣款` 并存。
ORDER_IN_REMARK = re.compile(r"[{(（]\s*(\d{15,25})\s*[})）]")
#: 支付宝商户订单号的一种格式，业务基础订单号为空时从这里取。
MERCHANT_ORDER = re.compile(r"T\d{3}P(\d{10,25})")


def recompute_recon() -> dict[str, dict[str, float]]:
    """从支付宝与微信原始流水重算各口径项，按主订单号汇总。

    返回 口径项 → 主订单号 → 金额。
    """
    from ledger.model import load_model

    model = load_model(Path(__file__).resolve().parents[2] / "models/cn-ecommerce")
    out: dict[str, dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    unmatched: dict[str, tuple[int, float]] = {}

    # -- 支付宝：订单号取业务基础订单号，为空则从商户订单号或备注里提取 --
    ali = Table(*load("对账/对账-淘宝喜必顺.xlsx", "支付宝"))
    income = ali.nums("收入金额（+元）")
    outgo = ali.nums("支出金额（-元）")
    base = ali.col("业务基础订单号")
    merchant = ali.col("商户订单号")
    remark = ali.col("备注")
    desc = ali.col("业务描述")

    stats = {"base": 0, "merchant": 0, "remark": 0, "none": 0}
    for i in range(len(ali)):
        key = normalize_key(base[i])
        if key:
            stats["base"] += 1
        else:
            text = str(merchant[i] or "")
            if m := MERCHANT_ORDER.search(text):
                key, _ = m.group(1), stats.__setitem__("merchant", stats["merchant"] + 1)
            elif m := ORDER_IN_REMARK.search(str(remark[i] or "")):
                key, _ = m.group(1), stats.__setitem__("remark", stats["remark"] + 1)
            else:
                stats["none"] += 1
                continue
        entry = model.lookup("taobao", str(desc[i] or ""))
        amount = income[i] + outgo[i]
        if entry is None:
            label = str(desc[i] or "(空)")
            c, a = unmatched.get(label, (0, 0.0))
            unmatched[label] = (c + 1, a + amount)
            continue
        out[entry.major][key] += amount

    print(f"  支付宝 {len(ali):,} 行：业务基础订单号 {stats['base']:,}、"
          f"商户订单号提取 {stats['merchant']:,}、备注提取 {stats['remark']:,}、"
          f"取不到订单号 {stats['none']:,}")

    # -- 微信：有主订单id 列，不需要提取 --
    wx = Table(*load("对账/对账-淘宝喜必顺.xlsx", "微信"))
    w_income = wx.nums("收入金额(元)")
    w_outgo = wx.nums("支出金额(元)")
    w_main = wx.col("主订单id")
    w_desc = wx.col("业务描述")
    w_remark = wx.col("备注")
    w_none = 0
    for i in range(len(wx)):
        key = normalize_key(w_main[i])
        if not key:
            if m := ORDER_IN_REMARK.search(str(w_remark[i] or "")):
                key = m.group(1)
            else:
                w_none += 1
                continue
        entry = model.lookup("taobao", str(w_desc[i] or ""))
        amount = w_income[i] + w_outgo[i]
        if entry is None:
            label = str(w_desc[i] or "(空)")
            c, a = unmatched.get(label, (0, 0.0))
            unmatched[label] = (c + 1, a + amount)
            continue
        out[entry.major][key] += amount
    print(f"  微信 {len(wx):,} 行：取不到订单号 {w_none:,}")

    if unmatched:
        print(f"  字典未命中 {len(unmatched)} 个科目，共 "
              f"{sum(c for c, _ in unmatched.values()):,} 行、"
              f"{sum(abs(a) for _, a in unmatched.values()):,.2f} 元：")
        for label, (c, a) in sorted(unmatched.items(), key=lambda kv: -abs(kv[1][1]))[:8]:
            print(f"     {label[:44]:<46} {c:>6} 行  {a:>12,.2f}")
    else:
        print("  字典全部命中")
    return {k: dict(v) for k, v in out.items()}


def main() -> int:
    print("载入订单明细（脊柱）…")
    headers, rows = load("订单明细/订单明细-淘宝喜必顺.xlsx", "5月")
    rows, dropped = drop_total_row(headers, rows, "子订单编号")
    detail = Table(headers, rows)
    print(f"  {len(detail):,} 行（去掉 {dropped} 行合计行）")

    sub = detail.keys("子订单编号")
    main_order = detail.keys("主订单编号")
    product = detail.keys("商品ID")
    ratio = detail.nums("收入分配率")

    # 主订单内子订单数、商品ID 出现次数——分摊要用
    main_count = collections.Counter(k for k in main_order if k)
    product_count = collections.Counter(k for k in product if k)

    print(f"\n脊柱结构：")
    print(f"  子订单编号 {len(set(sub)):,} 个唯一值 / {len(sub):,} 行"
          f"  {'行级唯一' if len(set(sub)) == len(sub) else '有重复'}")
    print(f"  主订单编号 {len(main_count):,} 个唯一值，"
          f"一对多分布 {dict(sorted(collections.Counter(main_count.values()).items())[:6])}")
    multi = sum(1 for c in main_count.values() if c > 1)
    print(f"  含多个子订单的主订单 {multi:,} 个（占 {multi / len(main_count):.1%}）")
    print(f"  分配率：等于1 {sum(1 for r in ratio if r == 1):,}，"
          f"等于0 {sum(1 for r in ratio if r == 0):,}，"
          f"区间 [{min(ratio):.4f}, {max(ratio):.4f}]")

    results = []

    # ---------------- 聚水潭成本：子订单级直接挂，不分摊 ----------------
    print("\n" + "=" * 92 + "\n聚水潭成本  聚水潭.总成本 by 线上子订单编号 → 明细.子订单编号（不分摊）")
    jst = Table(*load("聚水潭成本/聚水潭成本-淘宝喜必顺.xlsx", "聚水潭订单5+6月"))
    print(f"  源表 {len(jst):,} 行")
    jst_map = sumifs(jst.keys("线上子订单编号"), jst.nums("总成本"))
    got = [jst_map.get(k, 0.0) for k in sub]
    results.append(compare("聚水潭成本", detail.nums("聚水潭成本"), got))
    hit = sum(1 for k in sub if k in jst_map)
    print(f"    命中率 {hit / len(sub):.2%}（{hit:,}/{len(sub):,} 个子订单在成本表里找到）")

    # ---------------- 推广费用：按商品ID 均分到该商品的订单行 ----------------
    print("\n" + "=" * 92 + "\n推广费用  推广.花费 by 主体ID → 明细.商品ID，再按该商品的行数均分")
    ad = Table(*load("推广/推广-淘宝喜必顺.xlsx", "5月推广"))
    print(f"  源表 {len(ad):,} 行")
    ad_map = sumifs(ad.keys("主体ID"), ad.nums("花费"))
    got = [ad_map.get(p, 0.0) / product_count[p] if p and product_count.get(p) else 0.0 for p in product]
    results.append(compare("推广费用", detail.nums("推广费用"), got))

    # ---------------- 刷单/本金佣金：按主订单号关联，文档说不分摊 ----------------
    print("\n" + "=" * 92 + "\n刷单/本金佣金  刷单.总金额 by 订单号 → 明细.主订单编号")
    brush = Table(*load("刷单/刷单-淘宝喜必顺.xlsx", "刷单"))
    print(f"  源表 {len(brush):,} 行")
    brush_map = sumifs(brush.keys("订单号"), brush.nums("总金额"))
    no_split = [brush_map.get(k, 0.0) for k in main_order]
    even = [brush_map.get(k, 0.0) / main_count[k] if k and main_count.get(k) else 0.0 for k in main_order]
    results.append(compare("刷单（不分摊）", detail.nums("刷单/本金佣金"), no_split))
    results.append(compare("刷单（按子订单数均分）", detail.nums("刷单/本金佣金"), even))
    dup_risk = sum(v for k, v in brush_map.items() if main_count.get(k, 0) > 1)
    inflated = math.fsum(no_split) - math.fsum(brush_map.values())
    print(f"    源表刷单总额 {math.fsum(brush_map.values()):,.2f}，"
          f"不分摊挂上后变成 {math.fsum(no_split):,.2f}，虚增 {inflated:,.2f}")
    print(f"    命中主订单里有 {sum(1 for k in brush_map if main_count.get(k, 0) > 1)} 个含多个子订单，"
          f"涉及金额 {dup_risk:,.2f}")

    # ---------------- 代发成本：按主订单内子订单数均分 ----------------
    print("\n" + "=" * 92 + "\n代发成本  代发.代购成本 by 订单号 → 明细.主订单编号，按主订单内子订单数均分")
    ds = Table(*load("代发/代发-淘宝喜必顺.xlsx", "代发"))
    print(f"  源表 {len(ds):,} 行")
    ds_map = sumifs(ds.keys("订单号"), ds.nums("代购成本"))
    got = [ds_map.get(k, 0.0) / main_count[k] if k and main_count.get(k) else 0.0 for k in main_order]
    results.append(compare("代发成本", detail.nums("代发成本"), got))

    # ---------------- 补发成本：按主订单号关联，文档说不分摊 ----------------
    print("\n" + "=" * 92 + "\n补发成本  补发.总成本 by 原始线上订单号 → 明细.主订单编号")
    re_ship = Table(*load("补发/补发-淘宝喜必顺.xlsx", "补发订单"))
    print(f"  源表 {len(re_ship):,} 行")
    rs_map = sumifs(re_ship.keys("原始线上订单号"), re_ship.nums("总成本"))
    no_split = [rs_map.get(k, 0.0) for k in main_order]
    even = [rs_map.get(k, 0.0) / main_count[k] if k and main_count.get(k) else 0.0 for k in main_order]
    results.append(compare("补发成本（不分摊）", detail.nums("补发成本"), no_split))
    results.append(compare("补发成本（按子订单数均分）", detail.nums("补发成本"), even))
    print(f"    源表补发总额 {math.fsum(rs_map.values()):,.2f}，"
          f"不分摊挂上后 {math.fsum(no_split):,.2f}")

    # ---------------- 发货运费：运费表按运单号回查订单号，再乘分配率 ----------------
    print("\n" + "=" * 92 + "\n发货运费  运费.总金额 by 订单号 → 明细.主订单编号，乘收入分配率")
    freight = Table(*load("运费/运费-淘宝喜必顺.xlsx", "运费"))
    print(f"  源表 {len(freight):,} 行")
    f_map = sumifs(freight.keys("订单号"), freight.nums("总金额"))
    got = [f_map.get(k, 0.0) * ratio[i] for i, k in enumerate(main_order)]
    results.append(compare("发货运费", detail.nums("发货运费"), got))
    # 运费表自己的订单号是靠运单号回查来的，看回查成功率
    fkeys = freight.keys("订单号")
    print(f"    运费表订单号为空/回查失败 {sum(1 for k in fkeys if not k or k == '0'):,} 行"
          f"（占 {sum(1 for k in fkeys if not k or k == '0') / len(fkeys):.1%}）")

    # ---------------- 对账：从原始流水行 + 科目字典重算，而不是用他们的透视表 ----------------
    # 用透视表只能验证"我会不会加法"，用原始行 + 字典才是引擎真正要走的路径，
    # 顺带把导入的科目字典一起验了。
    print("\n" + "=" * 92 + "\n对账  从支付宝与微信原始流水行 + 科目字典重算")
    by_item = recompute_recon()
    for item, target in (("trade_receipt", "销售收入"), ("trade_refund", "销售退款"),
                         ("software_fee", "软件服务费"), ("marketing_fee", "营销费用"),
                         ("trade_compensation", "交易赔付")):
        pmap = by_item.get(item, {})
        got = [pmap.get(k, 0.0) * ratio[i] for i, k in enumerate(main_order)]
        results.append(compare(f"{target}（口径 {item}）", detail.nums(target), got))

    print("\n" + "=" * 92)
    print("汇总：")
    for r in results:
        status = "一致" if r["bad"] == 0 else f"差 {r['bad']} 行"
        print(f"  {r['label']:<28} {status:<14}"
              f" 表里 {r['total_expected']:>13,.2f}  重算 {r['total_got']:>13,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
