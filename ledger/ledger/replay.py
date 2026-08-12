"""回放：拿真实历史数据重算一遍，逐个数字和基线比。

这是引擎能不能被放心改的唯一依据，也是让模型参与改引擎的前提。

已有的 `tools/accept.py` 盯的是「引擎算的和人工 Excel 对不对得上」，护着约十个科目。
它护不住的东西比护住的多：净利率变了它不看，某家店的自检结论从「可结账」翻成
「拦截」它不看，未归类金额、未关联分桶、覆盖率、缺失数据源它一概不看。而这些正是
改动最容易碰坏的地方——改坏了不报错，只是数字悄悄变了。

回放换个判据：不问「对不对」，只问「变没变」。把每家店每个账期的整份对外结构存成
基线，改动之后重算，逐个字段比。变了就摆出来，让人看一眼是不是想要的变化。

    对不对    由 accept.py 管，判据是人工表。
    变没变    由这里管，判据是上一个已知good版本。

两者缺一不可。只有前者，改动会在没人看的角落里改掉数字；只有后者，第一版算错了
会被一路钉死当成正确答案。

基线是一份 JSON，跟着代码进 git。改动引起的数字变化会以 diff 的形式出现在评审里，
这比任何测试报告都直白：一次提交如果同时改了引擎和基线，那份 diff 就是它对账上
数字的全部影响，摆在明面上。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .engine.runtime import ingest, run
from .model.schema import Model
from .version import engine_version
from .view import slice_dict

#: 一分钱以内算没变。和 accept.py 同一个口径。
TOLERANCE = 0.005

#: 基线文件。放在仓库里，跟着代码走。
BASELINE = Path(__file__).resolve().parent.parent / "tests" / "baseline" / "statements.json"

#: 这些字段每次跑都不一样，比它们只会制造噪音。
#:
#: 目前只有一个：`sha` 之类的内容哈希不进 `slice_dict`，所以实际没有需要排除的。
#: 保留这个口子是因为将来一定会加——加了带时间戳的字段之后，忘记排除会让基线
#: 每次都对不上，然后人就开始习惯性地重录基线，门槛当天就废了。
VOLATILE: frozenset[str] = frozenset()

#: 哪些字段存的是钱。其余的数字是行数、笔数、覆盖率，变了同样要报，
#: 但不能和金额加在一起——那个合计是用来回答「这次改动动了多少钱」的。
MONEY_FIELDS = frozenset({"value", "amount", "unlinked_total", "base_total", "total"})


@dataclass(frozen=True, slots=True)
class Change:
    """一处变化。`path` 定位到具体字段，照着它能直接找到是哪个数字。"""

    store: str
    period: str
    path: str
    before: Any
    after: Any
    #: added 新出现、removed 消失了、changed 数值或文本变了。
    kind: str = "changed"

    @property
    def numeric(self) -> bool:
        return isinstance(self.before, (int, float)) and isinstance(self.after, (int, float))

    @property
    def money(self) -> bool:
        """是钱，不是行数也不是比率。

        分开是因为报告要给一个「绝对值合计」。把行数混进去，删掉一条指标就会
        看到「金额变化合计 96,879」，其中 93,174 是那条指标扫过的行数——一个
        本该回答「这次改动动了多少钱」的数字，变成了没有单位的加总。
        """
        return self.numeric and self.path.rsplit(".", 1)[-1] in MONEY_FIELDS

    @property
    def delta(self) -> float:
        return float(self.after) - float(self.before) if self.numeric else 0.0

    def line(self) -> str:
        where = f"{self.store} {self.period} {self.path}"
        if self.kind == "added":
            return f"  新增   {where} = {_short(self.after)}"
        if self.kind == "removed":
            return f"  消失   {where}（原为 {_short(self.before)}）"
        if self.numeric:
            return (
                f"  变化   {where}\n"
                f"         {self.before:,.2f} → {self.after:,.2f}"
                f"（{self.delta:+,.2f}）"
            )
        return f"  变化   {where}\n         {_short(self.before)}\n      →  {_short(self.after)}"


@dataclass
class Replay:
    """一次回放的结论。"""

    #: 当前算出来的整份结果：store_id → period → slice_dict。
    current: dict[str, dict[str, Any]] = field(default_factory=dict)
    baseline_version: str = ""
    version: str = ""
    changes: list[Change] = field(default_factory=list)
    #: 基线里有、这次没算出来的账期。整段消失比某个数字变了严重得多。
    vanished: list[str] = field(default_factory=list)
    #: 这次新算出来的账期。数据补齐时是正常的，代码改动引起的就要问为什么。
    appeared: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.changes and not self.vanished and not self.appeared

    @property
    def money_changes(self) -> list[Change]:
        """金额变了的部分。这是最要紧的一类，单独拎出来。"""
        return [c for c in self.changes if c.money and abs(c.delta) >= TOLERANCE]

    @property
    def count_changes(self) -> list[Change]:
        """行数、笔数、覆盖率。不是钱，但覆盖率掉了往往是金额出问题的前兆。"""
        return [c for c in self.changes if c.numeric and not c.money]

    def report(self) -> str:
        head = f"引擎 {self.version}"
        if self.baseline_version and self.baseline_version != self.version:
            head += f"（基线录于 {self.baseline_version}）"
        if self.clean:
            n = sum(len(p) for p in self.current.values())
            return f"{head}\n回放通过：{len(self.current)} 家店 {n} 个账期，没有一个数字变化。"

        out = [head, ""]
        if self.vanished:
            out.append(f"整个账期算不出来了（{len(self.vanished)} 个）：")
            out += [f"  {k}" for k in self.vanished]
            out.append("")
        if self.appeared:
            out.append(f"新出现的账期（{len(self.appeared)} 个）：")
            out += [f"  {k}" for k in self.appeared]
            out.append("")
        money = self.money_changes
        if money:
            total = math.fsum(abs(c.delta) for c in money)
            out.append(f"金额变化 {len(money)} 处，绝对值合计 {total:,.2f}：")
            out += [c.line() for c in money]
            out.append("")
        counts = self.count_changes
        if counts:
            out.append(f"行数与覆盖变化 {len(counts)} 处：")
            out += [c.line() for c in counts]
            out.append("")
        other = [c for c in self.changes if c not in money and c not in counts]
        if other:
            out.append(f"结论与文本变化 {len(other)} 处：")
            out += [c.line() for c in other]
            out.append("")
        out.append(
            "这些变化如果是这次改动想要的，跑 `python -m ledger.cli replay --record` 重录基线，"
            "把上面的 diff 一起提交；如果不是，改动碰坏了东西。"
        )
        return "\n".join(out)


def snapshot(model: Model, corpus: Path, store_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """拿语料把指定的店整个算一遍，返回 store_id → period → 对外结构。

    走的是产品真正在走的那条路（`ingest` → `run` → `slice_dict`），不是另搭一条
    测试专用通道。基线要能代表产品的行为，就不能算一份别的东西。
    """
    ids = list(store_ids) if store_ids is not None else [s.id for s in model.stores if not s.archived]
    out: dict[str, dict[str, Any]] = {}
    for store_id in ids:
        store = model.store(store_id)
        files = sorted(p for p in corpus.rglob("*.xlsx") if store.owns(p.name))
        if not files:
            continue
        result = run(ingest([str(p) for p in files], model, [store.name]), store.platform)
        periods = {
            sl.period: _strip(slice_dict(sl, store, model))
            for sl in result.slices.values()
        }
        if periods:
            out[store_id] = dict(sorted(periods.items()))
    return dict(sorted(out.items()))


def compare(current: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> Replay:
    """逐字段比。基线里的每个账期都要在当前结果里找到并且一模一样。"""
    base_periods: dict[str, dict[str, Any]] = baseline.get("stores", {})
    rp = Replay(
        current=current,
        baseline_version=baseline.get("engine_version", ""),
        version=engine_version(),
    )

    for store_id, periods in base_periods.items():
        for period, before in periods.items():
            after = current.get(store_id, {}).get(period)
            if after is None:
                rp.vanished.append(f"{store_id} {period}")
                continue
            rp.changes.extend(_walk(store_id, period, "", before, after))

    for store_id, periods in current.items():
        for period in periods:
            if period not in base_periods.get(store_id, {}):
                rp.appeared.append(f"{store_id} {period}")

    rp.changes.sort(key=lambda c: (-abs(c.delta), c.store, c.period, c.path))
    return rp


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(current: dict[str, dict[str, Any]], path: Path = BASELINE, note: str = "") -> None:
    """录基线。带引擎版本，好回答「这份基线是哪一版录的」。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine_version": engine_version(),
        "note": note,
        "stores": current,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _strip(payload: dict[str, Any]) -> Any:
    """去掉每次跑都变的字段。"""
    return {k: v for k, v in payload.items() if k not in VOLATILE}


