"""Finite-template normalization without persistence or orchestration.

The public boundary in this module is deliberately small:

``normalize_bytes(name, content, route_payload)`` consumes an immutable snapshot's
bytes plus the already adjudicated finite-template route.  It never reroutes to a
different template, infers missing reconciliation keys, writes artifacts, or
touches the metadata database.

XLSX monetary cells are read from their original worksheet XML text.  This keeps
IEEE-754 floats out of every monetary conversion.
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from openpyxl.utils.datetime import MAC_EPOCH, WINDOWS_EPOCH, from_excel

from .kernel.money import amount, negate_money, sum_money
from .parse.csv_profile import profile_csv
from .parse.templates import normalize_header
from .parse.xlsx import profile_xlsx
from .parse.zip_safe import read_safe_member
from .rules.wallet import WalletRuleSet

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF = re.compile(r"^([A-Z]+)([1-9]\d*)$")
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_REFUND_ZERO_TEXT = frozenset({"无退款申请", "无退款", "未退款", "无"})
_WALLET_RULES = WalletRuleSet()


class CanonicalSide(StrEnum):
    """The explicit carrier of a normalized row.

    ``operational`` keeps P&L inputs such as freight and cost out of a three-way
    order/platform/cash reconciliation until a contract explicitly places them.
    """

    ORDER = "order"
    PLATFORM = "platform"
    CASH = "cash"
    OPERATIONAL = "operational"


class IssueSeverity(StrEnum):
    REJECTED = "rejected"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """A deterministic monetary row with source evidence.

    ``source_snapshot_id`` is intentionally optional because ``normalize_bytes``
    has no database dependency.  The caller may attach the frozen snapshot ID
    with ``dataclasses.replace`` before persistence.
    """

    dataset_kind: str
    source_type: str
    side: CanonicalSide
    business_key: str
    cash_bridge_key: str | None
    occurred_at: datetime
    amount: Decimal
    period_key: str
    evidence_row: int
    source_name: str
    source_snapshot_id: str | None = None
    settlement_batch_id: str | None = None
    source_member: str | None = None
    source_sheet: str | None = None
    metric: str | None = None
    sku: str | None = None
    attributes: Mapping[str, str] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("dataset_kind", self.dataset_kind),
            ("source_type", self.source_type),
            ("business_key", self.business_key),
            ("period_key", self.period_key),
            ("source_name", self.source_name),
        ):
            if not value.strip():
                raise ValueError(f"{label} is required")
        if not re.fullmatch(r"\d{4}", self.period_key):
            raise ValueError("period_key must use YYMM")
        if self.evidence_row < 1:
            raise ValueError("evidence_row must be positive")
        object.__setattr__(self, "amount", amount(self.amount))
        object.__setattr__(
            self,
            "attributes",
            dict(sorted((str(key), str(value)) for key, value in self.attributes.items())),
        )


@dataclass(frozen=True, slots=True)
class NormalizationIssue:
    code: str
    message: str
    source_name: str
    severity: IssueSeverity = IssueSeverity.REJECTED
    template_id: str | None = None
    evidence_row: int | None = None
    field: str | None = None
    source_member: str | None = None
    source_sheet: str | None = None
    details: Mapping[str, str] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip() or not self.source_name.strip():
            raise ValueError("issue code, message, and source_name are required")
        if self.evidence_row is not None and self.evidence_row < 1:
            raise ValueError("issue evidence_row must be positive")
        object.__setattr__(
            self,
            "details",
            dict(sorted((str(key), str(value)) for key, value in self.details.items())),
        )


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    rows: tuple[CanonicalRow, ...]
    issues: tuple[NormalizationIssue, ...]


@dataclass(frozen=True, slots=True)
class _CellValue:
    text: str | None
    formula: str | None = None
    cell_type: str = ""


@dataclass(frozen=True, slots=True)
class _RouteContext:
    source_name: str
    template_id: str
    source_kind: str
    fields: Mapping[str, str]
    header_row: int
    sheet: str | None = None
    member: str | None = None
    explicit_period_key: str | None = None


class _RejectedRow(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field_name: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.details = details or {}


RowHandler = Callable[
    [_RouteContext, int, Mapping[str, _CellValue]],
    tuple[CanonicalRow, ...],
]


def normalize_bytes(
    name: str,
    content: bytes,
    route_payload: Mapping[str, Any],
) -> NormalizationResult:
    """Normalize one frozen snapshot from its matched finite-template route.

    Invalid files, unsafe/missing keys, unsupported control outputs, formulas,
    and malformed money are returned as structured issues.  A bad row does not
    erase valid rows from the same source.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if not isinstance(route_payload, Mapping):
        raise TypeError("route_payload must be a mapping")

    rows: list[CanonicalRow] = []
    issues: list[NormalizationIssue] = []
    kind = route_payload.get("kind")
    try:
        if kind == "file":
            context = _file_context(name, route_payload)
            _normalize_entry(content, context, rows, issues)
        elif kind == "archive":
            entries = route_payload.get("entries")
            if not isinstance(entries, list) or not entries:
                raise ValueError("archive route requires at least one entry")
            for raw_entry in entries:
                if not isinstance(raw_entry, Mapping):
                    raise ValueError("archive route entry must be an object")
                context = _archive_context(
                    name,
                    raw_entry,
                    inherited_period_key=route_payload.get("period_key"),
                )
                try:
                    member_content = read_safe_member(content, context.member or "")
                except Exception as exc:
                    issues.append(
                        _issue(
                            context,
                            "archive_member_unreadable",
                            "已匹配的压缩包成员无法安全读取",
                            details={"error_type": type(exc).__name__},
                        )
                    )
                    continue
                _normalize_entry(member_content, context, rows, issues)
            unmatched = route_payload.get("unmatched_members", [])
            if isinstance(unmatched, list):
                for member in unmatched:
                    issues.append(
                        NormalizationIssue(
                            code="archive_member_unmatched",
                            message="压缩包成员没有有限模板匹配，未进入标准化",
                            source_name=name,
                            source_member=str(member),
                            severity=IssueSeverity.WARNING,
                        )
                    )
        else:
            raise ValueError("route kind must be 'file' or 'archive'")
    except (BadZipFile, csv.Error, UnicodeError, ValueError) as exc:
        issues.append(
            NormalizationIssue(
                code="invalid_route_or_source",
                message="快照内容与已匹配路由不一致，无法安全标准化",
                source_name=name,
                details={"error_type": type(exc).__name__},
            )
        )
    return NormalizationResult(rows=tuple(rows), issues=tuple(issues))


