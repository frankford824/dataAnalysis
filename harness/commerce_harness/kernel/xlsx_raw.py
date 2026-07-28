"""Read original XLSX cell text directly from worksheet XML.

This module intentionally does not use openpyxl/fastexcel for monetary cells:
their public value APIs may expose an IEEE-754 float before Decimal sees it.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .money import MoneyValue, parse_money

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF = re.compile(r"^([A-Z]+)([1-9]\d*)$")


class XlsxRawError(ValueError):
    """Raised for malformed workbooks or unsupported cell values."""


@dataclass(frozen=True, slots=True)
class RawCell:
    reference: str
    row: int
    column: str
    cell_type: str
    raw_text: str | None
    formula: str | None = None

    def as_money(self, *, require_exact_scale: bool = False) -> MoneyValue:
        if self.cell_type not in {"n", ""}:
            raise XlsxRawError(
                f"cell {self.reference} is {self.cell_type!r}, not numeric"
            )
        if self.raw_text is None:
            raise XlsxRawError(f"cell {self.reference} has no cached numeric value")
        return parse_money(self.raw_text, require_exact_scale=require_exact_scale)


def _cell_parts(reference: str) -> tuple[str, int]:
    match = _CELL_REF.fullmatch(reference)
    if not match:
        raise XlsxRawError(f"invalid cell reference: {reference!r}")
    return match.group(1), int(match.group(2))


def _shared_strings(archive: ZipFile) -> tuple[str, ...]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return ()
    root = ET.fromstring(payload)
    result: list[str] = []
    for item in root.findall(f"{{{_MAIN_NS}}}si"):
        result.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))
    return tuple(result)


def _sheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    workbook_path = "xl/workbook.xml"
    workbook = ET.fromstring(archive.read(workbook_path))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }

    result: list[tuple[str, str]] = []
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise XlsxRawError("workbook contains no sheets")
    for sheet in sheets:
        relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
        if relationship_id not in targets:
            raise XlsxRawError(f"sheet {sheet.attrib.get('name')!r} has no relationship")
        target = targets[relationship_id]
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = posixpath.normpath(posixpath.join("xl", target))
        if path.startswith("../") or path.startswith("/"):
            raise XlsxRawError("worksheet relationship escapes workbook archive")
        result.append((sheet.attrib["name"], path))
    return result


def iter_raw_cells(
    workbook: str | Path,
    *,
    sheet: str | int = 0,
    columns: set[str] | None = None,
) -> Iterator[RawCell]:
    """Yield cells with worksheet XML's exact ``<v>`` text.

    ``sheet`` accepts a zero-based index or exact sheet name. ``columns`` uses
    Excel column letters and can reduce the emitted cells without changing how
    the source XML is interpreted.
    """

    normalized_columns = {column.upper() for column in columns} if columns else None
    try:
        with ZipFile(Path(workbook), "r") as archive:
            sheets = _sheet_paths(archive)
            if isinstance(sheet, int):
                try:
                    _, sheet_path = sheets[sheet]
                except IndexError as exc:
                    raise XlsxRawError(f"sheet index out of range: {sheet}") from exc
            else:
                matches = [path for name, path in sheets if name == sheet]
                if not matches:
                    raise XlsxRawError(f"unknown sheet: {sheet!r}")
                sheet_path = matches[0]

            shared = _shared_strings(archive)
            with archive.open(sheet_path) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag != f"{{{_MAIN_NS}}}c":
                        continue
                    reference = element.attrib.get("r")
                    if not reference:
                        element.clear()
                        continue
                    column, row = _cell_parts(reference)
                    if normalized_columns is not None and column not in normalized_columns:
                        element.clear()
                        continue

                    cell_type = element.attrib.get("t", "")
                    value_node = element.find(f"{{{_MAIN_NS}}}v")
                    formula_node = element.find(f"{{{_MAIN_NS}}}f")
                    raw_text = value_node.text if value_node is not None else None
                    if cell_type == "s" and raw_text is not None:
                        try:
                            raw_text = shared[int(raw_text)]
                        except (IndexError, ValueError) as exc:
                            raise XlsxRawError(
                                f"invalid shared string index in {reference}"
                            ) from exc
                    elif cell_type == "inlineStr":
                        inline = element.find(f"{{{_MAIN_NS}}}is")
                        raw_text = (
                            "".join(
                                node.text or ""
                                for node in inline.iter(f"{{{_MAIN_NS}}}t")
                            )
                            if inline is not None
                            else ""
                        )
                    yield RawCell(
                        reference=reference,
                        row=row,
                        column=column,
                        cell_type=cell_type,
                        raw_text=raw_text,
                        formula=formula_node.text if formula_node is not None else None,
                    )
                    element.clear()
    except BadZipFile as exc:
        raise XlsxRawError("file is not a valid XLSX archive") from exc


def read_raw_money_column(
    workbook: str | Path,
    column: str,
    *,
    sheet: str | int = 0,
    start_row: int = 1,
) -> tuple[tuple[RawCell, MoneyValue], ...]:
    """Read and parse one numeric column without a binary-float intermediary."""

    result: list[tuple[RawCell, MoneyValue]] = []
    for cell in iter_raw_cells(workbook, sheet=sheet, columns={column}):
        if cell.row >= start_row and cell.raw_text not in {None, ""}:
            result.append((cell, cell.as_money()))
    return tuple(result)
