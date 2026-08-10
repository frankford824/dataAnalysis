"""原语三：归一。把各平台的表达差异抹平，产出统一的内部表示。

四类归一：

  金额  → 有符号金额。符号规则绑模板版本，不绑列名——实测存在同一列里正负混存
          （微信收款无括号版 5 个文件：正 7260、负 1608），符号无法从数据本身推断。
  粒度  → 声明式去重。引擎提供"某些字段是父级字段，聚合前需按某个键去重"这一原语，
          具体哪些字段、按什么键，是模型数据。实测聚水潭应付金额直接求和相对
          去重后放大 1.54 到 3.03 倍。
  标识  → 多层 ID 并存（平台商品 ID、平台 SKU ID、内部编码），不强行统一。
  时间  → 归入五类语义槽位：下单、支付、发货、确认收货、结算。

符号的职责划分，两处不能重复施加：
  模板的 sign 修正**文件的编码怪癖**，产出真实经济符号（收入为正、支出为负）。
  指标的 sign 声明**会计方向**，只用于取值表达式产出的是量值而非有向金额的场合
  （聚水潭的 数量 × 成本价 是个量，需要指标声明它是成本）。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

import polars as pl

from ..model.schema import Template, TimeSlot, normalize_header
from .types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, RawTable

#: 标记同一去重键内的首行。父级字段只在首行计入，避免重复计算。
PARENT_FIRST = "__parent_first__"

#: 归一后金额字段的统一后缀，避免与原始角色名混淆。
AMOUNT_SIGN = "__sign__"


class NormalizeError(Exception):
    pass


def normalize(table: RawTable, template: Template) -> tuple[pl.DataFrame, list[str]]:
    """把原始表归一为按字段角色命名的数据帧。

    返回 (数据帧, 观察记录)。观察记录进入自检层，不静默吞掉。
    """
    notes = list(table.notes)
    index = _bind_columns(table.headers, template, notes)

    if not table.rows:
        notes.append(f"{table.ref.label()} 没有数据行")
        return _empty_frame(template), notes

    data: dict[str, list] = {role: [] for role in index}
    anchors: dict[str, list] = {ANCHOR_SHA: [], ANCHOR_FILE: [], ANCHOR_SHEET: [], ANCHOR_ROW: []}
    for row in table.rows:
        for role, pos in index.items():
            data[role].append(row.cells[pos] if pos < len(row.cells) else None)
        anchors[ANCHOR_SHA].append(table.ref.sha256)
        anchors[ANCHOR_FILE].append(table.ref.filename)
        anchors[ANCHOR_SHEET].append(table.ref.sheet or "")
        anchors[ANCHOR_ROW].append(row.row_no)

    frame = pl.DataFrame(
        {role: pl.Series(role, [None if v == "" else v for v in vals], dtype=pl.Utf8, strict=False)
         for role, vals in data.items()}
        | {k: pl.Series(k, v) for k, v in anchors.items()}
    )

    frame = _drop_total_rows(frame, template, notes)
    frame = _normalize_amounts(frame, template, notes)
    frame = _normalize_time(frame, template, notes)
    frame = _mark_parent_rows(frame, template, notes)
    return frame, notes


def _drop_total_rows(frame: pl.DataFrame, template: Template, notes: list[str]) -> pl.DataFrame:
    """丢掉表底的合计行。

    人手维护的表格底部常有一行合计。它的关联键为空但金额列有值，混进去会让每一列
    金额刚好翻倍——实测订单明细表就是这样，不处理的话利润直接翻倍。
    """
    marker = template.total_row_marker
    if not marker or marker not in frame.columns or frame.is_empty():
        return frame
    keep = pl.col(marker).is_not_null() & ~pl.col(marker).cast(pl.Utf8).str.strip_chars().is_in(
        ["", "合计", "总计", "小计", "汇总"]
    )
    kept = frame.filter(keep)
    dropped = frame.height - kept.height
    if dropped:
        notes.append(f"丢掉 {dropped} 行合计行（{marker} 为空或写着合计）")
    return kept


# --------------------------------------------------------------------------- #
# 列绑定
# --------------------------------------------------------------------------- #


def _bind_columns(headers: list[str], template: Template, notes: list[str]) -> dict[str, int]:
    """角色到列位置的绑定。按位置而非列名，重复列名才不会静默丢数据。"""
    normalized = [normalize_header(h) for h in headers]
    index: dict[str, int] = {}
    for binding in template.bindings:
        positions: list[int] = []
        for candidate in binding.columns:
            want = normalize_header(candidate)
            positions = [i for i, h in enumerate(normalized) if h == want]
            if positions:
                break
        if not positions:
            if binding.required:
                raise NormalizeError(
                    f"模板 {template.id} 要求的列没找到：角色 {binding.role} "
                    f"期望 {'、'.join(binding.columns)}，实际表头 {'、'.join(headers[:12])}"
                )
            notes.append(f"选填列缺失：{binding.role}（期望 {'、'.join(binding.columns)}）")
            continue
        if binding.occurrence >= len(positions):
            raise NormalizeError(
                f"模板 {template.id} 的角色 {binding.role} 要第 {binding.occurrence + 1} 个"
                f"同名列，但只找到 {len(positions)} 个"
            )
        if len(positions) > 1:
            notes.append(
                f"列名 {binding.columns[0]} 重复出现 {len(positions)} 次，"
                f"角色 {binding.role} 按位置取第 {binding.occurrence + 1} 个"
            )
        index[binding.role] = positions[binding.occurrence]
    return index


def _empty_frame(template: Template) -> pl.DataFrame:
    cols = {b.role: pl.Series(b.role, [], dtype=pl.Utf8) for b in template.bindings}
    cols |= {
        ANCHOR_SHA: pl.Series(ANCHOR_SHA, [], dtype=pl.Utf8),
        ANCHOR_FILE: pl.Series(ANCHOR_FILE, [], dtype=pl.Utf8),
        ANCHOR_SHEET: pl.Series(ANCHOR_SHEET, [], dtype=pl.Utf8),
        ANCHOR_ROW: pl.Series(ANCHOR_ROW, [], dtype=pl.Int64),
        PARENT_FIRST: pl.Series(PARENT_FIRST, [], dtype=pl.Boolean),
    }
    return pl.DataFrame(cols)


# --------------------------------------------------------------------------- #
# 金额
# --------------------------------------------------------------------------- #

_NUM_KEEP = re.compile(r"[^\d.\-+eE]")
_BRACKET = re.compile(r"^[(（]\s*(.+?)\s*[)）]$")
#: 数值型角色的判定：模板显式声明的金额与数量角色。
_NUMERIC_HINT = re.compile(
    r"(amount|money|fee|cost|price|spend|qty|quantity|rate|ratio|count|num|income|outgo)", re.I
)


def to_number(value: object) -> float | None:
    """把各种脏写法解成数。括号表示负数，这是会计表格的通用约定。

    实测遇到的写法：`1,234.56`、`¥1,234.56`、`1234.56元`、`(123.45)`、`（123.45）`、
    `12%`、`无退款申请`（混合类型字段）、`-`（空值占位）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    s = str(value).strip()
    if not s:
        return None

    negative = False
    if m := _BRACKET.match(s):
        negative, s = True, m.group(1)

    percent = s.endswith("%")
    cleaned = _NUM_KEEP.sub("", s)
    if not cleaned or cleaned in ("-", "+", ".", "-.", "e", "E"):
        return None
    try:
        num = float(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None
    if percent:
        num /= 100
    return -abs(num) if negative else num


def _numeric_roles(template: Template) -> list[str]:
    """哪些角色要转成数值。

    模板显式声明了就听它的，没声明才按角色名猜。猜是有代价的：漏判的列静默留成
    文本，等到有指标对它求和才炸，而且炸出来的错和业务无关。所以新模板应该写 kind。
    """
    out = []
    for b in template.bindings:
        if b.kind:
            if b.kind == "number":
                out.append(b.role)
        elif _NUMERIC_HINT.search(b.role):
            out.append(b.role)
    return out


def _normalize_amounts(frame: pl.DataFrame, template: Template, notes: list[str]) -> pl.DataFrame:
    roles = [r for r in _numeric_roles(template) if r in frame.columns]
    if roles:
        frame = frame.with_columns(
            [
                pl.col(r)
                .map_elements(to_number, return_dtype=pl.Float64)
                .alias(r)
                for r in roles
            ]
        )

    # 声明了 negate 的角色在这里取反，把各来源不一致的符号约定拉齐，
    # 下游算钱时就不用再记「这张表的支出是正是负」。
    flipped = [
        b.role for b in template.bindings if b.negate and b.role in frame.columns and b.role in roles
    ]
    if flipped:
        frame = frame.with_columns([(-pl.col(r)).alias(r) for r in flipped])
        notes.append(f"按符号约定取反：{'、'.join(flipped)}")

    sign = template.sign
    if sign == "as_is":
        factor = pl.lit(1.0)
    elif sign == "negate":
        factor = pl.lit(-1.0)
    elif sign == "abs_negate":
        factor = pl.lit(1.0)  # 取绝对值后再取负，见下
    elif sign == "abs_positive":
        factor = pl.lit(1.0)
    elif sign == "by_direction":
        role = template.direction_role
        if role not in frame.columns:
            raise NormalizeError(f"模板 {template.id} 的方向列角色 {role} 没有绑定")
        outflow = [str(v) for v in template.direction_outflow_values]
        factor = (
            pl.when(pl.col(role).cast(pl.Utf8).str.strip_chars().is_in(outflow))
            .then(pl.lit(-1.0))
            .otherwise(pl.lit(1.0))
        )
        notes.append(f"符号取自方向列 {role}，支出取值：{'、'.join(outflow)}")
    else:  # pragma: no cover
        raise NormalizeError(f"未知符号规则 {sign}")

    money = [r for r in roles if not re.search(r"(qty|quantity|rate|ratio|count|num|price)", r, re.I)]
    if not money:
        return frame.with_columns(pl.lit(1.0).alias(AMOUNT_SIGN))

    exprs = []
    for r in money:
        col = pl.col(r)
        if sign == "abs_negate":
            exprs.append((-col.abs()).alias(r))
        elif sign == "abs_positive":
            exprs.append(col.abs().alias(r))
        else:
            exprs.append((col * factor).alias(r))
    return frame.with_columns(exprs).with_columns(factor.alias(AMOUNT_SIGN))


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y%m%d%H%M%S", "%Y%m%d",
    "%Y年%m月%d日", "%Y.%m.%d",
)


