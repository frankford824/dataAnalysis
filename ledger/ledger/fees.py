"""引擎到底认识哪些费项，以及和外部对照表的差集。

为什么需要这个模块
----------------
「引擎认识哪些费项」这个问题，答案不在一个地方。它散在两处：

    科目字典（dictionary.csv）   平台原始科目名 → 口径项，精确匹配
    模板归类规则链（templates）   备注/业务类型里含某个词 → 口径项，或者显式排除

两处缺一不可。实测淘宝对账表里量最大的那一项——「消费者体验提升计划服财务费」
19,584 行——字典里根本没有，全靠规则链的一条 contains 接住。只看字典会得出
「这一项没人管」的错误结论；只看规则链又会漏掉字典里那一百多条。

所以拿引擎和业务的费项对照表比对时，必须先把这两处合成一份，否则比出来的差集
既有假阳也有假阴，照着它改反而会把对的改错。

这个模块只回答「知道什么」，不回答「用到了什么」——某一条本期有没有命中、
命中多少钱，那要跑完账才知道，属于归类报告的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model.schema import Model, normalize_header


@dataclass(frozen=True)
class FeeItem:
    """引擎认识的一个费项。"""

    #: 平台原始科目名或关键词。
    key: str
    #: 归到哪个口径项。空串表示这一条是「显式排除，不进账」。
    major: str
    platform: str
    #: 从哪儿知道的：dictionary、fee-rules，或模板 id。
    origin: str
    #: 匹配方式：exact（字典精确匹配）、contains、equals、regex。
    how: str
    #: 规则链匹配的是哪个字段角色。字典查的是科目名本身。
    field: str = "subject"
    #: 排除项：匹配上就不进账，不是归到某个口径项。
    excluded: bool = False
    #: 给人看的来源名。模板用模板中文名，字典用「科目字典」。
    origin_name: str = ""

    @property
    def norm(self) -> str:
        return normalize_header(self.key)


@dataclass
class FeeDiff:
    """引擎和一份外部对照表的差集。"""

    #: 引擎认识、对照表没有。这些是对照表该补的。
    only_engine: list[FeeItem] = field(default_factory=list)
    #: 对照表有、引擎不认。这些进来会落未分类。
    only_table: list[tuple[str, str]] = field(default_factory=list)
    #: 两边都有。
    both: list[FeeItem] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.only_engine and not self.only_table


def known_fees(model: Model) -> list[FeeItem]:
    """引擎认识的全部费项，字典和规则链合在一起。

    同一个 key 在两处都出现是正常的：字典给精确匹配兜底，规则链按备注里的词
    命中——同一笔钱两条路都能到达同一个口径项。这里两条都留着，因为它们的
    维护方式不同：字典是数据，改它不用动模板；规则链写在模板里，改它要发版。
    区分开才知道补一条新费项该往哪儿加。
    """
    out: list[FeeItem] = []

    for e in model.dictionary:
        out.append(FeeItem(
            key=e.raw, major=e.major, platform=e.platform,
            origin="dictionary", how="exact", field="subject",
            excluded=not e.major, origin_name="科目字典",
        ))

    plat_of = {t.id: _template_platform(model, t.id) for t in model.templates}
    for t in model.templates:
        for rule in t.classify_rules:
            if rule.dictionary or rule.when is None:
                continue
            m = rule.when
            common = {
                "major": rule.major or "", "platform": plat_of[t.id],
                "origin": t.id, "origin_name": t.name,
                "field": m.field, "excluded": rule.exclude,
            }
            for how, values in (("contains", m.contains), ("equals", m.equals)):
                for v in values or ():
                    out.append(FeeItem(key=v, how=how, **common))
            for how, pattern in (("regex", m.matches), ("regex", m.extract)):
                if pattern:
                    out.append(FeeItem(key=pattern, how=how, **common))

    for r in model.fee_rules:
        out.append(FeeItem(
            key=r.value, major=r.major, platform=r.platform,
            origin="fee-rules", how=r.how, field=r.field,
            excluded=r.exclude, origin_name="费项规则",
        ))
    return out


def _template_platform(model: Model, template_id: str) -> str:
    """模板本身不带平台，靠 id 前缀推。

    取最长匹配，因为平台 id 自己就有前缀关系：`jd` 是 `jd_1688` 的前缀，按声明
    顺序撞上哪个算哪个的话，`jd_1688_xxx` 这张模板会被算成京东的。

    推不出来返回 `*`：模板和平台不是一对一，一张通用表可能几个平台共用。
    这一列只用于展示和分组，推错不影响任何计算。
    """
    hits = [p.id for p in model.platforms if template_id.startswith(p.id)]
    return max(hits, key=len) if hits else "*"


def diff_table(model: Model, table: dict[str, str], platforms: set[str] | None = None) -> FeeDiff:
    """和一份外部对照表比。

    `table` 是 业务描述 → 业务大类。大类不参与比对——两边的口径项一个是中文一个是
    英文 id，硬要对齐得先有一份人工映射，而那份映射本身就是要人来定的东西。
    这里只比「这个费项两边都知道吗」，那是能自动判定的部分。
    """
    fees = [f for f in known_fees(model)
            if platforms is None or f.platform in platforms or f.platform == "*"]
    by_norm: dict[str, FeeItem] = {}
    for f in fees:
        by_norm.setdefault(f.norm, f)

    table_norm = {normalize_header(k): (k, v) for k, v in table.items() if k}

    diff = FeeDiff()
    for key, f in sorted(by_norm.items()):
        (diff.both if key in table_norm else diff.only_engine).append(f)
    for key, (raw, major) in sorted(table_norm.items()):
        if key not in by_norm:
            diff.only_table.append((raw, major))
    return diff


#: 界面上能配的匹配列。角色名是引擎的，中文是人对账表上看到的列名。
FEE_FIELDS = (
    ("subject", "业务描述"),
    ("remark", "备注"),
    ("biz_type", "业务类型"),
)

FEE_HOWS = (
    ("exact", "去空格后相同"),
    ("equals", "完全一致"),
    ("contains", "包含"),
    ("regex", "按格式"),
)

FEE_STAGES = (
    ("after", "仅处理尚未归类的流水"),
    ("before", "优先于现有规则"),
)

#: 口径项没有对应指标时的中文名。和 asset-import.yaml 的 major_labels 对齐。
#: 有指标的口径项优先用损益表上的指标名，避免下拉里出现 software_fee 这种内部代号。
MAJOR_LABELS = {
    "trade_receipt": "交易收款",
    "trade_refund": "交易退款",
    "trade_compensation": "交易赔付",
    "software_fee": "平台服务费",
    "marketing_fee": "平台营销费用",
    "dropship_cost": "代购代发",
    "logistics_fee": "物流运费",
    "withdrawal": "提现",
    "deposit": "保证金",
    "ad_topup": "广告充值",
    "misc_payment": "往来款",
    "trade_receipt_1688": "销售收入（1688）",
    "trade_expense_1688": "销售支出（1688）",
}

#: 科目字典里出现过、但不在 platforms.yaml 里的平台代号。
#: `jd_1688` 是导入工具带的作用域后缀，查表匹配不到在营店铺，
#: 但仍会出现在「已有归类」里，不能把英文 id 直接摊给人看。
EXTRA_PLATFORM_NAMES = {
    "jd_1688": "京东（1688）",
}

_EMPTY_SUBJECT = "（业务描述为空）"
_FIELD_CN = dict(FEE_FIELDS)

#: 支付宝备注里用来区分每一笔的编号。展示和归类时都该拿掉，不然几百行
#: 看起来都一样，点进去却配出几百条只命中一笔的规则。
_ID_NOISE = re.compile(
    r"(?:tradeid|memberid|batchno|alipayid|outtradeno)\s*:\s*\S+"
    r"|\bfee\s*:\s*[\d.]+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b\d{16,}\b",
    re.I,
)
_FALLBACK_FIELD = re.compile(r"\b(subject|remark|biz_type)=")
# 支付宝备注形如「扣款用途: 淘宝联盟佣金代扣 tradeid:5123…」。用途后面紧跟
# 英文键值，不能把 tradeid 算进匹配词，否则每笔流水都会拆成单独一条未归类。
_PURPOSE = re.compile(
    r"扣款用途[:：]\s*(.+?)"
    r"(?=\s+(?:tradeid|memberid|batchno|alipayid|outtradeno|fee)\s*:|[，,;；()（）]|$)",
    re.I,
)


def _fallback_fields(label: str) -> dict[str, str]:
    """把 `biz_type=转账 remark=代扣款 (扣款用途: …)` 解成完整字段，不按空格切开。"""
    if not (label or "").startswith(_EMPTY_SUBJECT):
        return {}
    rest = label[len(_EMPTY_SUBJECT):].strip()
    found = list(_FALLBACK_FIELD.finditer(rest))
    out: dict[str, str] = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(rest)
        out[m.group(1)] = rest[m.end():end].strip()
    return out


def _remark_needle(remark: str) -> str:
    """从长备注里抽出能配「包含」规则的那一小段。"""
    text = (remark or "").strip()
    if not text:
        return ""
    if hit := _PURPOSE.search(text):
        needle = _ID_NOISE.sub(" ", hit.group(1)).strip()
        needle = re.sub(r"\s+", " ", needle).strip(" ,;，；")
        if needle:
            return needle
    cleaned = _ID_NOISE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;，；")
    return cleaned[:80] if cleaned else text[:80]


def major_label(model: Model, major_id: str) -> str:
    """口径项给人看的名字。引擎内部仍用英文 id。"""
    if not major_id:
        return ""
    if major_id in ("trade_receipt_1688", "trade_expense_1688"):
        return MAJOR_LABELS[major_id]
    for m in model.metrics:
        if m.major == major_id:
            return m.name
    return MAJOR_LABELS.get(major_id, major_id)


def major_options(model: Model) -> list[dict[str, str]]:
    """口径项下拉。id 是引擎内部代号，name 是损益表上的中文。"""
    ids: set[str] = set()
    for m in model.metrics:
        if m.major:
            ids.add(m.major)
    for e in model.dictionary:
        if e.major:
            ids.add(e.major)
    for t in model.templates:
        for r in (*t.classify_rules, *t.reclassify):
            if r.major:
                ids.add(r.major)
    for r in model.fee_rules:
        if r.major:
            ids.add(r.major)
    return [
        {"id": k, "name": major_label(model, k)}
        for k in sorted(ids, key=lambda i: major_label(model, i))
    ]


def platform_aliases(model: Model) -> dict[str, str]:
    """platforms.yaml 以外、界面仍可能见到的平台 id → 中文名。"""
    known = {p.id for p in model.platforms}
    extra = dict(EXTRA_PLATFORM_NAMES)
    for e in model.dictionary:
        if e.platform not in known and e.platform != "*" and e.platform not in extra:
            extra[e.platform] = extra.get(e.platform, e.platform)
    return extra


def pretty_unmatched_label(label: str) -> str:
    """把引擎拼的未归类标签翻成对账的人能读的句子。

    引擎内部仍用 `biz_type=其它` 这种键值，方便猜该配哪一列。
    已经算过的账期快照也是这个格式，所以只在展示时翻译，不改标签本身。
    """
    text = (label or "").strip()
    fields = _fallback_fields(text)
    if not fields:
        if text.startswith(_EMPTY_SUBJECT):
            rest = text[len(_EMPTY_SUBJECT):].strip()
            return "业务描述为空" if not rest else f"业务描述为空 · {rest}"
        return text
    parts = []
    if biz := fields.get("biz_type"):
        parts.append(f"业务类型：{biz}")
    if remark := fields.get("remark"):
        shown = re.sub(r"\s+", " ", _ID_NOISE.sub(" ", remark)).strip(" ,;，；")
        if shown:
            parts.append(f"备注：{shown}")
    return "业务描述为空 · " + "；".join(parts) if parts else "业务描述为空"


def humanize_via(text: str, model: Model | None = None) -> str:
    """下钻里「这一行是怎么归上的」翻成中文。

    旧快照里还留着 `[配置] subject 等于 … → software_fee` 这种句子，
    不能等重算才变得可读。
    """
    if not text:
        return text
    shown = text.replace("[配置]", "费项规则")
    for eng, cn in FEE_FIELDS:
        shown = shown.replace(f"{eng} ", f"{cn} ")
        if shown == eng or shown.startswith(eng + " "):
            shown = cn + shown[len(eng):]
    if model is not None:
        names = {row["id"]: row["name"] for row in major_options(model)}
        for mid, name in sorted(names.items(), key=lambda kv: -len(kv[0])):
            shown = shown.replace(mid, name)
    return shown


def rule_dict(r) -> dict:
    """界面上一条可编辑规则的对外结构。"""
    return {
        "platform": r.platform,
        "field": r.field,
        "how": r.how,
        "value": r.value,
        "major": r.major,
        "minor": r.minor,
        "exclude": r.exclude,
        "count_without_order": r.count_without_order,
        "stage": r.stage,
        "note": r.note,
        "by": r.by,
        "at": r.at,
        "label": r.label,
    }


def unmatched_from(ws) -> list[dict]:
    """所有店、所有账期快照里还没认出来的科目，按可配规则合并。

    不重跑引擎：未归类清单已经在每次算账时写进快照。这里只是把它们摊平给人看。

    合并键不是原始标签：支付宝备注里每笔都带着 tradeid，按原文会拆成几百行
    看起来一样的条目。按「该配哪一列、配什么词」合并，一条规则就能盖住同一类。
    """
    buckets: dict[str, dict] = {}
    for st in ws.overview():
        payload = st.result or {}
        plat = payload.get("platform") or ""
        for item in payload.get("unclassified") or []:
            label = (item.get("label") or "").strip()
            if not label:
                continue
            hint = _unmatched_hint(label)
            key = f"{hint['field']}\0{hint['how']}\0{hint['value']}"
            hit = buckets.setdefault(key, {
                "label": label,
                "caption": _unmatched_caption(label, hint, 1),
                "count": 0,
                "amount": 0.0,
                "stores": [],
                "platforms": [],
                "variants": 0,
                **hint,
            })
            hit["count"] += int(item.get("count") or 0)
            hit["amount"] += float(item.get("amount") or 0.0)
            hit["variants"] += 1
            hit["caption"] = _unmatched_caption(label, hint, hit["variants"])
            if st.store_id not in hit["stores"]:
                hit["stores"].append(st.store_id)
            if plat and plat not in hit["platforms"]:
                hit["platforms"].append(plat)
    return sorted(buckets.values(), key=lambda d: -abs(d["amount"]))


def _unmatched_caption(label: str, hint: dict[str, str], variants: int) -> str:
    extra = f" · {variants} 种相近写法" if variants > 1 else ""
    if hint.get("field") == "remark" and hint.get("value"):
        return f"备注含「{hint['value']}」{extra}"
    if hint.get("field") == "biz_type" and hint.get("value"):
        return f"业务类型：{hint['value']}{extra}"
    return pretty_unmatched_label(label) + extra


def _unmatched_hint(label: str) -> dict[str, str]:
    """从归类报告的标签里猜这条该配在哪一列上。

    业务描述为空时，引擎用备注和业务类型拼标签（见 classify._fallback_label）。
    原样拿去当业务描述精确匹配，永远命中不了——那正是它落未归类的原因。
    """
    fields = _fallback_fields(label)
    if fields:
        if remark := fields.get("remark"):
            needle = _remark_needle(remark)
            if needle:
                return {"field": "remark", "value": needle, "how": "contains"}
        if biz := fields.get("biz_type"):
            return {"field": "biz_type", "value": biz, "how": "equals"}
        return {"field": "remark", "value": next(iter(fields.values()), ""), "how": "contains"}
    if label.startswith(_EMPTY_SUBJECT):
        rest = label[len(_EMPTY_SUBJECT):].strip()
        return {"field": "remark", "value": rest, "how": "contains"}
    return {"field": "subject", "value": label, "how": "exact"}


def payload_diff(before: dict | None, after: dict) -> list[dict]:
    """两份账期快照的损益差。金额没变的行不出现。"""
    old = {n["id"]: n.get("value") for n in (before or {}).get("statement") or []}
    names = {n["id"]: n.get("name") or n["id"] for n in after.get("statement") or []}
    out = []
    for n in after.get("statement") or []:
        nid = n["id"]
        a = n.get("value")
        b = old.get(nid)
        if a is None and b is None:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 0.005:
            continue
        if a == b:
            continue
        out.append({
            "id": nid, "name": names.get(nid, nid),
            "before": b, "after": a,
            "delta": (a or 0.0) - (b or 0.0) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None,
        })
    return out
