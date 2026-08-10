"""原语一：识别。给一个文件，判断它是什么。

未登记的签名必须报警。静默丢列是这个行业最大的隐形杀手——现有系统里一个模板别名
少写一个字（`线上子订单号` 应为 `线上子订单编号`），导致全部商品成本挂不到订单，
潜伏很久无人发现，因为它不报错，只是退回去命中了另一个能匹配上的列。

已知规模：千牛明细 19 种签名、微信收款 6 种、拼多多广告 6 种、淘宝万相台 5 种、
聚水潭 5 种、抖店广告 3 种。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model.schema import Model, Template, signature_of, normalize_header
from .parse import ParseError, digest, read_headers
from .types import FileRef, RawTable, Recognition

#: 候选模板要报为"接近"至少得覆盖这么多比例的必需列。
_NEAR_MISS_FLOOR = 0.6


def recognize(path: str | Path, model: Model, sha: str | None = None) -> list[Recognition]:
    """识别一个文件。xlsx 每张工作表单独识别，一个文件里可以混放多种表。"""
    path = Path(path)
    sha = sha or digest(path)
    try:
        header_sets = read_headers(path)
    except ParseError as exc:
        return [
            Recognition(
                ref=FileRef(sha256=sha, filename=path.name),
                signature="",
                header_count=0,
                reason=str(exc),
            )
        ]

    results = []
    for i, headers in enumerate(header_sets):
        ref = FileRef(sha256=sha, filename=path.name, sheet=None if len(header_sets) == 1 else f"#{i + 1}")
        results.append(match_headers(headers, model, ref))
    return results


def recognize_table(table: RawTable, model: Model) -> Recognition:
    """识别一张已解析的表。"""
    return match_headers(table.headers, model, table.ref)


def match_headers(headers: list[str], model: Model, ref: FileRef) -> Recognition:
    """表头签名匹配。命中多个模板时取最具体的那个。"""
    present = {normalize_header(h) for h in headers if normalize_header(h)}
    result = Recognition(
        ref=ref,
        signature=signature_of(headers),
        header_count=len(headers),
    )

    if not present:
        result.reason = f"{ref.label()} 没有表头，可能是空表或表头不在第一行"
        return result

    hits: list[Template] = []
    for tpl in model.templates:
        need = {normalize_header(c) for c in tpl.match_columns}
        forbidden = {normalize_header(c) for c in tpl.exclude_columns}
        if need <= present and not (forbidden & present):
            hits.append(tpl)
        elif need and len(need & present) / len(need) >= _NEAR_MISS_FLOOR:
            result.near_misses.append((tpl.id, tuple(sorted(need - present))))

    if not hits:
        result.reason = _explain_miss(ref, headers, result.near_misses, model)
        result.unmapped_columns = sorted(present)
        return result

    # 并列时先看文件名线索：表头一样的两张表（补发表就是聚水潭表筛出来的）
    # 只能靠来源区分，这个线索写在数据源契约里，不是硬编码。
    hinted = [t for t in hits if _filename_hits(model, t.source, ref.filename)]
    if hinted:
        hits = hinted
    # 再按最具体优先：必需列多的模板赢。同样具体时取声明在前的。
    best = max(hits, key=lambda t: (len(t.match_columns), -model.templates.index(t)))
    result.template_id = best.id
    result.source_id = best.source
    consumed = {normalize_header(c) for b in best.bindings for c in b.columns}
    consumed |= {normalize_header(c) for c in best.match_columns}
    result.unmapped_columns = sorted(present - consumed)
    result.reason = f"认出来了：{model.source(best.source).name} · {best.name or best.id}"
    if len(hits) > 1:
        others = "、".join(t.id for t in hits if t is not best)
        result.reason += f"（也匹配 {others}，按必需列更多的取了 {best.id}）"
    return result


def _filename_hits(model: Model, source_id: str, filename: str) -> bool:
    try:
        hints = model.source(source_id).filename_hints
    except KeyError:
        return False
    return any(h in filename for h in hints)


def _explain_miss(
    ref: FileRef,
    headers: list[str],
    near_misses: list[tuple[str, tuple[str, ...]]],
    model: Model,
) -> str:
    """认不出来必须说清为什么，而不是丢一句 unknown template。"""
    head = f"{ref.label()} 没见过这种表头（{len(headers)} 列）"
    if not near_misses:
        return head + "。确认列映射后即可解析。"
    closest = min(near_misses, key=lambda x: len(x[1]))
    tpl_id, missing = closest
    try:
        name = model.template(tpl_id).name or tpl_id
    except KeyError:
        name = tpl_id
    return (
        f"{head}。最接近的是「{name}」，"
        f"差 {len(missing)} 列：{'、'.join(missing[:5])}"
        + ("…" if len(missing) > 5 else "")
        + "。可能是平台改版加减了列。"
    )


# --------------------------------------------------------------------------- #
# 从文件名推断上下文
# --------------------------------------------------------------------------- #

_PERIOD_PATTERNS = (
    re.compile(r"(20\d{2})\s*[-年._]\s*(1[0-2]|0?[1-9])\s*月?"),
    re.compile(r"(20\d{2})(1[0-2]|0[1-9])(?!\d)"),
    re.compile(r"(?<!\d)(2[0-9])(1[0-2]|0[1-9])(?!\d)"),  # 2606 这类两位年
)


def infer_period(text: str) -> str | None:
    """从文件名推断账期，返回 YYYY-MM。用户不该被要求手选月份。"""
    for i, pattern in enumerate(_PERIOD_PATTERNS):
        if m := pattern.search(text):
            year, month = m.group(1), m.group(2)
            if i == 2:
                year = f"20{year}"
            if 2000 <= int(year) <= 2099:
                return f"{year}-{int(month):02d}"
    return None


def infer_store(text: str, known_stores: list[str]) -> str | None:
    """从文件名推断店铺。只在已知店铺清单里找，不猜新店。"""
    hay = normalize_header(text)
    matches = [s for s in known_stores if normalize_header(s) and normalize_header(s) in hay]
    return max(matches, key=len) if matches else None
