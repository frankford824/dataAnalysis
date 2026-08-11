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
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from .engine.audit import (
    BUCKET_EXCLUDED_FLOW,
    BUCKET_NEEDS_WORK,
    BUCKET_OTHER_PERIOD,
    BUCKET_OTHER_STORES,
)
from . import service
from .engine.runtime import Slice, ingest, run
from .model.config import add_store, update_store
from .model.loader import ModelError, load_model
from .model.schema import Model, Store
from .view import oneline as _oneline
from .view import slice_dict as _as_dict
from .view import source_name as _source_name
from .view import statement_order
from .workspace import default_root

#: 仓库自带的模型。
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"

#: 真实历史数据。不在仓库里（几百兆的平台导出），回放要它。
CORPUS = Path("/home/wsfwk/data/platform")

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
                    guesses[candidate] = model.guess_platform(candidate)
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
    for node in statement_order(model):
        nv = sl.nodes.get(node.id)
        if nv is None or not nv.applicable:
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


#: 每个桶为什么不用管，一句话说清。要人查的那个桶不在这里——它本来就该占注意力。
_BUCKET_WHY = {
    BUCKET_OTHER_STORES: "交上来就是全公司的，绝大多数属于别家店",
    BUCKET_EXCLUDED_FLOW: "理财、调拨、保证金、广告预充值，不是损益",
    BUCKET_OTHER_PERIOD: "订单号是对的，但订单不在本期。跨期结算或者导出时日期选宽了",
}


def _render_unlinked(sl: Slice) -> None:
    buckets = sl.audit.unlinked_buckets
    if not buckets:
        return
    total = sl.audit.unlinked_total
    head = "要查归属的钱" if total else "没有要查归属的钱"
    print(f"\n没进利润的钱：{head} {total:,.2f}")
    for label, count, amount in buckets:
        why = _BUCKET_WHY.get(label, "")
        print(f"  {_pad(label, 26)}{_pad(f'{amount:,.2f}', 15, right=True)}  {count:>7,} 笔"
              + (f"   {why}" if why else ""))
    if total:
        print(f"  只有「{BUCKET_NEEDS_WORK}」要人查，查清了才会进利润。"
              f"其余几类本来就不该算本店这一期。")


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
                payload.append(_as_dict(sl, store, model))
            else:
                render_slice(sl, store, model)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        report_orphans(orphans, model)
        print(f"\n{total_slices} 个店期，{closable} 个可以结账。")
    return 0


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
    blank = [s for s in model.stores if not s.entity]
    if blank:
        print("\n这些店还没配主体，按主体汇总时会漏掉：")
        for s in blank:
            print(f"  {s.name}    配置命令：ledger store set {s.id} --entity 主体全名")
    return 0


def cmd_store_set(args: argparse.Namespace) -> int:
    """改一家店的配置。

    法人主体这类东西数据里读不出来（支付宝和微信账单不带主体信息），只能靠人配。
    要人去改 YAML 才能配，那是脚手架不是产品，所以命令行和界面都得能改。
    """
    changes: dict[str, object] = {}
    for key in ("entity", "entity_tax_id", "note"):
        value = getattr(args, key, None)
        if value is not None:
            changes[key] = value
    if args.alias:
        model = load_model(args.model)
        existing = list(model.store(args.store_id).aliases)
        changes["aliases"] = existing + [a for a in args.alias if a not in existing]
    if args.archive is not None:
        changes["archived"] = args.archive
    if not changes:
        print("没说要改什么。可改：--entity --tax-id --alias --note --archive/--unarchive")
        return 1

    store = update_store(args.model, args.store_id, changes)
    print(f"{store.name} 已更新：")
    print(f"  平台      {store.platform}")
    print(f"  法人主体  {store.entity or '（未配置）'}")
    if store.entity_tax_id:
        print(f"  税号      {store.entity_tax_id}")
    if store.aliases:
        print(f"  别名      {'、'.join(store.aliases)}")
    print(f"  状态      {'已归档' if store.archived else '在营'}")
    return 0