def _file_context(name: str, payload: Mapping[str, Any]) -> _RouteContext:
    location = payload.get("location")
    if not isinstance(location, Mapping):
        raise ValueError("file route location is required")
    return _context(
        name,
        payload,
        header_row=location.get("header_row"),
        sheet=location.get("sheet"),
        member=None,
        explicit_period_key=payload.get("period_key"),
    )


def _archive_context(
    name: str,
    payload: Mapping[str, Any],
    *,
    inherited_period_key: Any = None,
) -> _RouteContext:
    member = payload.get("member")
    if not isinstance(member, str) or not member:
        raise ValueError("archive route member is required")
    return _context(
        name,
        payload,
        header_row=payload.get("header_row"),
        sheet=payload.get("sheet"),
        member=member,
        explicit_period_key=payload.get("period_key", inherited_period_key),
    )


def _context(
    name: str,
    payload: Mapping[str, Any],
    *,
    header_row: Any,
    sheet: Any,
    member: str | None,
    explicit_period_key: Any,
) -> _RouteContext:
    template_id = payload.get("template_id")
    source_kind = payload.get("source_kind")
    fields = payload.get("fields")
    if not isinstance(template_id, str) or not template_id:
        raise ValueError("template_id is required")
    if not isinstance(source_kind, str) or not source_kind:
        raise ValueError("source_kind is required")
    if not isinstance(fields, Mapping) or not fields:
        raise ValueError("route fields are required")
    if not isinstance(header_row, int) or header_row < 1:
        raise ValueError("header_row must be positive")
    if sheet is not None and not isinstance(sheet, str):
        raise ValueError("sheet must be text or null")
    if explicit_period_key is not None and (
        not isinstance(explicit_period_key, str)
        or not re.fullmatch(r"\d{4}", explicit_period_key)
    ):
        raise ValueError("explicit period_key must use YYMM")
    normalized_fields: dict[str, str] = {}
    for semantic_name, source_header in fields.items():
        if not isinstance(semantic_name, str) or not isinstance(source_header, str):
            raise ValueError("route fields must map text to text")
        normalized_fields[semantic_name] = source_header
    return _RouteContext(
        source_name=name,
        template_id=template_id,
        source_kind=source_kind,
        fields=normalized_fields,
        header_row=header_row,
        sheet=sheet,
        member=member,
        explicit_period_key=explicit_period_key,
    )


def _normalize_entry(
    content: bytes,
    context: _RouteContext,
    rows: list[CanonicalRow],
    issues: list[NormalizationIssue],
) -> None:
    effective_name = context.member or context.source_name
    if Path(effective_name).suffix.casefold() == ".xlsx" and context.sheet is None:
        try:
            profile = profile_xlsx(
                content,
                member_name=context.member or context.source_name,
            )
            context = replace(
                context,
                sheet=_resolve_sheet(profile.candidates, context),
            )
        except Exception as exc:
            issues.append(
                _issue(
                    context,
                    "source_read_failed",
                    "已匹配 XLSX 无法定位唯一工作表",
                    details={"error_type": type(exc).__name__},
                )
            )
            return
    handler = _HANDLERS.get(context.template_id)
    if handler is None:
        issues.append(
            _issue(
                context,
                "unsupported_template",
                "该有限模板不是交易标准化输入",
            )
        )
        return
    expected_kind = _EXPECTED_SOURCE_KINDS[context.template_id]
    if context.source_kind != expected_kind:
        issues.append(
            _issue(
                context,
                "route_source_kind_mismatch",
                "模板与来源类型不一致，拒绝继续",
                details={
                    "expected_source_kind": expected_kind,
                    "actual_source_kind": context.source_kind,
                },
            )
        )
        return

    before = len(rows)
    try:
        source_rows = _iter_source_rows(content, context)
        for row_number, values in source_rows:
            try:
                rows.extend(handler(context, row_number, values))
            except _RejectedRow as exc:
                issues.append(
                    _issue(
                        context,
                        exc.code,
                        str(exc),
                        row_number=row_number,
                        field_name=exc.field_name,
                        details=exc.details,
                    )
                )
    except Exception as exc:
        issues.append(
            _issue(
                context,
                "source_read_failed",
                "已匹配来源无法按原始结构读取",
                details={
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:300],
                },
            )
        )
        return
    if len(rows) == before and not any(
        issue.template_id == context.template_id
        and issue.source_member == context.member
        and issue.severity == IssueSeverity.REJECTED
        for issue in issues
    ):
        issues.append(
            _issue(context, "no_data_rows", "表头之后没有可标准化的数据行")
        )


