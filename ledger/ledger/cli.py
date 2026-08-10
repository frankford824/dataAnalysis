"""命令行入口。

店长的操作应该只有一件事：把这个月的表交上来。所以主命令只要一个路径，
剩下的——这是哪家店、哪个平台、哪个账期、每张表是什么、按谁的口径算——
全由引擎自己定。要人先填一堆参数才肯算，等于把复杂度又推回给人。

输出分三块，顺序是故意的：
先说能不能结账（结不了就说清缺什么），再出损益表，最后列没进利润的钱。
把「有没有问题」放在数字前面，人才不会拿着一张不完整的表当结论用。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from .engine.runtime import Slice, ingest, run
from .model.loader import ModelError, load_model
from .model.schema import Model, Store, guess_platform

#: 仓库自带的模型。
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"

#: 引擎能解析的文件。别的一律不碰，也不假装能读。
SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb", ".csv", ".zip"}


# --------------------------------------------------------------------------- #
# 排版
# --------------------------------------------------------------------------- #


def _width(text: str) -> int:
    """显示宽度。中文占两格，不算宽度的话表格全是歪的。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def _amount(value: float | None, *, available: bool = True, display: str = "amount") -> str:
    """按节点声明的形态出数。

    数据不全时留破折号——那和「算出来是 0」是两件事，混在一起会让人拿着
    缺数据的表当结论。利润率按百分比出，写成 0.57 得让人自己换算。
    """
    if not available or value is None:
        return "—"
    if display == "percent":
        return f"{value * 100:,.1f}%"
    if display == "count":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _oneline(text: str) -> str:
    """压成一行。模型里的提示语用 YAML 折叠写法，带着换行和缩进空格。"""
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# 文件归属
# --------------------------------------------------------------------------- #


def collect(paths: list[str]) -> list[Path]:
    """把命令行给的路径展开成文件清单。目录递归进去。"""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files.extend(q for q in sorted(p.rglob("*")) if q.suffix.lower() in SUFFIXES)
        elif p.is_file():
            files.append(p)
        else:
            print(f"跳过：{p} 不存在", file=sys.stderr)
    return files


def group_by_store(files: list[Path], model: Model) -> tuple[dict[str, list[Path]], list[Path]]:
    """按店分组。返回 (店铺 id → 文件, 认不出归属的文件)。

    认不出的绝不塞进某家店凑数——那会把一家店的钱记到另一家头上。宁可拦下来问人。
    """
    grouped: dict[str, list[Path]] = defaultdict(list)
    orphans: list[Path] = []
    for f in files:
        store = model.store_of(f.name)
        if store is None:
            orphans.append(f)
        else:
            grouped[store.id].append(f)
    return dict(grouped), orphans


def report_orphans(orphans: list[Path], model: Model) -> None:
    """认不出归属的文件要说清楚，还要给出下一步怎么办。"""
    if not orphans:
        return
    print(f"\n有 {len(orphans)} 个文件认不出是哪家店的，这些数据没进账：")
    for f in orphans:
        print(f"  {f.name}")
    # 文件名形如「类别-店铺名.xlsx」，破折号后面那截就是店名，据此提平台建议。
    guesses: dict[str, str] = {}
    for f in orphans:
        stem = f.stem
        for sep in ("-", "—", "_"):
            if sep in stem:
                candidate = stem.rsplit(sep, 1)[-1].strip()
                if candidate and not model.store_of(candidate):
                    guesses[candidate] = guess_platform(candidate)
                break
    if guesses:
        print("\n  看着像这些店，登记到 stores.yaml 就能算：")
        for name, platform in sorted(guesses.items()):
            hint = f"platform: {platform}" if platform else "platform: 待确认（店名前缀认不出平台）"
            print(f"    {name}  →  {hint}")


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render_slice(sl: Slice, store: Store, model: Model) -> None:
    title = f"{store.name} · {store.platform} · {sl.period or '账期未定'}"
    print("\n" + "═" * 64)
    print(title)
    if store.entity:
        print(f"主体：{store.entity}")
    else:
        print("主体：未配置（stores.yaml 里补上，才能按主体汇总）")
    print("═" * 64)

    _render_audit(sl, model)
    _render_statement(sl, model)
    _render_unlinked(sl)


def _source_name(model: Model, source_id: str) -> str:
    """数据源的中文名。催人补数据时说 order_detail 没人知道那是什么。"""
    return next((s.name for s in model.sources if s.id == source_id), source_id)


def _render_audit(sl: Slice, model: Model) -> None:
    findings = sl.audit.findings
    blocked = [f for f in findings if not f.passed and f.blocking]
    warned = [f for f in findings if not f.passed and not f.blocking]

    if sl.can_close:
        print(f"\n可以结账。{len(findings)} 项自检全部通过。")
    else:
        print(f"\n不能结账：{len(blocked)} 项拦住了。")
    for f in blocked:
        print(f"  ✗ {f.name}：{_oneline(f.message)}")
    for f in warned:
        print(f"  ! {f.name}：{_oneline(f.message)}")

    if sl.completeness.missing:
        print(f"\n缺 {len(sl.completeness.missing)} 项数据：")
        for src in sl.completeness.missing:
            reason = sl.completeness.reasons.get(src, "还没传")
            print(f"  {_source_name(model, src)}——{reason}")


