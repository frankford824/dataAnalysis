from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import PureWindowsPath

SCOPE_START = date(2026, 2, 1)

_PATH_SPLIT_RE = re.compile(r"[\\/]+")
_NAME_KEY_RE = re.compile(r"[\s\-_.·•（）()\[\]【】]+")
_PERIOD_RE = re.compile(r"^(?:(20\d{2})[-/]?(0[1-9]|1[0-2])|(\d{2})(0[1-9]|1[0-2]))$")
_FILENAME_PERIOD_RE = re.compile(
    r"(?<!\d)(?:(20\d{2})[-_.年]?(0[1-9]|1[0-2])|"
    r"(2\d)[-_.]?(0[1-9]|1[0-2]))"
)
_FILE_NAME_RE = re.compile(r"\.[a-z0-9]{2,8}$", re.IGNORECASE)

_GENERIC_NAMES = {
    "bi",
    "data",
    "desktop",
    "documents",
    "download",
    "downloads",
    "excel",
    "finance",
    "pbix",
    "report",
    "reports",
    "source",
    "店铺",
    "店铺数据",
    "店铺资料",
    "修改后数据",
    "原始数据",
    "历史数据",
    "备份",
    "归档",
    "报表",
    "报表数据",
    "数据",
    "数据源",
    "汇总",
    "模板",
    "模版",
    "测试",
    "财务",
}

_ECOMMERCE_PURPOSES = {
    "advertising",
    "advertising_statement",
    "alipay",
    "alipay_control",
    "alipay_ledger",
    "baobei_order",
    "cost",
    "freight",
    "freight_statement",
    "historical_output",
    "order",
    "orders",
    "order_detail",
    "platform_fee",
    "platform_ledger",
    "product_cost",
    "refund",
    "settlement",
    "taobao_platform_fee",
    "wechat",
    "wechat_control",
    "wechat_ledger",
}

_PLATFORM_ALIASES = {
    "1688": "1688",
    "amazon": "amazon",
    "dewu": "dewu",
    "douyin": "douyin",
    "jd": "jd",
    "jingdong": "jd",
    "kuaishou": "kuaishou",
    "pdd": "pinduoduo",
    "pinduoduo": "pinduoduo",
    "redbook": "xiaohongshu",
    "taobao": "taobao",
    "tmall": "tmall",
    "vip": "vip",
    "wechat": "wechat",
    "xiaohongshu": "xiaohongshu",
    "亚马逊": "amazon",
    "京东": "jd",
    "唯品会": "vip",
    "天猫": "tmall",
    "小红书": "xiaohongshu",
    "得物": "dewu",
    "微信": "wechat",
    "快手": "kuaishou",
    "拼多多": "pinduoduo",
    "抖音": "douyin",
    "淘宝": "taobao",
}

_SOURCE_PLATFORM_HINTS = {
    "taobao_platform_fee": "taobao",
}
_STORE_PLATFORM_PREFIXES = (
    ("pdd", "pinduoduo"),
    ("拼多多", "pinduoduo"),
    ("抖店", "douyin"),
    ("抖音", "douyin"),
    ("京东", "jd"),
    ("淘宝", "taobao"),
    ("天猫", "tmall"),
)
_STORE_ROOT_MARKERS = {
    "支付宝收支",
    "聚水潭成本",
    "商品成本",
    "成本明细",
}
_GOVERNANCE_PURPOSES = {
    "employee_master",
    "historical_workspace",
    "performance_reference",
    "responsibility_corpus",
    "rule_corpus",
}

_PLATFORM_FIELDS = (
    "platform",
    "platform_code",
    "platform_name",
    "detected_platform",
    "ecommerce_platform",
    "channel",
)
_STORE_FIELDS = (
    "logical_store",
    "logical_store_name",
    "store_name",
    "detected_store",
    "candidate_store",
    "shop",
    "shop_name",
)
_PERIOD_FIELDS = (
    "content_periods",
    "identified_periods",
    "detected_periods",
    "coverage_periods",
    "monthly_periods",
    "periods",
)
_DATE_RANGE_FIELDS = (
    ("content_start", "content_end"),
    ("content_date_from", "content_date_to"),
    ("identified_start", "identified_end"),
    ("detected_start", "detected_end"),
    ("coverage_start", "coverage_end"),
    ("period_start", "period_end"),
    ("date_from", "date_to"),
)
_SINGLE_DATE_FIELDS = (
    "content_date",
    "identified_date",
    "detected_date",
    "business_date",
    "transaction_date",
    "accounting_date",
)


class TargetStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    PARTIAL = "partial"


class PeriodState(StrEnum):
    CLOSED = "closed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class MonthlyTarget:
    target_key: str
    platform: str
    logical_store: str
    logical_store_key: str
    period: str
    status: TargetStatus
    period_state: PeriodState
    source_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    aliases: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["period_state"] = self.period_state.value
        payload["source_ids"] = list(self.source_ids)
        payload["evidence"] = list(self.evidence)
        payload["aliases"] = list(self.aliases)
        return payload


@dataclass(frozen=True, slots=True)
class ReviewRequired:
    candidate: str
    source_id: str
    platform: str | None
    reason: str
    explanation: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TargetPlan:
    scope_start: date
    scope_end: date
    targets: tuple[MonthlyTarget, ...]
    review_required: tuple[ReviewRequired, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope_start": self.scope_start.isoformat(),
            "scope_end": self.scope_end.isoformat(),
            "targets": [target.to_dict() for target in self.targets],
            "review_required": [item.to_dict() for item in self.review_required],
        }


@dataclass(slots=True)
class _StoreGroup:
    platform: str
    logical_store: str
    logical_store_key: str
    aliases: set[str]
    source_ids_by_period: dict[str, set[str]]
    evidence_by_period: dict[str, set[str]]


def _record_items(
    records: Iterable[Mapping[str, object] | object] | Mapping[str, object],
) -> list[Mapping[str, object] | object]:
    if isinstance(records, Mapping):
        nested = records.get("records")
        snapshots = records.get("snapshots")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            values = list(nested)
            if isinstance(snapshots, Sequence) and not isinstance(
                snapshots, (str, bytes)
            ):
                values.extend(snapshots)
            return values
        return [records]
    return list(records)


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _lookup(record: Mapping[str, object] | object, names: Sequence[str]) -> object | None:
    containers: list[Mapping[str, object]] = []
    if isinstance(record, Mapping):
        containers.append(record)
        for container_name in ("metadata", "attributes", "coverage", "profile", "route"):
            nested = _mapping(record.get(container_name))
            if nested:
                containers.append(nested)
        attributes_json = _mapping(record.get("attributes_json"))
        if attributes_json:
            containers.append(attributes_json)
    else:
        for name in names:
            value = getattr(record, name, None)
            if value not in (None, ""):
                return value
        for container_name in ("metadata", "attributes", "coverage", "profile", "route"):
            nested = _mapping(getattr(record, container_name, None))
            if nested:
                containers.append(nested)

    for container in containers:
        for name in names:
            value = container.get(name)
            if value not in (None, ""):
                return value
    return None


def _text(value: object | None) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _name_key(value: str) -> str:
    return _NAME_KEY_RE.sub("", unicodedata.normalize("NFKC", value).casefold())


def _platform(value: object | None) -> str | None:
    text = _text(value).casefold()
    if not text:
        return None
    return _PLATFORM_ALIASES.get(text, text)


def _platform_from_record(record: Mapping[str, object] | object) -> str | None:
    explicit = _platform(_lookup(record, _PLATFORM_FIELDS))
    if explicit:
        return explicit
    source_kind = _text(_lookup(record, ("source_kind", "dataset_kind", "source_type")))
    hinted = _SOURCE_PLATFORM_HINTS.get(source_kind.casefold())
    if hinted:
        return hinted
    template_id = _text(
        _lookup(record, ("template_id", "matched_template"))
    ).casefold()
    for alias, canonical in _PLATFORM_ALIASES.items():
        if re.search(rf"(?:^|[_\-.]){re.escape(alias)}(?:[_\-.]|$)", template_id):
            return canonical
    path = _text(_lookup(record, ("path", "source_uri", "original_path")))
    for part in _PATH_SPLIT_RE.split(path):
        path_platform = _PLATFORM_ALIASES.get(_text(part).casefold())
        if path_platform:
            return path_platform
    candidate = _candidate_from_path(record)
    candidate_key = _text(candidate).casefold()
    for prefix, canonical in _STORE_PLATFORM_PREFIXES:
        if candidate_key.startswith(prefix):
            return canonical
    if candidate_key.endswith("1688"):
        return "1688"
    return None