def _iter_source_rows(
    content: bytes,
    context: _RouteContext,
) -> Iterator[tuple[int, Mapping[str, _CellValue]]]:
    effective_name = context.member or context.source_name
    suffix = Path(effective_name).suffix.casefold()
    if suffix == ".csv":
        yield from _iter_csv_rows(content, context)
        return
    if suffix == ".xlsx":
        yield from _iter_xlsx_rows(content, context)
        return
    raise ValueError(f"unsupported routed member type: {suffix or '<none>'}")


def _iter_csv_rows(
    content: bytes,
    context: _RouteContext,
) -> Iterator[tuple[int, Mapping[str, _CellValue]]]:
    profile = profile_csv(content, member_name=context.member or context.source_name)
    text = content.decode(profile.encoding or "utf-8", errors="strict")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=profile.delimiter or ",")
    semantic_indexes: dict[str, int] | None = None
    for record_number, row in enumerate(reader, start=1):
        if record_number == context.header_row:
            semantic_indexes = _header_indexes(row, context.fields)
            continue
        if record_number <= context.header_row:
            continue
        if semantic_indexes is None:
            raise ValueError("route header row was not found")
        values = {
            semantic: _CellValue(row[index] if index < len(row) else None)
            for semantic, index in semantic_indexes.items()
        }
        if _has_source_value(values):
            yield record_number, values
    if semantic_indexes is None:
        raise ValueError("route header row exceeds CSV record count")


def _header_indexes(
    headers: Iterable[object],
    fields: Mapping[str, str],
) -> dict[str, int]:
    normalized: dict[str, int] = {}
    duplicates: set[str] = set()
    for index, header in enumerate(headers):
        key = normalize_header("" if header is None else str(header))
        if not key:
            continue
        if key in normalized:
            duplicates.add(key)
        else:
            normalized[key] = index
    result: dict[str, int] = {}
    for semantic, source_header in fields.items():
        key = normalize_header(source_header)
        if key in duplicates:
            raise ValueError(f"routed header is duplicated: {source_header}")
        if key not in normalized:
            raise ValueError(f"routed header is missing: {source_header}")
        result[semantic] = normalized[key]
    return result


def _iter_xlsx_rows(
    content: bytes,
    context: _RouteContext,
) -> Iterator[tuple[int, Mapping[str, _CellValue]]]:
    # Reuse the package safety limits before reading worksheet XML.
    profile = profile_xlsx(content, member_name=context.member or context.source_name)
    sheet_name = context.sheet or _resolve_sheet(profile.candidates, context)
    with ZipFile(io.BytesIO(content), "r") as archive:
        sheet_path, epoch = _xlsx_sheet_path_and_epoch(archive, sheet_name)
        shared_strings = _xlsx_shared_strings(archive)
        semantic_columns = _xlsx_header_columns(
            archive,
            sheet_path,
            shared_strings,
            context,
        )
        reverse_columns = {column: semantic for semantic, column in semantic_columns.items()}
        with archive.open(sheet_path) as stream:
            current_row: int | None = None
            current_values: dict[str, _CellValue] = {}
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag != f"{{{_MAIN_NS}}}c":
                    continue
                reference = element.attrib.get("r")
                match = _CELL_REF.fullmatch(reference or "")
                if match is None:
                    element.clear()
                    continue
                column, row_number_text = match.groups()
                row_number = int(row_number_text)
                if current_row is not None and row_number != current_row:
                    if current_row > context.header_row and _has_source_value(current_values):
                        yield current_row, current_values
                    current_values = {}
                current_row = row_number
                semantic = reverse_columns.get(column)
                if semantic is not None and row_number > context.header_row:
                    value = _xlsx_cell_value(element, shared_strings)
                    if (
                        semantic in {"business_date", "accounting_date"}
                        and value.formula is None
                    ):
                        value = _xlsx_date_value(value, epoch)
                    current_values[semantic] = value
                element.clear()
            if (
                current_row is not None
                and current_row > context.header_row
                and _has_source_value(current_values)
            ):
                yield current_row, current_values


def _resolve_sheet(candidates: Iterable[Any], context: _RouteContext) -> str:
    expected = {normalize_header(value) for value in context.fields.values()}
    matches: set[str] = {
        str(candidate.sheet_name)
        for candidate in candidates
        if candidate.header_row == context.header_row
        and not candidate.sheet_hidden
        and expected.issubset({normalize_header(value) for value in candidate.headers})
        and candidate.sheet_name
    }
    if len(matches) != 1:
        raise ValueError("archived XLSX route does not identify one worksheet")
    return next(iter(matches))