def _walk(store: str, period: str, path: str, before: Any, after: Any) -> list[Change]:
    """递归比两份结构。字典按键比，列表优先按名字配对、配不上才退回按下标。

    报表行、自检项、分桶都是有名字的列表。按下标比会在增删一项时把后面全部错位：
    删掉「平台物流费」那一行，损益表 14 项里有 13 项报「变了」，实际动的只有一项。
    这种报告没法看，而回放门的全部价值就在于让人一眼看出动了什么。

    顺序仍然要盯。按名字配对之后，如果两边共有项的先后次序不一样，单独报一条
    「顺序」变化——一条，而不是把顺序变化伪装成十几条金额变化。
    """
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[Change] = []
        for key in sorted(set(before) | set(after)):
            sub = f"{path}.{key}" if path else key
            if key not in after:
                out.append(Change(store, period, sub, before[key], None, "removed"))
            elif key not in before:
                out.append(Change(store, period, sub, None, after[key], "added"))
            else:
                out.extend(_walk(store, period, sub, before[key], after[key]))
        return out

    if isinstance(before, list) and isinstance(after, list):
        keyed = _pair_by_name(store, period, path, before, after)
        if keyed is not None:
            return keyed
        out = []
        for i in range(max(len(before), len(after))):
            item = before[i] if i < len(before) else after[i]
            sub = f"{path}[{i}]{_label(item)}"
            if i >= len(after):
                out.append(Change(store, period, sub, before[i], None, "removed"))
            elif i >= len(before):
                out.append(Change(store, period, sub, None, after[i], "added"))
            else:
                out.extend(_walk(store, period, sub, before[i], after[i]))
        return out

    if isinstance(before, bool) or isinstance(after, bool):
        # bool 是 int 的子类，不先挡住的话 True/1 会被判成相等。
        # `can_close` 从 True 变 1 无所谓，从 True 变 False 是天大的事。
        return [] if bool(before) == bool(after) else [Change(store, period, path, before, after)]

    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if math.isnan(before) and math.isnan(after):
            return []
        if abs(float(after) - float(before)) < TOLERANCE:
            return []
        return [Change(store, period, path, before, after)]

    return [] if before == after else [Change(store, period, path, before, after)]


