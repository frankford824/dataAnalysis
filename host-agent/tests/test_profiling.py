
import openpyxl
import pytest

from finance_agent.profiling import (
    UnsupportedProfileError,
    profile_file,
    schema_fingerprint,
)


def test_csv_profile_classifies_orders_and_fingerprint_is_stable(tmp_path):
    path = tmp_path / "orders.csv"
    path.write_text(
        "订单号,业务日期,销售额,退款金额\n"
        "A-1,2026-07-01,100.00,0.00\n"
        "A-2,2026-07-02,80.00,10.00\n",
        encoding="utf-8-sig",
    )
    profile = profile_file(path)
    assert profile.classification == "orders"
    assert profile.classification_confidence == "high"
    assert profile.row_count_sampled == 2
    assert len(profile.fingerprint) == 64
    assert profile.columns[0].sample_values[0].startswith("<text:length=")
    assert "A-1" not in profile.columns[0].sample_values[0]
    assert profile.fingerprint == schema_fingerprint(
        ["订单号", "业务日期", "销售额", "退款金额"],
        [item.inferred_type for item in profile.columns],
    )


def test_xlsx_profile_detects_header_and_visible_sheet(tmp_path):
    path = tmp_path / "fees.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "平台费用"
    sheet.append(["平台费用月报"])
    sheet.append(["业务日期", "广告费"])
    sheet.append(["2026-07-01", "12.34"])
    workbook.save(path)

    profile = profile_file(path, purpose_hint="advertising")
    assert profile.sheet == "平台费用"
    assert profile.header_row == 2
    assert profile.classification == "advertising"
    assert profile.row_count_sampled == 1


def test_pbix_and_xls_are_metadata_only_or_manual(tmp_path):
    pbix = tmp_path / "model.pbix"
    pbix.write_bytes(b"not parsed")
    with pytest.raises(UnsupportedProfileError, match="只登记元数据"):
        profile_file(pbix)
    xls = tmp_path / "old.xls"
    xls.write_bytes(b"old")
    with pytest.raises(UnsupportedProfileError, match="另存为"):
        profile_file(xls)