def _xlsx_sheet_path_and_epoch(
    archive: ZipFile,
    sheet_name: str,
) -> tuple[str, datetime]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    workbook_properties = workbook.find(f"{{{_MAIN_NS}}}workbookPr")
    epoch = (
        MAC_EPOCH
        if workbook_properties is not None
        and workbook_properties.attrib.get("date1904") in {"1", "true", "True"}
        else WINDOWS_EPOCH
    )
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("XLSX contains no worksheets")
    for sheet in sheets:
        if sheet.attrib.get("name") != sheet_name:
            continue
        relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
        target = targets.get(relationship_id or "")
        if target is None:
            break
        path = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        if path.startswith("../") or path.startswith("/"):
            raise ValueError("worksheet relationship escapes XLSX package")
        return path, epoch
    raise ValueError(f"routed worksheet does not exist: {sheet_name}")


def _xlsx_shared_strings(archive: ZipFile) -> tuple[str, ...]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    return tuple(
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    )


def _xlsx_header_columns(
    archive: ZipFile,
    sheet_path: str,
    shared_strings: tuple[str, ...],
    context: _RouteContext,
) -> dict[str, str]:
    header_by_column: dict[str, str] = {}
    with archive.open(sheet_path) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag != f"{{{_MAIN_NS}}}c":
                continue
            reference = element.attrib.get("r")
            match = _CELL_REF.fullmatch(reference or "")
            if match is None:
                element.clear()
                continue
            column, row_number = match.groups()
            row = int(row_number)
            if row > context.header_row:
                element.clear()
                break
            if row == context.header_row:
                value = _xlsx_cell_value(element, shared_strings)
                if value.text is not None:
                    header_by_column[column] = value.text
            element.clear()
    normalized: dict[str, str] = {}
    duplicates: set[str] = set()
    for column, header in header_by_column.items():
        key = normalize_header(header)
        if not key:
            continue
        if key in normalized:
            duplicates.add(key)
        else:
            normalized[key] = column
    result: dict[str, str] = {}
    for semantic, source_header in context.fields.items():
        key = normalize_header(source_header)
        if key in duplicates:
            raise ValueError(f"routed XLSX header is duplicated: {source_header}")
        if key not in normalized:
            raise ValueError(f"routed XLSX header is missing: {source_header}")
        result[semantic] = normalized[key]
    return result


def _xlsx_cell_value(
    element: ET.Element,
    shared_strings: tuple[str, ...],
) -> _CellValue:
    cell_type = element.attrib.get("t", "")
    formula_node = element.find(f"{{{_MAIN_NS}}}f")
    formula = formula_node.text if formula_node is not None else None
    value_node = element.find(f"{{{_MAIN_NS}}}v")
    raw_text = value_node.text if value_node is not None else None
    if cell_type == "s" and raw_text is not None:
        try:
            raw_text = shared_strings[int(raw_text)]
        except (IndexError, ValueError) as exc:
            raise ValueError("invalid XLSX shared-string index") from exc
    elif cell_type == "inlineStr":
        inline = element.find(f"{{{_MAIN_NS}}}is")
        raw_text = (
            "".join(node.text or "" for node in inline.iter(f"{{{_MAIN_NS}}}t"))
            if inline is not None
            else ""
        )
    return _CellValue(text=raw_text, formula=formula, cell_type=cell_type)


def _xlsx_date_value(value: _CellValue, epoch: datetime) -> _CellValue:
    text = _clean_text(value.text)
    if (
        text is None
        or value.cell_type not in {"", "n"}
        or not _DECIMAL_TEXT.fullmatch(text)
    ):
        return value
    try:
        serial = Decimal(text)
        if serial <= 0 or serial >= 100000:
            return value
        converted = from_excel(float(serial), epoch=epoch)
    except (InvalidOperation, OverflowError, ValueError):
        return value
    if isinstance(converted, datetime):
        return _CellValue(converted.isoformat(sep=" "), value.formula, "d")
    if isinstance(converted, date):
        return _CellValue(converted.isoformat(), value.formula, "d")
    return value


def _has_source_value(values: Mapping[str, _CellValue]) -> bool:
    return any(_clean_text(value.text) is not None or value.formula for value in values.values())


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _text(
    values: Mapping[str, _CellValue],
    field_name: str,
    *,
    required: bool = True,
) -> str | None:
    value = values.get(field_name)
    if value is not None and value.formula is not None:
        raise _RejectedRow(
            "formula_not_allowed",
            "标准化字段包含未经审核的公式",
            field_name=field_name,
        )
    cleaned = _clean_text(value.text if value is not None else None)
    if required and cleaned is None:
        raise _RejectedRow(
            "missing_required_value",
            "标准化所需字段为空",
            field_name=field_name,
        )
    return cleaned


def _money(
    values: Mapping[str, _CellValue],
    field_name: str,
    *,
    blank_is_zero: bool = False,
) -> Decimal:
    text = _text(values, field_name, required=not blank_is_zero)
    if text is None and blank_is_zero:
        return Decimal("0.0000")
    try:
        return amount(text or "")
    except (TypeError, ValueError) as exc:
        raise _RejectedRow(
            "invalid_money",
            "金额不是可安全转换的十进制定点数",
            field_name=field_name,
            details={"error_type": type(exc).__name__},
        ) from exc


def _refund_money(values: Mapping[str, _CellValue]) -> Decimal:
    text = _text(values, "refund_amount", required=False)
    if text is None or text in _REFUND_ZERO_TEXT:
        return Decimal("0.0000")
    try:
        return amount(text)
    except (TypeError, ValueError) as exc:
        raise _RejectedRow(
            "invalid_money",
            "退款金额不是可安全转换的十进制定点数",
            field_name="refund_amount",
            details={"error_type": type(exc).__name__},
        ) from exc


