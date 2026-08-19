"""模型辅助：让大模型帮着猜列该映到哪个角色，但不让它碰账。

## 为什么值得接

接表提议器是纯规则的：列名归一化之后查模型自己的词汇表，配上值形态打分。在真实
语料上实测过一遍——把每个已落库的模板逐个从模型里摘掉，让它重考自己那张表：

    规则   77 项里对 42 项（55%），另有 29 处乱映
    模型   77 项里对 55 项（71%），另有 14 处乱映，0 次发明清单外的角色

两个方向都更好。最能说明问题的是万相台那张 77 列的推广表：规则把「主体名称」
映成了 `store_name`（会把店铺认错），模型三项全中，而且 71 列平台自带的展现量、
点击率、投产比一列都没碰。

## 为什么接了不危险

模型的输出不是配置，是**草案里的一个建议**。它必须穿过接表向导原有的四道关：

    角色校验    不在这张表所属数据源的角色清单里，直接丢掉（实测 0 次发生，但不能靠它自觉）
    唯一性      一个角色只能映一列，撞了就丢后来的；模型完整性检查还会再拦一次
    试跑        真解析一遍，合计行、掉行率、控制合计、脊柱缺列都要过
    人确认      向导里人逐列拍板，落库前还要再看一次试跑结果

所以这里错了的后果是「人多点几下」，不是「钱算错」。这个性质是设计出来的，不是
碰巧——模型辅助只在**提议**这一步生效，提议之后的每一道关都是确定性的。

## 边界

出站数据经过 `outbound()`，只允许走三样东西：列名、值形态、脱敏后的样值。
买家昵称、手机号、收货地址、订单号这些不出去——它们对判断「这列是不是钱」
毫无帮助，却是最不该离开这台机器的东西。

模型关掉、没配、超时、返回垃圾，全都只是「这次没有模型建议」，规则提议照旧。
一条 `except` 都不许漏出去：接表向导不能因为模型抽风就打不开。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .workspace import default_root

#: 出站请求最长等多久。接表向导是人在等着的界面，超过这个就不如直接给规则结果。
TIMEOUT = 60.0

#: 一次最多带多少列出去。万相台那张表 77 列，留足余量。
MAX_COLUMNS = 200

#: 每列带几个样值。
SAMPLES = 3

#: 样值里的文本最长带多少字。再长的是商品标题、收货地址这类，带出去没收益。
TEXT_CAP = 12


class Disabled(Exception):
    """没配模型。调用方当成「这次没有建议」处理，不是错误。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Config:
    """模型接入配置。

    密钥不进这个文件，只存一个指向密钥文件的路径。仓库里、工作区快照里、
    出错时的报错信息里，都不该出现密钥本身。
    """

    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = TIMEOUT

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


def config_path(root: Path | None = None) -> Path:
    return (root or default_root()) / "llm.json"


def load_config(root: Path | None = None) -> Config:
    """读配置。环境变量优先，然后是工作区里的 llm.json。

    两条路都读不出完整配置就返回空的 `Config`，`ready` 是 False。这不是错误——
    没配模型是完全正常的运行状态，整套东西不靠模型也能用。
    """
    env = os.environ
    base_url = env.get("LEDGER_LLM_BASE_URL", "")
    model = env.get("LEDGER_LLM_MODEL", "")
    key = env.get("LEDGER_LLM_API_KEY", "")
    timeout = _positive(env.get("LEDGER_LLM_TIMEOUT"), TIMEOUT)

    path = config_path(root)
    if path.exists() and not (base_url and model and key):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            if not raw.get("enabled", True):
                return Config()
            base_url = base_url or str(raw.get("base_url") or "")
            model = model or str(raw.get("model") or "")
            timeout = _positive(raw.get("timeout"), timeout)
            if not key:
                key = _read_key(raw)

    return Config(
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=key,
        timeout=timeout,
    )


