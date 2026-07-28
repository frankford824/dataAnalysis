from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from commerce_harness.evidence_viewer import (
    EvidencePreviewError,
    EvidencePreviewNotFoundError,
    EvidencePreviewSecurityError,
    EvidenceViewer,
)
from commerce_harness.snapshot import BytesReader, SnapshotStore


def _capture(tmp_path: Path, content: bytes) -> tuple[EvidenceViewer, str]:
    store = SnapshotStore(tmp_path / "snapshots")
    manifest = store.capture(BytesReader(content))
    return EvidenceViewer(store), manifest.content_sha256


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["导出说明"])
    sheet.append(["订单号", "店铺", "金额"])
    sheet.append(["A-1", "一店", 10.25])
    sheet.append(["A-2", "二店", 20])
    sheet.append(["A-3", "三店", "=SUM(C3:C4)"])
    hidden = workbook.create_sheet("隐藏")
    hidden.sheet_state = "hidden"
    hidden.append(["编号", "值"])
    hidden.append(["H-1", 1])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_csv_window_distinguishes_source_and_data_rows(tmp_path: Path) -> None:
    content = (
        "导出说明\n"
        "订单号,店铺,金额\n"
        "A-1,一店,10.25\n"
        "A-2,二店,20.00\n"
        "A-3,三店,30.75\n"
        "A-4,四店,40.00\n"
    ).encode()
    viewer, digest = _capture(tmp_path, content)

    workbook = viewer.preview(
        digest,
        target_row_number=4,
        window_radius=1,
        target_column="金额",
    )

    window = workbook.sheet.window
    assert workbook.file_kind == "csv"
    assert workbook.read_only is True
    assert window.header_row_number == 2
    assert window.target_row_number == 4
    assert window.target_data_row_number == 2
    assert window.start_row_number == 3
    assert window.end_row_number == 5
    assert [row.source_row_number for row in window.rows] == [3, 4, 5]
    assert [row.data_row_number for row in window.rows] == [1, 2, 3]
    assert window.target_column_index == 2
    assert window.rows[1].cells[2].value == "20.00"
    assert workbook.to_dict()["sheet"]["window"]["targetRowNumber"] == 4


def test_csv_multiline_record_maps_physical_source_line_to_data_row(tmp_path: Path) -> None:
    content = '订单号,备注\nA-1,"第一行\n第二行"\nA-2,普通\n'.encode()
    viewer, digest = _capture(tmp_path, content)

    workbook = viewer.preview(digest, target_row_number=3, window_radius=0)

    row = workbook.sheet.window.rows[0]
    assert row.source_row_number == 2
    assert row.source_end_row_number == 3
    assert row.data_row_number == 1
    assert row.cells[1].value == "第一行\n第二行"

    next_record = viewer.preview(digest, target_row_number=4, window_radius=0)
    assert next_record.sheet.window.rows[0].data_row_number == 2


def test_xlsx_window_keeps_formula_as_non_deterministic_metadata(tmp_path: Path) -> None:
    viewer, digest = _capture(tmp_path, _xlsx_bytes())

    workbook = viewer.preview(
        digest,
        sheet_name="订单",
        target_row_number=5,
        window_radius=1,
        target_column="金额",
    )

    window = workbook.sheet.window
    assert workbook.file_kind == "xlsx"
    assert workbook.sheet_names == ("订单", "隐藏")
    assert window.header_row_number == 2
    assert window.target_data_row_number == 3
    assert window.target_column_index == 2
    formula = window.rows[-1].cells[2]
    assert formula.value is None
    assert formula.value_kind == "formula"
    assert formula.formula == "=SUM(C3:C4)"
    assert formula.deterministic is False
    assert workbook.formulas_are_deterministic is False