def _decimal_quantity(values: Mapping[str, _CellValue]) -> Decimal:
    text = _text(values, "quantity")
    normalized = (text or "").replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise _RejectedRow(
            "invalid_quantity",
            "数量不是有效十进制数",
            field_name="quantity",
        ) from exc
    if not value.is_finite():
        raise _RejectedRow(
            "invalid_quantity",
            "数量必须是有限十进制数",
            field_name="quantity",
        )
    return value


def _occurred_at(
    values: Mapping[str, _CellValue],
    *field_names: str,
) -> tuple[datetime, str]:
    for field_name in field_names:
        text = _text(values, field_name, required=False)
        if text is None:
            continue
        if _spans_multiple_months(text):
            raise _RejectedRow(
                "cross_month_period",
                "来源内容覆盖多个自然月，不能压成一个期间",
                field_name=field_name,
            )
        parsed = _parse_datetime(text)
        if parsed is not None:
            precision = "month" if re.fullmatch(r"\d{6}|\d{4}-\d{2}", text) else "exact"
            return parsed, precision
        raise _RejectedRow(
            "invalid_datetime",
            "业务时间无法按有限日期格式解析",
            field_name=field_name,
        )
    raise _RejectedRow(
        "missing_occurred_at",
        "来源内容没有可证明所属期间的业务时间",
        field_name=field_names[0] if field_names else None,
    )


def _parse_datetime(text: str) -> datetime | None:
    normalized = text.strip().replace("/", "-")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for pattern in ("%Y%m%d", "%Y%m", "%Y-%m", "%Y年%m月%d日", "%Y年%m月"):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    return None


def _spans_multiple_months(text: str) -> bool:
    candidates = re.findall(
        r"(?:20\d{2}[-/]?\d{2}(?:[-/]?\d{2})?)",
        text,
    )
    months: set[tuple[int, int]] = set()
    for candidate in candidates:
        compact = candidate.replace("-", "").replace("/", "")
        if len(compact) < 6:
            continue
        try:
            months.add((int(compact[:4]), int(compact[4:6])))
        except ValueError:
            continue
    return len(months) > 1


def _base_attributes(values: Mapping[str, _CellValue]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for name, value in values.items():
        # Cached formula output is not source evidence.  Optional descriptive
        # fields may be retained only when the source cell itself is literal.
        if value.formula is not None:
            continue
        cleaned = _clean_text(value.text)
        if cleaned is not None:
            attributes[name] = cleaned
    return attributes


def _keys(values: Mapping[str, _CellValue]) -> tuple[str | None, str | None]:
    settlement_batch_id = _text(values, "settlement_batch_id", required=False)
    cash_bridge_key = _text(values, "cash_bridge_key", required=False)
    return settlement_batch_id, cash_bridge_key


def _wallet_rule_context(
    values: Mapping[str, _CellValue],
    *,
    merchant_order_id: str | None,
    occurred_at: datetime,
) -> tuple[str | None, dict[str, str]]:
    description_cell = values.get("business_description")
    description = (
        _clean_text(description_cell.text)
        if description_cell is not None and description_cell.formula is None
        else None
    )
    classification = _WALLET_RULES.classify_business_description(description)
    order_key = _WALLET_RULES.extract_order_key(
        merchant_order_id=merchant_order_id,
        remark=description,
        occurred_at_text=occurred_at.isoformat(),
        classification_value=classification.value,
    )
    attributes = {
        "wallet_ruleset_version": _WALLET_RULES.version,
        "wallet_ruleset_checksum": _WALLET_RULES.checksum,
        "wallet_classification_status": (
            "matched" if classification.matched else "unmatched"
        ),
        "wallet_classification_rule_id": classification.rule_id or "",
        "wallet_classification": classification.value or "",
        "wallet_classification_unmatched_reason": (
            classification.unmatched_reason or ""
        ),
        "wallet_order_key_status": "matched" if order_key.matched else "unmatched",
        "wallet_order_key_rule_id": order_key.rule_id or "",
        "wallet_order_key_kind": order_key.value_kind or "",
        "wallet_order_key_unmatched_reason": order_key.unmatched_reason or "",
    }
    if description_cell is not None and description_cell.formula is not None:
        attributes["wallet_business_description_formula_ignored"] = "true"
    resolved_order_key = (
        order_key.value
        if order_key.matched and order_key.value_kind == "order_key"
        else None
    )
    if order_key.matched and order_key.value_kind == "legacy_grouping_key":
        attributes["wallet_legacy_grouping_key"] = order_key.value or ""
    return resolved_order_key, attributes


def _canonical(
    context: _RouteContext,
    row_number: int,
    *,
    dataset_kind: str,
    source_type: str,
    side: CanonicalSide,
    business_key: str,
    occurred_at: datetime,
    value: Decimal,
    attributes: Mapping[str, str],
    metric: str | None = None,
    sku: str | None = None,
    period_key: str | None = None,
    settlement_batch_id: str | None = None,
    cash_bridge_key: str | None = None,
) -> CanonicalRow:
    return CanonicalRow(
        dataset_kind=dataset_kind,
        source_type=source_type,
        side=side,
        business_key=business_key,
        settlement_batch_id=settlement_batch_id,
        cash_bridge_key=cash_bridge_key,
        occurred_at=occurred_at,
        amount=value,
        period_key=period_key or occurred_at.strftime("%y%m"),
        evidence_row=row_number,
        source_name=context.source_name,
        source_member=context.member,
        source_sheet=context.sheet,
        metric=metric,
        sku=sku,
        attributes=attributes,
    )


def _normalize_order(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    order_id = _text(values, "order_id") or ""
    sku = _text(values, "sku", required=False)
    occurred_at, precision = _occurred_at(values, "business_date")
    paid = _money(values, "paid_amount")
    if paid < 0:
        raise _RejectedRow(
            "unexpected_amount_direction",
            "订单实付金额为负，不能自动改写方向",
            field_name="paid_amount",
        )
    refund = _refund_money(values)
    net = sum_money((paid, negate_money(abs(refund))))
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "order_id",
            "gross_paid_amount": format(paid, "f"),
            "refund_amount": format(abs(refund), "f"),
            "occurred_at_precision": precision,
            "sku": sku or "",
        }
    )
    settlement_batch_id, cash_bridge_key = _keys(values)
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="order",
            source_type="baobei_order",
            side=CanonicalSide.ORDER,
            business_key=order_id,
            occurred_at=occurred_at,
            value=net,
            attributes=attributes,
            metric="net_order_amount",
            sku=sku,
            settlement_batch_id=settlement_batch_id,
            cash_bridge_key=cash_bridge_key,
        ),
    )