def _read_key(raw: dict[str, Any]) -> str:
    """从 `api_key_file` 指向的文件读密钥。

    只支持指向文件、不支持直接写在配置里，是故意的：配置文件会被人打开看、
    会被顺手贴到别处，密钥不该待在那种地方。
    """
    where = raw.get("api_key_file")
    if not where:
        return ""
    try:
        return Path(str(where)).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _positive(value: Any, fallback: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return out if out > 0 else fallback


# --------------------------------------------------------------------------- #
# 出站边界
# --------------------------------------------------------------------------- #

#: 看着像身份标识的样值。整串数字、长串字母数字混排，都归这一类。
_ID_LIKE = re.compile(r"^[0-9]{6,}$|^[A-Za-z0-9_-]{12,}$")

#: 手机号。单独挡一次，虽然它也满足上面那条。
_PHONE = re.compile(r"^1[3-9][0-9]{9}$")


def mask(value: Any) -> str:
    """把一个样值变成能出站的形式。

    保留的是「形状」，丢掉的是「内容」：一串 13 位数字对判断列的用途有帮助，
    这串数字具体是多少没有帮助。所以出去的是「13位数字」而不是订单号本身。

    短文本原样带出去，因为枚举值是强信号——「商品/关键词」这两个值能让模型
    确定那列是主体类型。收货地址、商品标题这类长文本换成长度描述。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:g}"

    text = str(value).strip()
    if not text:
        return ""
    if _PHONE.match(text):
        return "手机号"
    if _ID_LIKE.match(text):
        kind = "数字" if text.isdigit() else "编号"
        return f"{len(text)}位{kind}"
    if len(text) > TEXT_CAP:
        return f"{len(text)}字文本"
    return text


def outbound(columns: Sequence[Any], vocabulary: Iterable[Any]) -> dict[str, Any]:
    """组装这次要发出去的东西。这是唯一的出站口，安全边界就在这个函数里。

    只有三样东西出去：

        列名        任务的全部信号来源，不给它就没法做
        值形态      引擎自己算出来的分类，不含原始值
        脱敏样值    过 `mask()`，只剩形状和短枚举

    店名、法人主体、税号、金额合计、订单号一概不出去。判断「这列该映成什么角色」
    根本不需要它们，而它们恰恰是最不该离开这台机器的。
    """
    return {
        "角色清单": [
            {
                "role": f.role,
                "形态": f.kind,
                "别处的列名": list(f.columns)[:6],
                "供给指标": list(f.metrics)[:4],
            }
            for f in vocabulary
        ],
        "表里的列": [
            {
                "序号": c.index,
                "列名": c.column,
                "值形态": c.shape,
                "样值": [s for s in (mask(v) for v in c.samples[:SAMPLES]) if s],
            }
            for c in list(columns)[:MAX_COLUMNS]
        ],
    }


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #

_SYSTEM = """你在帮一个记账系统把电商平台导出表的列映射到字段角色。只输出 JSON。
硬约束，违反任何一条都会让账算错：
1. role 只能取自给定的角色清单。不许发明新角色，拿不准就不要输出这一列。
2. 一个角色最多只能映一列。
3. 平台报表自带的派生指标（展现量、点击率、投产比、转化率、成交金额这类）不要映射，
   它们不是账上的原始数据，映了会重复计算。
4. 带「求和项:」前缀的是 Excel 透视表汇总列，绝对不要映射。
5. 映错比不映坏得多：不映会被覆盖率报出来，映错不报错，只是钱静默算错。"""

_ASK = (
    '\n\n输出 {"mappings": [{"序号": 0, "role": "spend", "why": "为什么"}]}，'
    "只列出你认为该映射的列。why 写给人看，说清判断依据。"
)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """模型对一列的建议。`why` 会显示在向导里给人看。"""

    index: int
    role: str
    why: str = ""


@dataclass
class Advice:
    """一次模型辅助的结果。

    `ok` 是 False 时 `items` 一定是空的，调用方直接用规则提议即可。没配模型、
    超时、返回垃圾，在调用方看来是同一件事，区别只在 `note` 里写给人看的那句话。
    """

    ok: bool = False
    items: list[Suggestion] = field(default_factory=list)
    model: str = ""
    note: str = ""
    #: 被校验挡掉的建议，写清为什么。这些必须留痕：模型开始频繁越界时要能看出来。
    rejected: list[str] = field(default_factory=list)
    request_id: str = ""
    elapsed_ms: int = 0

    def by_index(self) -> dict[int, Suggestion]:
        return {s.index: s for s in self.items}


def suggest_roles(
    columns: Sequence[Any],
    vocabulary: Sequence[Any],
    *,
    config: Config | None = None,
    root: Path | None = None,
) -> Advice:
    """让模型看一遍这些列，建议每列映到哪个角色。

    `columns` 是 `propose.ColumnGuess` 序列，`vocabulary` 是 `propose.RoleFacts`
    序列——都是接表向导已经算出来的东西，这里不重新推导，避免两套逻辑对不上。

    任何异常都不往外抛。接表向导是人在用的界面，模型抽风不该让它打不开。
    """
    cfg = config or load_config(root)
    if not cfg.ready:
        return Advice(note="没有配置模型，这次只有规则提议")
    if not columns or not vocabulary:
        return Advice(note="没有可提议的列或可选的角色")

    allowed = {f.role for f in vocabulary}
    payload = outbound(columns, vocabulary)
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    try:
        content = _post(cfg, payload, request_id)
    except Disabled as exc:
        return Advice(note=str(exc), model=cfg.model, request_id=request_id)
    except Exception as exc:
        return Advice(
            note=f"模型没能给出建议（{_safe(exc, cfg.api_key)}），这次只有规则提议",
            model=cfg.model,
            request_id=request_id,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    advice = _validate(content, allowed, {c.index for c in columns})
    advice.model = cfg.model
    advice.request_id = request_id
    advice.elapsed_ms = round((time.monotonic() - started) * 1000)
    _log(root, advice, payload, content)
    return advice


def _post(cfg: Config, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    """接表向导那一次请求。保留这个名字，调用处读起来才知道发的是列映射。"""
    return _chat(cfg, _SYSTEM, json.dumps(payload, ensure_ascii=False) + _ASK, request_id)


_FEE_SYSTEM = """你在帮一个记账系统给平台流水里的一条费项归类。只输出 JSON。
硬约束，违反任何一条都会让账算错：
1. major 只能取自给定的口径项清单。不许发明新的口径项。
2. 拿不准就不要给 major，返回 {"major":""}。
3. exclude 只有在这笔钱根本不是经营流水（提现、充值、账户互转、保证金缴存）时才为 true。
4. 不要建议 count_without_order。那个开关让钱不经订单直接进损益，必须由人自己勾。
5. 输入里的数字已经脱敏，不要根据编号形态做判断。"""

_FEE_ASK = (
    '\n\n输出 {"major":"software_fee","minor":"中文细项","exclude":false,"why":"判断依据"}。'
    "major 必须是清单里的 id。"
)


def suggest_fee(
    label: str,
    majors: Sequence[dict[str, str]],
    field: str = "subject",
    *,
    config: Config | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """给一条未归类的科目/备注建议口径项。输出不是配置，人确认后才落库。"""
    cfg = config or load_config(root)
    if not cfg.ready:
        return {"ok": False, "note": "没有配置模型，请直接选口径项"}
    text = (label or "").strip()
    if not text:
        return {"ok": False, "note": "没有可归类的科目"}
    allowed = {str(m.get("id") or "") for m in majors if m.get("id")}
    if not allowed:
        return {"ok": False, "note": "模型里还没有任何口径项"}

    safe = re.sub(r"\d{8,}", "编号", text)[:80]
    payload = {
        "看的列": field,
        "费项": safe,
        "可选口径项": [{"id": m.get("id"), "名称": m.get("name")} for m in majors],
    }
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    try:
        content = _chat(cfg, _FEE_SYSTEM, json.dumps(payload, ensure_ascii=False) + _FEE_ASK, request_id)
    except Exception as exc:
        return {
            "ok": False,
            "note": f"模型没能给出建议（{_safe(exc, cfg.api_key)}）",
            "request_id": request_id,
        }

    major = str(content.get("major") or "").strip()
    if major and major not in allowed:
        return {
            "ok": False,
            "note": f"模型给的口径项 {major} 不在清单里，已丢弃",
            "request_id": request_id,
        }
    advice = {
        "ok": True,
        "major": major,
        "minor": str(content.get("minor") or "").strip()[:40],
        "exclude": bool(content.get("exclude")),
        "why": str(content.get("why") or "").strip()[:200],
        "note": "模型建议，确认后才会进账",
        "request_id": request_id,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    _log(
        root,
        Advice(ok=True, model=cfg.model, request_id=request_id, elapsed_ms=advice["elapsed_ms"]),
        payload,
        content,
    )
    return advice


def _chat(cfg: Config, system: str, user: str, request_id: str) -> dict[str, Any]:
    """发一次请求。OpenAI 兼容接口，强制 JSON，温度 0。

    温度固定 0 不给配：同一条费项两次提议给出不同结果，人就没法判断该不该信它。
    """
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        cfg.base_url + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        },
    )
    with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
        wire = json.loads(response.read().decode("utf-8"))
    choices = wire.get("choices") or []
    if not choices:
        raise ValueError("返回里没有 choices")
    text = (choices[0].get("message") or {}).get("content") or ""
    out = json.loads(text)
    if not isinstance(out, dict):
        raise ValueError("返回的不是 JSON 对象")
    return out


def _validate(content: dict[str, Any], allowed: set[str], indexes: set[int]) -> Advice:
    """校验模型的输出。挡掉的每一条都记下来。

    这里是模型和确定性世界的接缝，也是唯一一处「模型说了什么」变成「系统要考虑
    什么」的地方。所以宁可严：认不出的一律丢，丢了最坏结果是人自己去点，
    放过去最坏结果是钱算错。
    """
    advice = Advice(ok=True)
    seen_roles: dict[str, int] = {}
    seen_index: set[int] = set()

    raw = content.get("mappings")
    if not isinstance(raw, list):
        return Advice(note="模型返回的结构不对，这次只有规则提议")

    for item in raw:
        if not isinstance(item, dict):
            advice.rejected.append(f"不是对象的条目：{_brief(item)}")
            continue
        index = item.get("序号", item.get("index"))
        role = str(item.get("role") or "").strip()
        why = str(item.get("why") or "").strip()

        if not isinstance(index, int) or isinstance(index, bool):
            advice.rejected.append(f"序号不是整数：{_brief(index)}")
            continue
        if index not in indexes:
            advice.rejected.append(f"序号 {index} 不在这张表里")
            continue
        if not role:
            continue
        if role not in allowed:
            advice.rejected.append(f"第 {index} 列的 {role} 不在角色清单里")
            continue
        if index in seen_index:
            advice.rejected.append(f"第 {index} 列给了不止一个角色")
            continue
        if role in seen_roles:
            advice.rejected.append(
                f"{role} 被同时映到第 {seen_roles[role]} 列和第 {index} 列，只取前者"
            )
            continue
        seen_index.add(index)
        seen_roles[role] = index
        advice.items.append(Suggestion(index=index, role=role, why=why))

    advice.note = (
        f"模型提了 {len(advice.items)} 列"
        + (f"，另有 {len(advice.rejected)} 条被校验挡掉" if advice.rejected else "")
    )
    return advice


def _log(root: Path | None, advice: Advice, sent: dict[str, Any], got: dict[str, Any]) -> None:
    """记一条调用日志。出站内容原样记——它已经脱敏过了，记的就是真的发出去的东西。

    这份日志的用途不是排障，是回答「模型都被问了什么、答了什么」。哪天要判断
    该不该更信任它，凭据在这里。写失败不影响主流程：日志坏了不该让接表打不开。
    """
    try:
        path = (root or default_root()) / "llm_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "request_id": advice.request_id,
                "purpose": "onboard_column_mapping",
                "model": advice.model,
                "elapsed_ms": advice.elapsed_ms,
                "sent": sent,
                "got": got,
                "accepted": [{"index": s.index, "role": s.role} for s in advice.items],
                "rejected": advice.rejected,
            },
            ensure_ascii=False,
            default=str,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _safe(exc: Exception, api_key: str) -> str:
    """报错信息里不许出现密钥。

    urllib 的报错会把整个 URL 带出来，而有些网关把 token 放在查询串里。
    出错信息会进日志、进界面、被人贴到聊天里。
    """
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        text = f"HTTP {exc.code}"
    elif isinstance(exc, urllib.error.URLError):
        text = f"连不上模型服务（{exc.reason}）"
    if api_key:
        text = text.replace(api_key, "<已隐去>")
    return text[:200]


def _brief(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 40 else text[:39] + "…"


__all__ = [
    "Advice",
    "Config",
    "Disabled",
    "Suggestion",
    "config_path",
    "load_config",
    "mask",
    "outbound",
    "suggest_fee",
    "suggest_roles",
]