def cmd_store_add(args: argparse.Namespace) -> int:
    """登记一家新店。开新店、接新平台都走这里，不改代码也不手编文件。"""
    model = load_model(args.model)
    platform = args.platform or model.guess_platform(args.name)
    if not platform:
        print(f"猜不出 {args.name} 是哪个平台，用 --platform 指定。"
              f"可选：{'、'.join(model.platform_ids())}")
        return 1
    store = add_store(args.model, Store(
        id=args.id, name=args.name, platform=platform,
        entity=args.entity or "", entity_tax_id=args.tax_id or "",
        aliases=tuple(args.alias or ()), note=args.note or "",
    ))
    print(f"已登记 {store.name}（{store.platform}）")
    if not store.entity:
        print(f"  主体还没配：ledger store set {store.id} --entity 主体全名")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    """把文件交进工作区，留档后自动算账。

    和 `run` 的区别是留不留档。`run` 是一次性核对，算完什么都不留；`submit` 存进
    工作区，往后每次算账都会带上以前交的表——店长这周只补一张运费表，也能出完整损益，
    不用把整个月的表重传一遍。
    """
    from .workspace import Workspace  # 只有留档路径才需要，别拖慢纯核对

    model = load_model(args.model)
    files = collect(args.paths)
    if not files:
        print("没找到可解析的文件。", file=sys.stderr)
        return 1

    ws = Workspace(args.home or default_root())
    print(f"工作区：{ws.root}")
    result = service.intake(ws, model, [(f.name, f) for f in files], by=args.by or "")
    print(result.summary())

    for r in result.rejected:
        print(f"\n没进账：{r.file}")
        print(f"  {r.why}")
        if r.suggest.get("store"):
            hint = f" --platform {r.suggest['platform']}" if r.suggest.get("platform") else ""
            print(f"  登记：ledger store add <id> {r.suggest['store']}{hint}")

    for f in result.failures:
        print(f"\n{f['store']}：{f['why']}")
        for reason in f.get("reasons", []):
            print(f"  {reason}")

    if not result.periods:
        return 1
    print()
    for p in sorted(result.periods, key=lambda d: (d["store"], d["period"])):
        mark = "可结账" if p["can_close"] else "结不了"
        missing = f"，缺 {'、'.join(p['missing_sources'])}" if p["missing_sources"] else ""
        print(f"  {p['store']} · {p['period']}  {mark}{missing}")
    print(f"\n打开界面看明细：ledger web")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """起界面。"""
    import uvicorn

    from . import api

    api.DEFAULT_MODEL = args.model
    api.WORKSPACE_ROOT = args.home or default_root()
    api._ws = None
    print(f"模型：{api.DEFAULT_MODEL}")
    print(f"工作区：{api.WORKSPACE_ROOT}")
    print(f"界面：http://{args.host}:{args.port}")
    uvicorn.run(api.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """回放：拿真实历史数据重算，逐个数字和基线比。

    改引擎之后必须跑这个。返回码是给自动化用的：1 表示有数字变了，
    调用方（提交前的检查、模型自己发起的改动）据此停下来等人看。
    """
    from .replay import compare, load_baseline, snapshot, write_baseline

    corpus = args.corpus
    if not corpus.exists():
        print(
            f"找不到历史数据语料 {corpus}。\n"
            "回放是引擎改动的唯一验收依据，没有语料就没有依据，这里不会假装通过。",
            file=sys.stderr,
        )
        return 2

    model = load_model(args.model)
    current = snapshot(model, corpus, args.store or None)
    if not current:
        print(f"{corpus} 里没有任何一家在营店铺的数据", file=sys.stderr)
        return 2

    if args.record:
        from .replay import BASELINE, engine_version

        version = engine_version()
        write_baseline(current, note=args.note or "")
        n = sum(len(p) for p in current.values())
        print(f"已录基线：{len(current)} 家店 {n} 个账期 → {BASELINE}")
        if version.endswith("-dirty"):
            print(
                "注意：工作区有未提交的改动，这份基线对应的代码不在版本库里。\n"
                "     提交之后再录一次，否则将来没法回答「这份基线是哪一版录的」。"
            )
        print("把它和这次的代码改动一起提交，那份 diff 就是改动对账上数字的全部影响。")
        return 0

    baseline = load_baseline()
    if not baseline:
        print(
            "还没有基线。先在一个已知算得对的版本上跑 "
            "`ledger replay --record` 把当前结果录下来。",
            file=sys.stderr,
        )
        return 2

    rp = compare(current, baseline)
    print(rp.report())
    return 0 if rp.clean else 1


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

    sb = sub.add_parser("submit", help="交表：留档进工作区，然后自动算账")
    sb.add_argument("paths", nargs="+", help="文件或文件夹")
    sb.add_argument("--home", type=Path, help=f"工作区目录，默认 {default_root()}")
    sb.add_argument("--by", help="交表人，记进留痕")
    sb.set_defaults(func=cmd_submit)

    w = sub.add_parser("web", help="起界面")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--home", type=Path, help=f"工作区目录，默认 {default_root()}")
    w.set_defaults(func=cmd_web)

    rp = sub.add_parser(
        "replay", help="回放：拿真实历史数据重算，逐个数字和基线比。改引擎后必跑"
    )
    rp.add_argument("--corpus", type=Path, default=CORPUS, help=f"历史数据目录，默认 {CORPUS}")
    rp.add_argument("--store", action="append", help="只回放这家店，可重复")
    rp.add_argument("--record", action="store_true",
                    help="把当前结果录成新基线。只在确认变化是想要的之后用")
    rp.add_argument("--note", help="录基线时写一句为什么变，进基线文件")
    rp.set_defaults(func=cmd_replay)

    s = sub.add_parser("stores", help="看店铺注册表")
    s.set_defaults(func=cmd_stores)

    st = sub.add_parser("store", help="改店铺配置：主体、税号、别名、归档")
    ssub = st.add_subparsers(dest="store_cmd", required=True)

    se = ssub.add_parser("set", help="改一家店的配置")
    se.add_argument("store_id", help="店铺 id，用 ledger stores 看")
    se.add_argument("--entity", help="法人主体全名")
    se.add_argument("--tax-id", dest="entity_tax_id", help="主体税号")
    se.add_argument("--alias", action="append", help="再认一个文件名里的别名，可重复")
    se.add_argument("--note", help="备注：这个值是从哪来的")
    se.add_argument("--archive", dest="archive", action="store_true", default=None,
                    help="归档：不参与新账期，历史账仍可查")
    se.add_argument("--unarchive", dest="archive", action="store_false",
                    help="取消归档")
    se.set_defaults(func=cmd_store_set)

    sa = ssub.add_parser("add", help="登记一家新店")
    sa.add_argument("id", help="店铺 id，英文，以后不能改（它是关联键）")
    sa.add_argument("name", help="店铺全名，交上来的文件名里带的就是这个")
    sa.add_argument("--platform", help="平台 id，不给就从店名猜。可选值见 platforms.yaml")
    sa.add_argument("--entity", help="法人主体全名")
    sa.add_argument("--tax-id", dest="tax_id", help="主体税号")
    sa.add_argument("--alias", action="append", help="别名，可重复")
    sa.add_argument("--note", help="备注")
    sa.set_defaults(func=cmd_store_add)

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