def _render_statement(sl: Slice, model: Model) -> None:
    print("\n损益")
    print("─" * 64)
    for node in model.statement:
        nv = sl.nodes.get(node.id)
        if nv is None:
            continue
        indent = "  " * max(0, nv.level - 1)
        label = f"{indent}{nv.name}"
        amount = _amount(nv.value, available=nv.available, display=nv.display)
        line = f"{_pad(label, 40)}{_pad(amount, 18, right=True)}"
        if nv.is_total:
            print("─" * 64)
        print(line)
        if not nv.available and nv.missing_sources:
            print(f"{'  ' * nv.level}└ 缺 {'、'.join(nv.missing_sources)}，这一项不出数")


def _render_unlinked(sl: Slice) -> None:
    buckets = sl.audit.unlinked_buckets
    if not buckets:
        return
    print(f"\n挂不到本店订单的钱：本店 {sl.audit.unlinked_total:,.2f}")
    for label, count, amount in buckets:
        print(f"  {_pad(label, 30)}{_pad(f'{amount:,.2f}', 16, right=True)}  {count:,} 笔")
    print("  本店那部分不是丢了，是还没归属，查清归属才会进利润。")
    print("  公司级主表那部分交上来就是全公司的，绝大多数属于别家店，不算本店的账。")


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    model = load_model(args.model)
    files = collect(args.paths)
    if not files:
        print("没找到可解析的文件。", file=sys.stderr)
        return 1

    grouped, orphans = group_by_store(files, model)
    if args.store:
        wanted = {s.id for s in model.stores if args.store in (s.id, s.name)}
        if not wanted:
            print(f"没有叫「{args.store}」的店。用 ledger stores 看已登记的。", file=sys.stderr)
            return 1
        grouped = {k: v for k, v in grouped.items() if k in wanted}

    print(f"{len(files)} 个文件，{len(grouped)} 家店")

    payload: list[dict] = []
    closable = 0
    total_slices = 0
    for store_id, store_files in grouped.items():
        store = model.store(store_id)
        ing = ingest(list(store_files), model, [store.name])
        result = run(ing, store.platform)

        if not result.slices:
            print(f"\n{store.name}：{len(store_files)} 个文件都没算出结果。")
            for item in ing.unknown:
                print(f"  认不出：{item.ref.label()}——{item.error or item.recognition.reason}")
            continue

        for (_s, period), sl in sorted(
            result.slices.items(), key=lambda kv: (kv[0][1] or "")
        ):
            total_slices += 1
            closable += 1 if sl.can_close else 0
            if args.json:
                payload.append(_as_dict(sl, store))
            else:
                render_slice(sl, store, model)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        report_orphans(orphans, model)
        print(f"\n{total_slices} 个店期，{closable} 个可以结账。")
    return 0


def _as_dict(sl: Slice, store: Store) -> dict:
    """给 API 和界面用的结构。和终端输出同源，不会两边说法不一致。"""
    return {
        "store": store.name,
        "store_id": store.id,
        "platform": store.platform,
        "entity": store.entity,
        "period": sl.period,
        "can_close": sl.can_close,
        "statement": [
            {
                "id": nv.id, "name": nv.name, "level": nv.level,
                "value": nv.value, "available": nv.available,
                "missing_sources": nv.missing_sources, "is_total": nv.is_total,
            }
            for nv in sl.nodes.values()
        ],
        "findings": [
            {"id": f.check_id, "name": f.name, "passed": f.passed,
             "blocking": f.blocking, "message": f.message}
            for f in sl.audit.findings
        ],
        "missing_sources": sl.completeness.missing,
        "unlinked_total": sl.audit.unlinked_total,
        "unlinked_buckets": [
            {"label": b[0], "count": b[1], "amount": b[2]}
            for b in sl.audit.unlinked_buckets
        ],
    }


def cmd_stores(args: argparse.Namespace) -> int:
    model = load_model(args.model)
    if not model.stores:
        print("店铺注册表是空的。往 stores.yaml 里加店。")
        return 0
    rows = [("店铺", "平台", "法人主体", "状态")]
    for s in model.stores:
        rows.append((
            s.name, s.platform, s.entity or "（未配置）",
            "已归档" if s.archived else "在营",
        ))
    widths = [max(_width(r[i]) for r in rows) for i in range(4)]
    for i, row in enumerate(rows):
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())
        if i == 0:
            print("  ".join("─" * w for w in widths))

    entities = defaultdict(list)
    for s in model.stores:
        entities[s.entity].append(s.name)
    shared = {e: names for e, names in entities.items() if e and len(names) > 1}
    if shared:
        print("\n一个主体下有多家店：")
        for entity, names in shared.items():
            print(f"  {entity}：{'、'.join(names)}")
    if any(not s.entity for s in model.stores):
        print("\n主体未配置的店没法按主体汇总。数据里读不到主体的平台只能手工配。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ledger", description="把交上来的表算成账。"
    )
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="模型目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="算账：认表、挂钩、出损益表和自检结果")
    r.add_argument("paths", nargs="+", help="文件或文件夹")
    r.add_argument("--store", help="只算这家店")
    r.add_argument("--json", action="store_true", help="输出 JSON，给界面和接口用")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("stores", help="看店铺注册表")
    s.set_defaults(func=cmd_stores)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ModelError as exc:
        print(f"模型有问题，先修模型：\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