def to_date(value: object) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Excel 序列号
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    s = s.replace("T", " ").split("+")[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if m := re.match(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})", s):
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _normalize_time(frame: pl.DataFrame, template: Template, notes: list[str]) -> pl.DataFrame:
    """把时间列归入五类语义槽位。各平台叫法不同，语义只有这五种。"""
    exprs = []
    for slot, role in template.time_slots.items():
        if role not in frame.columns:
            notes.append(f"时间槽位 {slot} 的来源列 {role} 缺失")
            continue
        exprs.append(pl.col(role).map_elements(to_date, return_dtype=pl.Date).alias(str(slot)))
    if not exprs:
        return frame
    frame = frame.with_columns(exprs)
    for slot in template.time_slots:
        col = str(slot)
        if col in frame.columns:
            bad = frame.select(
                (pl.col(template.time_slots[slot]).is_not_null() & pl.col(col).is_null()).sum()
            ).item()
            if bad:
                notes.append(f"{slot} 有 {bad} 行日期解不出来")
    return frame


def period_of(frame: pl.DataFrame, slot: TimeSlot | str) -> pl.Expr:
    """账期表达式。时间归属依据由模型声明，引擎只执行。"""
    col = str(slot)
    if col not in frame.columns:
        return pl.lit(None, dtype=pl.Utf8)
    return pl.col(col).dt.strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# 粒度