def _pair_by_name(
    store: str, period: str, path: str, before: list[Any], after: list[Any]
) -> list[Change] | None:
    """两个列表按项目名配对着比。配不上返回 None，由调用方退回按下标比。

    要求两边每一项都有名字、而且名字在各自列表里唯一。差一点都不行：名字有重复
    就没法确定谁对谁，硬配会把两个同名项的数字张冠李戴——那比错位更糟，错位起码
    看得出来不对劲。
    """
    keys_before = [_key(x) for x in before]
    keys_after = [_key(x) for x in after]
    for keys in (keys_before, keys_after):
        if not all(keys) or len(set(keys)) != len(keys):
            return None

    by_before = dict(zip(keys_before, before))
    by_after = dict(zip(keys_after, after))
    # 配对认 id，显示认中文名——报告是给人看的，`statement[n_compensation]` 要人
    # 回模型文件里查那是哪一行。名字有重的时候退回用 id，宁可难读也不能有两条
    # 路径长得一模一样。
    shown = {k: _label(v).strip() or k for k, v in (*by_before.items(), *by_after.items())}
    dupes = {n for n in shown.values() if list(shown.values()).count(n) > 1}
    shown = {k: (f"{n} {k}" if n in dupes else n) for k, n in shown.items()}

    out: list[Change] = []
    for key in keys_before:
        sub = f"{path}[{shown[key]}]"
        if key in by_after:
            out.extend(_walk(store, period, sub, by_before[key], by_after[key]))
        else:
            out.append(Change(store, period, sub, by_before[key], None, "removed"))
    for key in keys_after:
        if key not in by_before:
            out.append(Change(store, period, f"{path}[{shown[key]}]", None, by_after[key], "added"))

    # 只看共有项之间的先后。新增和删除已经各自报过了，把它们算进顺序里，
    # 每次增删一项都会附带一条没有信息量的「顺序变了」。
    common_before = [k for k in keys_before if k in by_after]
    common_after = [k for k in keys_after if k in by_before]
    if common_before != common_after:
        out.append(Change(
            store, period, f"{path} 顺序",
            "、".join(shown[k] for k in common_before),
            "、".join(shown[k] for k in common_after),
        ))
    return out


def _key(item: Any) -> str:
    """列表项的配对标识。取 id 优先于名字：改中文显示名不该被当成换了一项。"""
    if not isinstance(item, dict):
        return ""
    for name in ("id", "node_id", "metric", "name", "label"):
        value = item.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _label(item: Any) -> str:
    """列表项的人话标识，拼进路径。

    没有它，报告里出现的是 `statement[19].value` —— 要人去数模型文件的第 19 行
    才知道说的是净利润。
    """
    if not isinstance(item, dict):
        return ""
    for key in ("name", "label", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return f" {value}"
    return ""


def _short(value: Any, width: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 1] + "…"


__all__ = [
    "BASELINE",
    "TOLERANCE",
    "Change",
    "Replay",
    "compare",
    "engine_version",
    "load_baseline",
    "snapshot",
    "write_baseline",
]