def _normalize_two_column_ledger(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    transaction_id = _text(values, "transaction_id") or ""
    merchant_order_id = _text(values, "merchant_order_id", required=False)
    occurred_at, precision = _occurred_at(values, "accounting_date")
    resolved_order_key, wallet_attributes = _wallet_rule_context(
        values,
        merchant_order_id=merchant_order_id,
        occurred_at=occurred_at,
    )
    business_key = resolved_order_key or merchant_order_id or transaction_id
    business_key_kind = (
        "wallet_order_key"
        if resolved_order_key
        else "merchant_order_id"
        if merchant_order_id
        else "transaction_id"
    )
    income = abs(_money(values, "income_amount", blank_is_zero=True))
    expense = abs(_money(values, "expense_amount", blank_is_zero=True))
    net = sum_money((income, negate_money(expense)))
    attributes = _base_attributes(values)
    attributes.update(wallet_attributes)
    attributes.update(
        {
            "business_key_kind": business_key_kind,
            "income_amount": format(income, "f"),
            "expense_amount": format(expense, "f"),
            "occurred_at_precision": precision,
            "transaction_id": transaction_id,
        }
    )
    settlement_batch_id, cash_bridge_key = _keys(values)
    source_type = (
        "alipay_ledger"
        if context.template_id == "alipay_ledger_v1"
        else "wechat_ledger"
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="platform_ledger",
            source_type=source_type,
            side=CanonicalSide.PLATFORM,
            business_key=business_key,
            occurred_at=occurred_at,
            value=net,
            attributes=attributes,
            metric="platform_net_amount",
            settlement_batch_id=settlement_batch_id,
            cash_bridge_key=cash_bridge_key,
        ),
    )


def _normalize_direction_ledger(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    transaction_id = _text(values, "transaction_id") or ""
    merchant_order_id = _text(values, "merchant_order_id", required=False)
    occurred_at, precision = _occurred_at(values, "accounting_date")
    resolved_order_key, wallet_attributes = _wallet_rule_context(
        values,
        merchant_order_id=merchant_order_id,
        occurred_at=occurred_at,
    )
    business_key = resolved_order_key or merchant_order_id or transaction_id
    business_key_kind = (
        "wallet_order_key"
        if resolved_order_key
        else "merchant_order_id"
        if merchant_order_id
        else "transaction_id"
    )
    raw_amount = abs(_money(values, "amount"))
    direction = normalize_header(_text(values, "direction") or "")
    if direction in {"收入", "入账", "收款"}:
        normalized_amount = raw_amount
    elif direction in {"支出", "出账", "付款", "扣款"}:
        normalized_amount = negate_money(raw_amount)
    else:
        raise _RejectedRow(
            "unknown_money_direction",
            "资金方向不在有限白名单内",
            field_name="direction",
        )
    attributes = _base_attributes(values)
    attributes.update(wallet_attributes)
    attributes.update(
        {
            "business_key_kind": business_key_kind,
            "occurred_at_precision": precision,
            "transaction_id": transaction_id,
        }
    )
    settlement_batch_id, cash_bridge_key = _keys(values)
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="platform_ledger",
            source_type="wechat_ledger",
            side=CanonicalSide.PLATFORM,
            business_key=business_key,
            occurred_at=occurred_at,
            value=normalized_amount,
            attributes=attributes,
            metric="platform_net_amount",
            settlement_batch_id=settlement_batch_id,
            cash_bridge_key=cash_bridge_key,
        ),
    )


