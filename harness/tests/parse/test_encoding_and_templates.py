from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from commerce_harness.parse import (
    AmbiguousTemplateError,
    NoTemplateMatchError,
    SourceKind,
    TemplateDefinition,
    TemplateRouter,
    detect_csv_encoding,
)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (("子订单编号", "订单创建时间", "买家实际支付金额"), SourceKind.ORDER),
        (
            ("支付宝交易号", "入账时间", "收入金额（+元）", "支出金额（-元）"),
            SourceKind.ALIPAY,
        ),
        (
            ("微信支付业务单号", "记账时间", "收支金额", "收支类型"),
            SourceKind.WECHAT,
        ),
        (("内部订单号", "商品编码", "数量", "成本价"), SourceKind.COST),
        (("数据日期", "消耗", "广告计划"), SourceKind.ADVERTISING),
        (("运单号", "发货日期", "运费"), SourceKind.FREIGHT),
    ],
)
def test_six_finite_csv_templates_route_uniquely(
    headers: tuple[str, ...],
    expected: SourceKind,
) -> None:
    data = (",".join(headers) + "\n" + ",".join("1" for _ in headers) + "\n").encode()
    route = TemplateRouter().route_bytes("synthetic.csv", data)
    assert route.source_kind is expected


def test_gb18030_csv_encoding_is_detected_and_routed() -> None:
    text = "子订单编号,订单创建时间,买家实际支付金额\nA-1,2026-02-01,10.00\n"
    encoded = text.encode("gb18030")
    detection = detect_csv_encoding(encoded)
    route = TemplateRouter().route_bytes("orders.csv", encoded)

    assert detection.encoding == "gb18030"
    assert route.source_kind is SourceKind.ORDER


def test_comment_preamble_uses_header_delimiter_not_tabbed_data_values() -> None:
    text = (
        "#支付宝账务明细查询\n"
        "#账号：[masked]\n"
        "账务流水号,发生时间,收入金额（+元）,支出金额（-元）,业务描述\n"
        "1\t,2026-02-01\t,10.00\t,0.00\t,交易收款\t\n"
    )
    route = TemplateRouter().route_bytes(
        "alipay.csv",
        text.encode("gb18030"),
    )

    assert route.source_kind is SourceKind.ALIPAY
    assert route.location.header_row == 3


def test_fingerprint_depends_on_structure_not_row_values() -> None:
    first = TemplateRouter().route_bytes(
        "orders.csv",
        "子订单编号,订单创建时间,买家实际支付金额\nA-1,2026-02-01,10\n".encode(),
    )
    second = TemplateRouter().route_bytes(
        "orders.csv",
        "子订单编号,订单创建时间,买家实际支付金额\nB-9,2026-03-09,999\n".encode(),
    )
    changed = TemplateRouter().route_bytes(
        "orders.csv",
        "子订单编号,支付时间,买家实际支付金额\nB-9,2026-03-09,999\n".encode(),
    )

    assert first.fingerprint.digest == second.fingerprint.digest
    assert first.fingerprint.digest != changed.fingerprint.digest


def test_no_template_does_not_fall_back_to_first_definition() -> None:
    with pytest.raises(NoTemplateMatchError):
        TemplateRouter().route_bytes("unknown.csv", b"alpha,beta\n1,2\n")


def test_ambiguous_match_is_rejected_instead_of_selecting_first() -> None:
    first = TemplateDefinition(
        template_id="first",
        source_kind=SourceKind.ORDER,
        aliases={"key": frozenset({"编号"})},
        required_groups=(("key",),),
    )
    second = TemplateDefinition(
        template_id="second",
        source_kind=SourceKind.COST,
        aliases={"key": frozenset({"编号"})},
        required_groups=(("key",),),
    )
    router = TemplateRouter((first, second))
    with pytest.raises(AmbiguousTemplateError):
        router.route_bytes("ambiguous.csv", "编号,备注\n1,x\n".encode())


def test_xlsx_scans_visible_sheets_and_detects_header_row(tmp_path: Path) -> None:
    workbook = Workbook()
    hidden = workbook.active
    hidden.title = "说明"
    hidden.sheet_state = "hidden"
    hidden.append(["子订单编号", "订单创建时间", "买家实际支付金额"])
    data = workbook.create_sheet("订单数据")
    data.append(["导出说明", None, None])
    data.append(["子订单编号", "订单创建时间", "买家实际支付金额"])
    data.append(["A-1", "2026-02-01", "10.00"])
    path = tmp_path / "orders.xlsx"
    workbook.save(path)

    route = TemplateRouter().route_path(path)

    assert route.source_kind is SourceKind.ORDER
    assert route.location.sheet_name == "订单数据"
    assert route.location.header_row == 2
