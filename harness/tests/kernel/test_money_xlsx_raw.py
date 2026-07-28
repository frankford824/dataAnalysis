from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest

from commerce_harness.kernel.money import (
    DECIMAL38_4_MAX,
    MoneyPrecisionError,
    negate_money,
    parse_money,
    subtract_money,
    sum_money,
)
from commerce_harness.kernel.xlsx_raw import iter_raw_cells, read_raw_money_column


def _write_minimal_xlsx(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="账单" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
                Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="inlineStr"><is><t>金额</t></is></c></row>
                <row r="2"><c r="A2"><v>123.450000000000017</v></c></row>
                <row r="3"><c r="A3"><f>1+2</f><v>3.0000</v></c></row>
              </sheetData>
            </worksheet>""",
        )


def test_float_is_rejected_and_decimal38_4_boundaries_are_enforced() -> None:
    with pytest.raises(TypeError, match="float"):
        parse_money(0.1)

    assert parse_money(str(DECIMAL38_4_MAX)).amount == DECIMAL38_4_MAX
    assert parse_money(f"-{DECIMAL38_4_MAX}").amount == Decimal(
        "-9999999999999999999999999999999999.9999"
    )
    assert negate_money(DECIMAL38_4_MAX) == Decimal(
        "-9999999999999999999999999999999999.9999"
    )
    assert subtract_money(DECIMAL38_4_MAX, DECIMAL38_4_MAX) == Decimal("0.0000")
    assert sum_money((DECIMAL38_4_MAX, negate_money(DECIMAL38_4_MAX))) == Decimal(
        "0.0000"
    )
    with pytest.raises(MoneyPrecisionError, match="range"):
        parse_money("10000000000000000000000000000000000.0000")


def test_money_records_source_precision_and_explicit_rounding_strategy() -> None:
    exact = parse_money("1,234.500")
    rounded = parse_money("(1.23456)")

    assert exact.amount == Decimal("1234.5000")
    assert exact.source_scale == 3
    assert exact.strategy == "text:exact_scale"
    assert rounded.amount == Decimal("-1.2346")
    assert rounded.source_scale == 5
    assert "quantize" in rounded.strategy
    with pytest.raises(MoneyPrecisionError, match="source scale"):
        parse_money("1.00001", require_exact_scale=True)


def test_xlsx_reader_returns_original_xml_text_without_float_round_trip(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "raw-money.xlsx"
    _write_minimal_xlsx(workbook)

    cells = list(iter_raw_cells(workbook, sheet="账单", columns={"A"}))
    assert cells[1].raw_text == "123.450000000000017"
    assert cells[2].formula == "1+2"
    assert cells[2].raw_text == "3.0000"

    money_cells = read_raw_money_column(workbook, "A", sheet="账单", start_row=2)
    assert money_cells[0][0].reference == "A2"
    assert money_cells[0][1].source_text == "123.450000000000017"
    assert money_cells[0][1].amount == Decimal("123.4500")
    assert money_cells[1][1].amount == Decimal("3.0000")