def _normalize_platform_fee(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    occurred_at, precision = _occurred_at(values, "business_date", "billing_period")
    candidates = (
        ("sub_order_id", _text(values, "sub_order_id", required=False)),
        ("main_order_id", _text(values, "main_order_id", required=False)),
        ("merchant_order_id", _text(values, "merchant_order_id", required=False)),
    )
    selected = next(((kind, key) for kind, key in candidates if key), None)
    if selected is None:
        raise _RejectedRow(
            "missing_business_key",
            "平台账单行没有订单或商户单号，不能猜测关联键",
            field_name="sub_order_id",
        )
    business_key_kind, business_key = selected
    sku = _text(values, "sku", required=False)
    fee = abs(_money(values, "fee_amount"))
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": business_key_kind,
            "occurred_at_precision": precision,
            "sku": sku or "",
        }
    )
    settlement_batch_id, cash_bridge_key = _keys(values)
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="platform_fee",
            source_type="taobao_platform_fee",
            side=CanonicalSide.PLATFORM,
            business_key=business_key or "",
            occurred_at=occurred_at,
            value=negate_money(fee),
            attributes=attributes,
            metric="platform_fee",
            sku=sku,
            settlement_batch_id=settlement_batch_id,
            cash_bridge_key=cash_bridge_key,
        ),
    )


def _normalize_cost(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    order_id = _text(values, "order_id") or ""
    sku = _text(values, "sku") or ""
    try:
        occurred_at, precision = _occurred_at(values, "business_date")
        period_key = None
    except _RejectedRow as exc:
        if exc.code != "missing_occurred_at" or context.explicit_period_key is None:
            raise
        occurred_at = _period_start(context.explicit_period_key)
        precision = "month"
        period_key = context.explicit_period_key
    quantity = _decimal_quantity(values)
    unit_cost = _money(values, "unit_cost")
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        extended_cost = quantity * unit_cost
    try:
        total_cost = amount(extended_cost)
    except ValueError as exc:
        raise _RejectedRow(
            "invalid_money",
            "数量乘单位成本超出 DECIMAL(38,4)",
            field_name="unit_cost",
        ) from exc
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "order_id+sku",
            "occurred_at_precision": precision,
            "quantity": format(quantity, "f"),
            "unit_cost": format(unit_cost, "f"),
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="cost",
            source_type="jushuitan_cost",
            side=CanonicalSide.OPERATIONAL,
            business_key=f"{order_id}|{sku}",
            occurred_at=occurred_at,
            value=negate_money(total_cost),
            attributes=attributes,
            metric="cost",
            sku=sku,
            period_key=period_key,
        ),
    )


def _normalize_advertising(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    occurred_at, precision = _occurred_at(values, "business_date")
    campaign = _text(values, "campaign") or ""
    sku = _text(values, "sku", required=False)
    entity_type = (_text(values, "entity_type", required=False) or "").casefold()
    if entity_type and entity_type not in {"商品", "product"}:
        sku = None
    spend = abs(_money(values, "spend_amount"))
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "business_date+campaign",
            "occurred_at_precision": precision,
            "entity_type": entity_type,
            "sku": sku or "",
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="advertising",
            source_type="advertising_spend",
            side=CanonicalSide.OPERATIONAL,
            business_key=f"{occurred_at.date().isoformat()}|{campaign}",
            occurred_at=occurred_at,
            value=negate_money(spend),
            attributes=attributes,
            metric="advertising",
            sku=sku,
        ),
    )


def _normalize_freight(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    tracking_number = _text(values, "tracking_number") or ""
    order_id = _text(values, "order_id", required=False)
    sku = _text(values, "sku", required=False)
    occurred_at, precision = _occurred_at(values, "business_date")
    freight = abs(_money(values, "freight_amount"))
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "tracking_number",
            "occurred_at_precision": precision,
            "order_id": order_id or "",
            "sku": sku or "",
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="freight",
            source_type="freight_statement",
            side=CanonicalSide.OPERATIONAL,
            business_key=tracking_number,
            occurred_at=occurred_at,
            value=negate_money(freight),
            attributes=attributes,
            metric="freight",
            sku=sku,
        ),
    )


def _normalize_order_freight(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    order_id = _text(values, "order_id") or ""
    occurred_at, precision = _occurred_at(values, "business_date")
    amount = _money(values, "freight_amount")
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "order_id+source_row",
            "occurred_at_precision": precision,
            "order_id": order_id,
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="freight",
            source_type="store_order_freight",
            side=CanonicalSide.OPERATIONAL,
            business_key=f"{order_id}|{row_number}",
            occurred_at=occurred_at,
            value=amount,
            attributes=attributes,
            metric="freight",
        ),
    )


def _normalize_wechat_control(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    occurred_at, precision = _occurred_at(values, "period")
    income = abs(_money(values, "income_amount"))
    expense = abs(_money(values, "expense_amount"))
    net = sum_money((income, negate_money(expense)))
    period_key = _text(values, "period") or ""
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "control_period",
            "income_amount": format(income, "f"),
            "expense_amount": format(expense, "f"),
            "occurred_at_precision": precision,
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="control_total",
            source_type="wechat_control_total",
            side=CanonicalSide.PLATFORM,
            business_key=period_key,
            occurred_at=occurred_at,
            value=net,
            attributes=attributes,
            metric="platform_control_net",
        ),
    )