def _is_pbix(record: Mapping[str, object] | object) -> bool:
    purpose = _text(_lookup(record, ("purpose", "record_purpose"))).casefold()
    extension = _text(_lookup(record, ("extension", "suffix"))).casefold()
    path = _text(_lookup(record, ("path", "source_uri", "original_name")))
    return purpose == "pbix_asset" or extension == ".pbix" or path.casefold().endswith(
        ".pbix"
    )


def _candidate_from_path(record: Mapping[str, object] | object) -> str | None:
    path = _text(_lookup(record, ("path", "source_uri", "original_path")))
    if not path:
        return None
    parts = [part.strip() for part in _PATH_SPLIT_RE.split(path) if part.strip()]
    for marker in ("店铺", "店铺数据", "店铺资料"):
        for index, part in enumerate(parts[:-1]):
            if part.casefold() == marker.casefold():
                return parts[index + 1]
    for index, part in enumerate(parts[:-1]):
        if _platform(part) in set(_PLATFORM_ALIASES.values()):
            return parts[index + 1]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() not in {
            marker.casefold() for marker in _STORE_ROOT_MARKERS
        }:
            continue
        for candidate in parts[index + 1 : -1]:
            normalized = _text(candidate)
            if re.fullmatch(r"(?:19|20)\d{2}", normalized):
                continue
            if _invalid_candidate_reason(normalized) is None:
                return normalized
    return None


def _store_candidate(record: Mapping[str, object] | object) -> str | None:
    explicit = _text(_lookup(record, _STORE_FIELDS))
    return explicit or _candidate_from_path(record)


def _invalid_candidate_reason(candidate: str | None) -> tuple[str, str] | None:
    if not candidate:
        return "store_not_identified", "没有足够信息确定逻辑店铺"
    normalized = _text(candidate).strip(". ")
    folded = normalized.casefold()
    if folded in {name.casefold() for name in _GENERIC_NAMES}:
        return "generic_directory_name", "候选名称是通用目录或报表名称，不是店铺"
    if _FILE_NAME_RE.search(folded):
        return "filename_not_store", "候选名称看起来是文件名，不能直接作为店铺"
    if re.fullmatch(r"(?:19|20)?\d{2,8}", normalized):
        return "date_or_number_name", "候选名称只是日期或编号，不能确认店铺"
    if re.fullmatch(
        r"(?:0?[1-9]|1[0-2]|[一二三四五六七八九十]{1,3})月份?",
        normalized,
    ):
        return "date_or_number_name", "候选名称只是月份，不能确认店铺"
    return None


def _has_ecommerce_evidence(record: Mapping[str, object] | object) -> bool:
    explicit = _lookup(
        record,
        ("ecommerce_evidence", "is_ecommerce", "commerce_evidence"),
    )
    if explicit is True:
        return True
    if isinstance(explicit, str) and explicit.casefold() in {"true", "yes", "1"}:
        return True
    values = {
        _text(_lookup(record, ("purpose", "record_purpose"))).casefold(),
        _text(_lookup(record, ("source_kind", "dataset_kind", "source_type"))).casefold(),
    }
    if values & _ECOMMERCE_PURPOSES:
        return True
    template_id = _text(_lookup(record, ("template_id", "matched_template"))).casefold()
    return any(token in template_id for token in _ECOMMERCE_PURPOSES)


def _parse_period(value: object) -> str | None:
    text = _text(value)
    match = _PERIOD_RE.fullmatch(text)
    if not match:
        return None
    year = match.group(1) or f"20{match.group(3)}"
    return f"{year}-{match.group(2) or match.group(4)}"


