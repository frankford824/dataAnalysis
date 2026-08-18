"""下钻淘宝挂不上订单的那 30 万。

命中率只有 36.4%，审计报「看起来是订单的钱 31,618 笔、-30.29 万」。人工表也没把
这笔算进利润，所以当期账是对的；但"对得上"不等于"说得清"。

口径必须和审计一致，否则数字对不上还以为发现了新问题：源事实里一张对账表被七个
指标各求值一遍，同一物理行出现七次，要先按 audit 的规则去重（保留指标口径和这行
科目一致的那条，只有它符号是对的）。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.audit import BUCKET_OTHER_STORES, _one_row_once
from ledger.engine.runtime import ingest, run
from ledger.model.loader import load_model

MODELS = Path(__file__).resolve().parents[2] / "models"
DATA = Path("/home/wsfwk/data/platform")


def show(df: pl.DataFrame, by: str, indent: str = "  ", top: int = 20) -> None:
    g = (
        df.group_by(by)
        .agg(pl.len().alias("rows"), pl.col("amount").sum().fill_null(0.0).alias("amt"))
        .sort("amt")
    )
    for r in g.head(top).to_dicts():
        label = str(r[by] if r[by] not in (None, "") else "（空）")[:44]
        print(f"{indent}{label:<46} {r['rows']:>7,} 行  {float(r['amt'] or 0):>14,.2f}")


def main() -> None:
    model = load_model(MODELS / "cn-ecommerce")
    store = model.store("taobao_xibishun")
    found = list(DATA.rglob("*.xlsx"))
    mine = set(model.files_of(store.id, (p.name for p in found)))
    files = [p for p in found if p.name in mine]
    result = run(ingest(files, model, [store.name]), store.platform)

    unl = _one_row_once(result.facts.filter(~pl.col("linked")), model)
    company_wide = {s.id for s in model.sources if s.company_wide}
    natural_raw = {e.raw for e in model.dictionary if e.naturally_unlinked}
    natural_metrics = {m.id for m in model.metrics if m.naturally_unlinked}

    mine = unl.filter(
        ~pl.col("source_id").is_in(list(company_wide))
        & ~pl.col("metric_id").is_in(list(natural_metrics))
        & ~pl.col("subject").is_in(list(natural_raw)).fill_null(False)
    )
    print(f"审计口径的「看起来是订单的钱」：{mine.height:,} 行  "
          f"{mine.select(pl.col('amount').sum()).item():,.2f}")
    print(f"（另有公司级主表 {unl.filter(pl.col('source_id').is_in(list(company_wide))).height:,} 行"
          f"，归 {BUCKET_OTHER_STORES}，不算本店）\n")

    print("=" * 92)
    print("一、钱从哪张表来")
    print("=" * 92)
    show(mine, "source_id")

    print()
    print("=" * 92)
    print("二、归成了什么口径项")
    print("=" * 92)
    show(mine, "major")

    print()
    print("=" * 92)
    print("三、为什么挂不上")
    print("=" * 92)

    def bucket(k: object) -> str:
        s = "" if k is None else str(k)
        if s == "__excluded__":
            return "规则链显式排除"
        if not s:
            return "取不出订单号"
        return "订单号取到了，本店订单明细里没有"

    b = mine.with_columns(
        pl.col("link_key").map_elements(bucket, return_dtype=pl.Utf8).alias("why")
    )
    show(b, "why")

    for why in ("订单号取到了，本店订单明细里没有", "取不出订单号", "规则链显式排除"):
        part = b.filter(pl.col("why") == why)
        if part.is_empty():
            continue
        print(f"\n  ── {why}：{part.height:,} 行  "
              f"{part.select(pl.col('amount').sum()).item():,.2f}")
        print("     按口径项：")
        show(part, "major", indent="       ", top=10)
        print("     按原始科目：")
        show(part, "subject", indent="       ", top=10)

    ghost = b.filter(pl.col("why") == "订单号取到了，本店订单明细里没有")
    if not ghost.is_empty():
        keys = ghost.get_column("link_key").unique().to_list()
        print(f"\n  这些订单号涉及 {len(keys):,} 个单号。样例：{[str(k) for k in keys[:5]]}")
        lens: dict[int, int] = defaultdict(int)
        for k in keys:
            lens[len(str(k))] += 1
        print(f"  长度分布：{dict(sorted(lens.items()))}")
        spine = result.spine
        for col in ("order_id", "sub_order_id"):
            if col in spine.columns:
                mk = spine.get_column(col).cast(pl.Utf8).drop_nulls().unique().to_list()
                ml: dict[int, int] = defaultdict(int)
                for k in mk:
                    ml[len(str(k))] += 1
                print(f"  本店 {col} 长度分布：{dict(sorted(ml.items()))}（{len(mk):,} 个）")

    print()
    print("=" * 92)
    print("四、取键规则链各环命中")
    print("=" * 92)
    tpl = next((t for t in model.templates if t.id == "taobao_settlement_alipay_v1"), None)
    for (_, period), sl in result.slices.items():
        rep = sl.link_reports.get("trade_receipt")
        if rep is None or rep.chain is None:
            continue
        print(f"  {period} 命中率 {rep.hit_rate:.1%}  覆盖率 {rep.coverage:.1%}")
        hits = getattr(rep.chain, "hits", {})
        for idx in sorted(hits):
            note = ""
            if tpl and 1 <= idx <= len(tpl.key_rules):
                note = (tpl.key_rules[idx - 1].note or "").strip().replace("\n", " ")[:50]
            print(f"    第 {idx:>2} 条 {hits[idx]:>8,} 行   {note}")
        print(f"    显式排除 {getattr(rep.chain, 'excluded', 0):,} 行，"
              f"一条都没命中 {getattr(rep.chain, 'unmatched', 0):,} 行")


if __name__ == "__main__":
    main()