# --------------------------------------------------------------------------- #


def _mark_parent_rows(frame: pl.DataFrame, template: Template, notes: list[str]) -> pl.DataFrame:
    """标记去重键内的首行。父级字段只在首行计入。"""
    rule = template.dedup
    if not rule.key or not rule.parent_fields:
        return frame.with_columns(pl.lit(True).alias(PARENT_FIRST))

    keys = [k for k in rule.key if k in frame.columns]
    if len(keys) != len(rule.key):
        missing = set(rule.key) - set(keys)
        notes.append(f"去重键缺列 {'、'.join(missing)}，本表父级字段按行级计入（可能重复计算）")
        return frame.with_columns(pl.lit(True).alias(PARENT_FIRST))

    frame = frame.with_columns(
        (pl.int_range(pl.len()).over(keys) == 0).alias(PARENT_FIRST)
    )
    dupes = frame.height - int(frame.select(pl.col(PARENT_FIRST).sum()).item())
    if dupes:
        notes.append(
            f"按 {'+'.join(keys)} 去重后减少 {dupes} 行（{dupes / frame.height:.1%}），"
            f"父级字段 {'、'.join(rule.parent_fields)} 只在首行计入"
        )
    return frame


def is_parent_only(template: Template, roles: tuple[str, ...]) -> bool:
    """取值表达式是否只引用父级字段。是则聚合前必须按去重键取首行。"""
    parents = set(template.dedup.parent_fields)
    return bool(roles) and set(roles) <= parents