def _normalize_alipay_control(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    if context.explicit_period_key is None:
        raise _RejectedRow(
            "missing_period_key",
            "支付宝控制汇总内容没有期间字段，路由也没有已确认期间",
            field_name="period_key",
        )
    category = _text(values, "category") or ""
    income = abs(_money(values, "income_amount"))
    expense = abs(_money(values, "expense_amount"))
    declared_net = _money(values, "net_amount")
    calculated_net = sum_money((income, negate_money(expense)))
    if declared_net != calculated_net:
        raise _RejectedRow(
            "control_total_mismatch",
            "控制汇总净额与收入减支出不一致",
            field_name="net_amount",
            details={
                "declared_net": format(declared_net, "f"),
                "calculated_net": format(calculated_net, "f"),
            },
        )
    occurred_at = _period_start(context.explicit_period_key)
    attributes = _base_attributes(values)
    attributes.update(
        {
            "business_key_kind": "control_period+category",
            "income_amount": format(income, "f"),
            "expense_amount": format(expense, "f"),
            "occurred_at_precision": "month",
        }
    )
    return (
        _canonical(
            context,
            row_number,
            dataset_kind="control_total",
            source_type="alipay_control_total",
            side=CanonicalSide.PLATFORM,
            business_key=f"{context.explicit_period_key}|{category}",
            occurred_at=occurred_at,
            value=declared_net,
            period_key=context.explicit_period_key,
            metric="platform_control_net",
            attributes=attributes,
        ),
    )


def _normalize_historical_pnl(
    context: _RouteContext,
    row_number: int,
    values: Mapping[str, _CellValue],
) -> tuple[CanonicalRow, ...]:
    if context.explicit_period_key is None:
        raise _RejectedRow(
            "missing_period_key",
            "历史输出内容没有期间字段，路由也没有已确认期间",
            field_name="period_key",
        )
    sku = _text(values, "sku") or ""
    occurred_at = _period_start(context.explicit_period_key)
    rows: list[CanonicalRow] = []
    for metric in (
        "sales",
        "refund",
        "platform_fee",
        "freight",
        "cost",
        "advertising",
        "profit",
    ):
        if metric not in values:
            continue
        raw = _text(values, metric, required=False)
        if raw is None:
            continue
        metric_value = _money(values, metric)
        attributes = _base_attributes(values)
        attributes.update(
            {
                "business_key_kind": "period+sku+metric",
                "occurred_at_precision": "month",
            }
        )
        rows.append(
            _canonical(
                context,
                row_number,
                dataset_kind="historical_pnl",
                source_type="historical_pnl",
                side=CanonicalSide.OPERATIONAL,
                business_key=f"{context.explicit_period_key}|{sku}|{metric}",
                occurred_at=occurred_at,
                value=metric_value,
                period_key=context.explicit_period_key,
                metric=metric,
                sku=sku,
                attributes=attributes,
            )
        )
    if not rows:
        raise _RejectedRow(
            "missing_metric_value",
            "历史输出行没有可对表的金额指标",
        )
    return tuple(rows)


def _period_start(period_key: str) -> datetime:
    if not re.fullmatch(r"\d{4}", period_key):
        raise ValueError("period_key must use YYMM")
    year = 2000 + int(period_key[:2])
    month = int(period_key[2:])
    if not 1 <= month <= 12:
        raise ValueError("period_key month is invalid")
    return datetime(year, month, 1)


def _issue(
    context: _RouteContext,
    code: str,
    message: str,
    *,
    row_number: int | None = None,
    field_name: str | None = None,
    details: Mapping[str, str] | None = None,
) -> NormalizationIssue:
    return NormalizationIssue(
        code=code,
        message=message,
        source_name=context.source_name,
        template_id=context.template_id,
        evidence_row=row_number,
        field=field_name,
        source_member=context.member,
        source_sheet=context.sheet,
        details=details or {},
    )


_HANDLERS: dict[str, RowHandler] = {
    "pdd_order_v1": _normalize_order,
    "douyin_order_v1": _normalize_order,
    "taobao_order_v1": _normalize_order,
    "alipay_ledger_v1": _normalize_two_column_ledger,
    "wechat_income_expense_v1": _normalize_two_column_ledger,
    "wechat_ledger_v1": _normalize_direction_ledger,
    "taobao_platform_fee_v1": _normalize_platform_fee,
    "jushuitan_cost_v1": _normalize_cost,
    "advertising_spend_v1": _normalize_advertising,
    "store_order_freight_v1": _normalize_order_freight,
    "freight_statement_v1": _normalize_freight,
    "alipay_control_total_v1": _normalize_alipay_control,
    "wechat_control_total_v1": _normalize_wechat_control,
    "historical_pnl_16_v1": _normalize_historical_pnl,
}

_EXPECTED_SOURCE_KINDS = {
    "pdd_order_v1": "order",
    "douyin_order_v1": "order",
    "taobao_order_v1": "order",
    "alipay_ledger_v1": "alipay",
    "wechat_income_expense_v1": "wechat",
    "wechat_ledger_v1": "wechat",
    "taobao_platform_fee_v1": "platform_fee",
    "jushuitan_cost_v1": "cost",
    "advertising_spend_v1": "advertising",
    "store_order_freight_v1": "freight",
    "freight_statement_v1": "freight",
    "alipay_control_total_v1": "alipay_control",
    "wechat_control_total_v1": "wechat_control",
    "historical_pnl_16_v1": "historical_output",
}

__all__ = [
    "CanonicalRow",
    "CanonicalSide",
    "IssueSeverity",
    "NormalizationIssue",
    "NormalizationResult",
    "normalize_bytes",
]