def test_zip_can_select_csv_or_xlsx_member_without_extracting_paths(tmp_path: Path) -> None:
    archive = _zip_bytes(
        {
            "safe/orders.csv": b"id,amount\nA-1,12.34\n",
            "safe/fees.xlsx": _xlsx_bytes(),
        }
    )
    viewer, digest = _capture(tmp_path, archive)

    csv_workbook = viewer.preview(
        digest,
        member_name="safe/orders.csv",
        target_row_number=2,
        window_radius=0,
    )
    xlsx_workbook = viewer.preview(
        digest,
        member_name="safe/fees.xlsx",
        sheet_name="订单",
        target_row_number=3,
        window_radius=0,
    )

    assert csv_workbook.member_name == "safe/orders.csv"
    assert csv_workbook.sheet.window.rows[0].cells[1].value == "12.34"
    assert xlsx_workbook.member_name == "safe/fees.xlsx"
    assert xlsx_workbook.sheet.window.rows[0].cells[0].value == "A-1"


def test_zip_rejects_slip_and_requires_member_when_ambiguous(tmp_path: Path) -> None:
    unsafe_viewer, unsafe_digest = _capture(
        tmp_path / "unsafe",
        _zip_bytes({"../escape.csv": b"id,value\n1,2\n"}),
    )
    with pytest.raises(EvidencePreviewSecurityError):
        unsafe_viewer.preview(unsafe_digest, target_row_number=2)

    ambiguous_viewer, ambiguous_digest = _capture(
        tmp_path / "ambiguous",
        _zip_bytes(
            {
                "one.csv": b"id,value\n1,2\n",
                "two.csv": b"id,value\n3,4\n",
            }
        ),
    )
    with pytest.raises(EvidencePreviewError, match="explicit member_name"):
        ambiguous_viewer.preview(ambiguous_digest, target_row_number=2)


def test_invalid_sheet_row_column_digest_and_limits_are_rejected(tmp_path: Path) -> None:
    viewer, digest = _capture(tmp_path, _xlsx_bytes())

    with pytest.raises(EvidencePreviewNotFoundError, match="worksheet"):
        viewer.preview(digest, target_row_number=3)
    with pytest.raises(EvidencePreviewNotFoundError, match="sheet"):
        viewer.preview(digest, sheet_name="不存在", target_row_number=3)
    with pytest.raises(EvidencePreviewError, match="after the header"):
        viewer.preview(digest, sheet_name="订单", target_row_number=2, header_row_number=2)
    with pytest.raises(EvidencePreviewNotFoundError, match="row"):
        viewer.preview(digest, sheet_name="订单", target_row_number=100)
    with pytest.raises(EvidencePreviewNotFoundError, match="column"):
        viewer.preview(
            digest,
            sheet_name="订单",
            target_row_number=3,
            target_column="不存在",
        )
    with pytest.raises(EvidencePreviewError, match="window_radius"):
        viewer.preview(digest, sheet_name="订单", target_row_number=3, window_radius=201)
    with pytest.raises(EvidencePreviewError, match="max_columns"):
        viewer.preview(digest, sheet_name="订单", target_row_number=3, max_columns=257)
    with pytest.raises(EvidencePreviewNotFoundError, match="snapshot"):
        viewer.preview("A" * 64, target_row_number=2)


def test_window_never_exceeds_server_side_row_or_column_limits(tmp_path: Path) -> None:
    rows = [["id", *[f"c{index}" for index in range(1, 20)]]]
    rows.extend(
        [[str(row), *[f"{row}-{column}" for column in range(1, 20)]] for row in range(1, 30)]
    )
    content = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    viewer, digest = _capture(tmp_path, content)

    workbook = viewer.preview(
        digest,
        target_row_number=15,
        window_radius=3,
        max_columns=5,
    )

    assert len(workbook.sheet.window.rows) == 7
    assert len(workbook.sheet.window.columns) == 5
    assert all(len(row.cells) == 5 for row in workbook.sheet.window.rows)
    assert workbook.content_sha256 == hashlib.sha256(content).hexdigest()
