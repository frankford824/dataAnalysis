"""一批新数据进不进得来：走产品自己的识别链路问一遍。

新给一批表的时候，第一个要回答的问题是「哪些认得出来、哪些认不出来、为什么」。
拿眼睛比对表头和 templates.yaml 是不作数的：`match_headers` 比的是归一化之后的列名
集合，模板还可能声明表头不在第一行、要重解一次、或者判成人工加工产物直接不进账。
这几步任何一步的结论都和肉眼看不一样，所以这里直接调 `ingest()`——和上传接口、
命令行走的是同一条路，它说认识才是真认识。

只读，不写工作区，不算账。要看算出来多少钱用 `ledger run` 或 tools/accept.py。

    .venv/bin/python tools/intake_survey.py /home/wsfwk/data/platform-20260814
    .venv/bin/python tools/intake_survey.py <目录> --store 淘宝喜必顺   # 只看一家店
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.recognize import infer_period, infer_store  # noqa: E402
from ledger.engine.runtime import ingest  # noqa: E402
from ledger.model import load_model  # noqa: E402

MODEL = Path(__file__).resolve().parents[2] / "models/cn-ecommerce"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="文件或目录")
    ap.add_argument("--model", type=Path, default=MODEL)
    ap.add_argument("--store", action="append", help="只看这家店的文件，可重复")
    ap.add_argument("--headers", action="store_true", help="认不出来的表把表头原样打出来")
    args = ap.parse_args()

    model = load_model(args.model)
    known = [name for s in model.stores for name in (s.name, *s.aliases)]

    root = Path(args.root)
    files = sorted(p for p in root.rglob("*.xlsx") if p.is_file() and not p.name.startswith("~$")) \
        if root.is_dir() else [root]
    if args.store:
        files = [p for p in files if any(s in p.name for s in args.store)]

    print(f"模型 {model.name}：模板 {len(model.templates)}、数据源 {len(model.sources)}、"
          f"店铺 {len(model.stores)}")
    print(f"输入 {len(files)} 个文件\n")

    result = ingest([str(p) for p in files], model, known_stores=known)
    print(result.summary() + "\n")

    print("=" * 110)
    print("认出来的表")
    print("=" * 110)
    by_source: Counter[str] = Counter()
    for item in result.known:
        by_source[item.recognition.source_id] += 1
        # 认表和认店是两件事：认表看表头，认店看文件名。文件名里的店名没配进
        # stores.yaml 的别名时，表照样认得出来，只是这张表的钱一分都落不到店上。
        store = infer_store(item.ref.filename, known) or "（文件名认不出店）"
        print(f"  {item.template.name:<26} {item.rows:>8,} 行  店={store:<18} {item.ref.label()[:44]}")
        for n in item.notes:
            print(f"        · {n}")

    print("\n" + "=" * 110)
    print("认不出来的表")
    print("=" * 110)
    if not result.unknown:
        print("  （没有）")
    for item in result.unknown:
        kind = "人工加工产物" if item.derivative else "无匹配模板"
        print(f"  [{kind}] {item.ref.label()}")
        print(f"        {item.error or item.recognition.reason}")
        if args.headers:
            print(f"        表头：{item.recognition.signature[:200]}")

    print("\n" + "=" * 110)
    print("按数据源汇总")
    print("=" * 110)
    for source in model.sources:
        got = by_source.get(source.id, 0)
        flag = "" if got else ("   ← 结账必需，一份都没认出来" if source.required_for_close else "")
        print(f"  {source.name:<16} {got:>3} 份{flag}")

    # 账期是文件名兜底推断出来的，数据里读到的时间优先。这里只报文件名这一层：
    # 一批文件全都推不出账期时，最终落哪个月完全取决于表里的时间列，值得先知道。
    periods = Counter(infer_period(p.name) or "（文件名没有账期）" for p in files)
    print("\n文件名推断出的账期：" + "、".join(f"{k}×{v}" for k, v in periods.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