def _parse_date(value: object | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        period = _parse_period(text)
        if period:
            return date(int(period[:4]), int(period[5:]), 1)
    return None


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _months_between(start: date, end: date) -> tuple[str, ...]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    values: list[str] = []
    while cursor <= last:
        values.append(_month_key(cursor))
        cursor = _next_month(cursor)
    return tuple(values)


def _period_values(value: object | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values: Iterable[object] = re.split(r"[,;，、\s]+", value)
    elif isinstance(value, Iterable):
        raw_values = value
    else:
        raw_values = (value,)
    return tuple(
        period
        for period in (_parse_period(item) for item in raw_values)
        if period is not None
    )


def _coverage_periods(
    record: Mapping[str, object] | object,
    *,
    as_of: date,
) -> tuple[str, ...]:
    explicit_periods = _period_values(_lookup(record, _PERIOD_FIELDS))
    if explicit_periods:
        return tuple(
            sorted(
                {
                    period
                    for period in explicit_periods
                    if _month_key(SCOPE_START) <= period <= _month_key(as_of)
                }
            )
        )

    for start_field, end_field in _DATE_RANGE_FIELDS:
        start = _parse_date(_lookup(record, (start_field,)))
        end = _parse_date(_lookup(record, (end_field,)))
        if start is None and end is None:
            continue
        range_start = max(start or end or SCOPE_START, SCOPE_START)
        range_end = min(end or start or as_of, as_of)
        return _months_between(range_start, range_end) if range_start <= range_end else ()

    for field in _SINGLE_DATE_FIELDS:
        identified = _parse_date(_lookup(record, (field,)))
        if identified is not None:
            if SCOPE_START <= identified <= as_of:
                return (_month_key(identified),)
            return ()
    path = _text(_lookup(record, ("path", "source_uri", "original_path")))
    filename = PureWindowsPath(path).name
    filename_periods = {
        f"{match.group(1) or '20' + match.group(3)}-"
        f"{match.group(2) or match.group(4)}"
        for match in _FILENAME_PERIOD_RE.finditer(filename)
    }
    if filename_periods:
        return tuple(
            sorted(
                period
                for period in filename_periods
                if _month_key(SCOPE_START) <= period <= _month_key(as_of)
            )
        )
    return ()


def _source_id(record: Mapping[str, object] | object, index: int) -> str:
    explicit = _text(
        _lookup(record, ("source_id", "snapshot_id", "record_id", "content_sha256"))
    )
    if explicit:
        return explicit
    path = _text(_lookup(record, ("path", "source_uri", "original_name")))
    digest = hashlib.sha256(f"{index}\0{path}".encode()).hexdigest()[:16]
    return f"anonymous_{digest}"


def _alias_index(aliases: Mapping[str, str] | None) -> dict[str, str]:
    return {
        _name_key(alias): _text(canonical)
        for alias, canonical in (aliases or {}).items()
        if _text(alias) and _text(canonical)
    }


def _canonical_store(
    record: Mapping[str, object] | object,
    candidate: str,
    aliases: Mapping[str, str],
) -> str:
    explicit = _text(
        _lookup(record, ("logical_store", "logical_store_name", "canonical_store"))
    )
    if explicit:
        return explicit
    return aliases.get(_name_key(candidate), candidate)


def _stable_key(platform: str, logical_store: str) -> str:
    digest = hashlib.sha256(
        f"{platform}\0{_name_key(logical_store)}".encode()
    ).hexdigest()[:20]
    return f"store_{digest}"


def _target_key(logical_store_key: str, period: str) -> str:
    digest = hashlib.sha256(
        f"{logical_store_key}\0{period}".encode()
    ).hexdigest()[:20]
    return f"target_{digest}"


def build_target_plan(
    records: Iterable[Mapping[str, object] | object] | Mapping[str, object],
    *,
    as_of: date | None = None,
    aliases: Mapping[str, str] | None = None,
    configured_stores: Iterable[Mapping[str, object] | object] | None = None,
) -> TargetPlan:
    """Build deterministic platform/store/month targets from inventory records.

    Directory names may help identify a store only behind a known business marker.
    Directory dates are deliberately ignored; periods come exclusively from
    content-derived or explicitly identified coverage metadata.
    """

    scope_end = as_of or date.today()
    if scope_end < SCOPE_START:
        raise ValueError("as_of cannot be earlier than 2026-02-01")
    known_aliases = _alias_index(aliases)
    groups: dict[tuple[str, str], _StoreGroup] = {}
    reviews: list[ReviewRequired] = []

    for record in _record_items(configured_stores or ()):
        platform = _platform_from_record(record)
        candidate = _store_candidate(record)
        if platform is None or _invalid_candidate_reason(candidate) is not None:
            continue
        assert candidate is not None
        logical_store = _canonical_store(record, candidate, known_aliases)
        logical_store_key = _stable_key(platform, logical_store)
        group = groups.setdefault(
            (platform, logical_store_key),
            _StoreGroup(
                platform=platform,
                logical_store=logical_store,
                logical_store_key=logical_store_key,
                aliases=set(),
                source_ids_by_period={},
                evidence_by_period={},
            ),
        )
        group.aliases.add(candidate)

    for index, record in enumerate(_record_items(records)):
        source_id = _source_id(record, index)
        purpose = _text(
            _lookup(record, ("purpose", "record_purpose"))
        ).casefold()
        if purpose in _GOVERNANCE_PURPOSES:
            continue
        platform = _platform_from_record(record)
        candidate = _store_candidate(record)

        if _is_pbix(record):
            path = _text(_lookup(record, ("path", "source_uri", "original_name")))
            reviews.append(
                ReviewRequired(
                    candidate=PureWindowsPath(path).name if path else candidate or "",
                    source_id=source_id,
                    platform=platform,
                    reason="pbix_asset_not_store",
                    explanation="PBIX 文件名不能作为店铺依据，需要其他电商数据证据",
                )
            )
            continue

        invalid = _invalid_candidate_reason(candidate)
        if invalid is not None:
            reason, explanation = invalid
            reviews.append(
                ReviewRequired(
                    candidate=candidate or "",
                    source_id=source_id,
                    platform=platform,
                    reason=reason,
                    explanation=explanation,
                )
            )
            continue
        assert candidate is not None

        if platform is None:
            candidate_key = _name_key(candidate)
            matching_platforms = {
                group.platform
                for group in groups.values()
                if candidate_key
                in {
                    _name_key(group.logical_store),
                    *(_name_key(alias) for alias in group.aliases),
                }
            }
            if len(matching_platforms) == 1:
                platform = next(iter(matching_platforms))

        if not _has_ecommerce_evidence(record):
            reviews.append(
                ReviewRequired(
                    candidate=candidate,
                    source_id=source_id,
                    platform=platform,
                    reason="missing_ecommerce_evidence",
                    explanation="候选名称缺少订单、账单、费用或成本等电商数据证据",
                )
            )
            continue
        if platform is None:
            reviews.append(
                ReviewRequired(
                    candidate=candidate,
                    source_id=source_id,
                    platform=None,
                    reason="platform_not_identified",
                    explanation="已发现电商数据，但无法确定所属平台",
                )
            )
            continue

        logical_store = _canonical_store(record, candidate, known_aliases)
        logical_store_key = _stable_key(platform, logical_store)
        group_key = (platform, logical_store_key)
        group = groups.setdefault(
            group_key,
            _StoreGroup(
                platform=platform,
                logical_store=logical_store,
                logical_store_key=logical_store_key,
                aliases=set(),
                source_ids_by_period={},
                evidence_by_period={},
            ),
        )
        group.aliases.add(candidate)
        for period in _coverage_periods(record, as_of=scope_end):
            group.source_ids_by_period.setdefault(period, set()).add(source_id)
            group.evidence_by_period.setdefault(period, set()).add(
                "content_or_identified_coverage"
            )

    all_periods = _months_between(SCOPE_START, scope_end)
    current_period = _month_key(scope_end)
    targets: list[MonthlyTarget] = []
    for group in sorted(
        groups.values(),
        key=lambda item: (item.platform, _name_key(item.logical_store)),
    ):
        for period in all_periods:
            source_ids = tuple(sorted(group.source_ids_by_period.get(period, set())))
            period_state = (
                PeriodState.PARTIAL if period == current_period else PeriodState.CLOSED
            )
            if period_state is PeriodState.PARTIAL:
                status = TargetStatus.PARTIAL
            elif not source_ids:
                status = TargetStatus.MISSING
            else:
                status = TargetStatus.AVAILABLE
            targets.append(
                MonthlyTarget(
                    target_key=_target_key(group.logical_store_key, period),
                    platform=group.platform,
                    logical_store=group.logical_store,
                    logical_store_key=group.logical_store_key,
                    period=period,
                    status=status,
                    period_state=period_state,
                    source_ids=source_ids,
                    evidence=tuple(
                        sorted(group.evidence_by_period.get(period, set()))
                    ),
                    aliases=tuple(sorted(group.aliases, key=_name_key)),
                )
            )

    reviews.sort(
        key=lambda item: (
            item.reason,
            item.platform or "",
            _name_key(item.candidate),
            item.source_id,
        )
    )
    return TargetPlan(
        scope_start=SCOPE_START,
        scope_end=scope_end,
        targets=tuple(targets),
        review_required=tuple(reviews),
    )


__all__ = [
    "MonthlyTarget",
    "PeriodState",
    "ReviewRequired",
    "SCOPE_START",
    "TargetPlan",
    "TargetStatus",
    "build_target_plan",
]
