from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

from commerce_harness.normalization import (
    CanonicalRow,
    CanonicalSide,
    IssueSeverity,
    NormalizationIssue,
    NormalizationResult,
    _base_attributes,
    _CellValue,
    normalize_bytes,
)
from commerce_harness.parse import TemplateRouter
from commerce_harness.profiling import _route_payload


def _payload(name: str, content: bytes) -> dict:
    route = TemplateRouter().route_bytes(name, content)
    return _route_payload(route)[3]


def _xlsx_bytes(headers: list[str], rows: list[list[object]], *, sheet: str = "数据") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_public_result_types_and_caller_attached_snapshot_id() -> None:
    content = (
        "子订单编号,订单创建时间,买家实付金额,退款金额\n"
        "O-1,2026-02-01 12:30:00,10.10,无退款申请\n"
    ).encode()

    result = normalize_bytes("orders.csv", content, _payload("orders.csv", content))

    assert isinstance(result, NormalizationResult)
    assert isinstance(result.rows[0], CanonicalRow)
    assert result.issues == ()
    assert result.rows[0].source_snapshot_id is None
    attached = replace(result.rows[0], source_snapshot_id="snapshot-001")
    assert attached.source_snapshot_id == "snapshot-001"
    assert attached.amount == Decimal("10.1000")
    assert attached.period_key == "2602"
    assert attached.metric == "net_order_amount"
    assert attached.sku is None


def test_order_xlsx_uses_raw_decimal_text_and_net_refund_semantics() -> None:
    content = _xlsx_bytes(
        ["子订单编号", "订单创建时间", "买家实付金额", "退款金额"],
        [
            ["O-1", "2026-02-01 08:00:00", 0.1, "无退款申请"],
            ["O-2", "2026-02-02 09:30:00", 20.25, 1.05],
        ],
        sheet="export",
    )

    result = normalize_bytes("orders.xlsx", content, _payload("orders.xlsx", content))

    assert result.issues == ()
    assert [row.amount for row in result.rows] == [
        Decimal("0.1000"),
        Decimal("19.2000"),
    ]
    assert [row.business_key for row in result.rows] == ["O-1", "O-2"]
    assert result.rows[0].side is CanonicalSide.ORDER
    assert result.rows[0].source_type == "baobei_order"
    assert result.rows[0].source_sheet == "export"
    assert result.rows[0].evidence_row == 2
    assert result.rows[1].attributes["refund_amount"] == "1.0500"


@pytest.mark.parametrize(
    ("name", "content", "expected_key", "expected_sku", "expected_amount"),
    [
        (
            "pdd-orders.csv",
            (
                "订单号,订单成交时间,商家实收金额(元),商品id,"
                "商家编码-商品维度\n"
                "PDD-1,2026-03-31 21:48:01,24.79,919453948069,X-456\n"
            ).encode(),
            "PDD-1",
            "919453948069",
            Decimal("24.7900"),
        ),
        (
            "douyin-orders.csv",
            (
                "主订单编号,子订单编号,商品ID,支付完成时间,子订单收入\n"
                "MAIN-1,SUB-1,DOU-998,2026-04-30 23:58:41,25.90\n"
            ).encode(),
            "SUB-1",
            "DOU-998",
            Decimal("25.9000"),
        ),
    ],
)
def test_platform_order_templates_preserve_platform_product_identity(
    name: str,
    content: bytes,
    expected_key: str,
    expected_sku: str,
    expected_amount: Decimal,
) -> None:
    result = normalize_bytes(name, content, _payload(name, content))

    assert result.issues == ()
    assert len(result.rows) == 1
    assert result.rows[0].business_key == expected_key
    assert result.rows[0].sku == expected_sku
    assert result.rows[0].amount == expected_amount


def test_advertising_subject_id_is_sku_only_for_product_rows() -> None:
    content = (
        "日期,主体ID,主体类型,主体名称,花费\n"
        "2026-03-31,1038581156034,商品,商品A,12.34\n"
        "2026-03-31,PLAN-1,计划,计划A,5.00\n"
    ).encode()

    result = normalize_bytes("advertising.csv", content, _payload("advertising.csv", content))

    assert result.issues == ()
    assert [row.sku for row in result.rows] == ["1038581156034", None]
    assert [row.amount for row in result.rows] == [
        Decimal("-12.3400"),
        Decimal("-5.0000"),
    ]


def test_order_freight_keeps_expense_and_reversal_direction() -> None:
    content = _xlsx_bytes(
        ["发货日期", "金额", "订单号", "店铺名称"],
        [
            ["2026-02-05", "-22.00", "PDD-1", "测试店铺"],
            ["2026-02-06", "3.50", "PDD-1", "测试店铺"],
        ],
        sheet="发货运费",
    )

    result = normalize_bytes("26年发货运费.xlsx", content, _payload("26年发货运费.xlsx", content))

    assert result.issues == ()
    assert [row.amount for row in result.rows] == [
        Decimal("-22.0000"),
        Decimal("3.5000"),
    ]
    assert [row.attributes["order_id"] for row in result.rows] == ["PDD-1", "PDD-1"]
    assert [row.metric for row in result.rows] == ["freight", "freight"]


def test_alipay_ledger_keeps_explicit_key_but_never_invents_cash_bridge() -> None:
    content = (
        "#支付宝账务明细\n"
        "#账号：[masked]\n"
        "账务流水号,发生时间,收入金额（+元）,"
        "支出金额（-元）,业务描述,商户订单号\n"
        "TX-1\t,2026-02-01 01:02:03\t,25.60\t,-1.00\t,交易收款\t,ORDER-1\t\n"
    ).encode("gb18030")

    result = normalize_bytes("alipay.csv", content, _payload("alipay.csv", content))

    assert result.issues == ()
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.amount == Decimal("24.6000")
    assert row.business_key == "ORDER-1"
    assert row.attributes["business_key_kind"] == "merchant_order_id"
    assert row.settlement_batch_id is None
    assert row.cash_bridge_key is None
    assert row.side is CanonicalSide.PLATFORM
    assert row.evidence_row == 4


def test_wallet_rules_normalize_t200p_key_and_keep_rule_evidence() -> None:
    content = (
        "账务流水号,发生时间,收入金额（+元）,"
        "支出金额（-元）,业务描述,商户订单号\n"
        "TX-1,2026-02-01 01:02:03,25.60,0.00,"
        "订单完成打款,T200P1234567890123456789\n"
    ).encode("gb18030")

    result = normalize_bytes("alipay.csv", content, _payload("alipay.csv", content))

    assert result.issues == ()
    row = result.rows[0]
    assert row.business_key == "1234567890123456789"
    assert row.attributes["business_key_kind"] == "wallet_order_key"
    assert row.attributes["wallet_order_key_rule_id"] == "wallet.order_key.t200p"
    assert row.attributes["wallet_classification_rule_id"] == (
        "wallet.classify.order_payment"
    )
    assert len(row.attributes["wallet_ruleset_checksum"]) == 64


def test_wallet_formula_description_is_ignored_without_dropping_money_row() -> None:
    content = _xlsx_bytes(
        [
            "支付流水号",
            "入帐日期",
            "收入金额（+元）",
            "支出金额（-元）",
            "业务描述",
            "业务基础订单号",
        ],
        [
            [
                "WX-1",
                "2026-02-01 01:02:03",
                "10.00",
                "0.00",
                "=IFS(A2=\"WX-1\",\"订单完成打款\")",
                "1234567890123456789",
            ]
        ],
    )

    result = normalize_bytes("wechat.xlsx", content, _payload("wechat.xlsx", content))

    assert result.issues == ()
    assert result.rows[0].amount == Decimal("10.0000")
    assert result.rows[0].business_key == "1234567890123456789"
    assert (
        result.rows[0].attributes["wallet_business_description_formula_ignored"]
        == "true"
    )
    assert result.rows[0].attributes["wallet_classification_status"] == "unmatched"


def test_archive_uses_each_matched_member_and_reports_unmatched_member() -> None:
    ledger = (
        "账务流水号,发生时间,收入金额（+元）,"
        "支出金额（-元）,业务描述,商户订单号\n"
        "TX-1,2026-02-01 01:02:03,10.00,0.00,交易收款,ORDER-1\n"
    ).encode("gb18030")
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("ledger.csv", ledger)
        archive.writestr("notes.txt", "not tabular")
    content = output.getvalue()

    result = normalize_bytes("alipay.zip", content, _payload("alipay.zip", content))

    assert len(result.rows) == 1
    assert result.rows[0].source_member == "ledger.csv"
    assert result.rows[0].amount == Decimal("10.0000")
    assert [(issue.code, issue.severity) for issue in result.issues] == [
        ("archive_member_unmatched", IssueSeverity.WARNING)
    ]


def test_platform_fee_requires_explicit_order_key_and_uses_period_fallback() -> None:
    content = (
        "账期,业务小类,扣费金额,交易子订单号\n"
        "202602,软件服务费,1.25,SUB-1\n"
        "202602,软件服务费,2.00,\n"
    ).encode()

    result = normalize_bytes("fees.csv", content, _payload("fees.csv", content))

    assert len(result.rows) == 1
    assert result.rows[0].business_key == "SUB-1"
    assert result.rows[0].amount == Decimal("-1.2500")
    assert result.rows[0].occurred_at.isoformat() == "2026-02-01T00:00:00"
    assert result.rows[0].attributes["occurred_at_precision"] == "month"
    assert [(issue.code, issue.evidence_row) for issue in result.issues] == [
        ("missing_business_key", 3)
    ]


def test_cost_multiplies_exact_decimals_and_rejects_missing_period() -> None:
    content = _xlsx_bytes(
        ["内部订单号", "商品编码", "数量", "成本价", "订单日期"],
        [
            ["O-1", "SKU-1", 3, 0.2621, "2026-02-03"],
            ["O-2", "SKU-2", 1, 9.99, None],
        ],
    )

    result = normalize_bytes("cost.xlsx", content, _payload("cost.xlsx", content))

    assert len(result.rows) == 1
    assert result.rows[0].business_key == "O-1|SKU-1"
    assert result.rows[0].amount == Decimal("-0.7863")
    assert result.rows[0].attributes["unit_cost"] == "0.2621"
    assert result.rows[0].metric == "cost"
    assert result.rows[0].sku == "SKU-1"
    assert [(issue.code, issue.field, issue.evidence_row) for issue in result.issues] == [
        ("missing_occurred_at", "business_date", 3)
    ]


def test_cost_without_row_date_accepts_only_explicit_confirmed_period() -> None:
    content = _xlsx_bytes(
        ["内部订单号", "商品编码", "数量", "成本价"],
        [["O-1", "SKU-1", 2, 1.2345]],
    )
    payload = _payload("cost.xlsx", content)
    rejected = normalize_bytes("cost.xlsx", content, payload)
    confirmed_payload = dict(payload)
    confirmed_payload["period_key"] = "2602"
    confirmed = normalize_bytes("cost.xlsx", content, confirmed_payload)

    assert rejected.rows == ()
    assert rejected.issues[0].code == "missing_occurred_at"
    assert confirmed.issues == ()
    assert confirmed.rows[0].amount == Decimal("-2.4690")
    assert confirmed.rows[0].period_key == "2602"
    assert confirmed.rows[0].occurred_at.isoformat() == "2026-02-01T00:00:00"


@pytest.mark.parametrize(
    ("name", "content", "source_type", "business_key", "expected_amount"),
    [
        (
            "advertising.csv",
            "日期,花费,计划名称\n2026-02-04,3.21,计划A\n".encode(),
            "advertising_spend",
            "2026-02-04|计划A",
            Decimal("-3.2100"),
        ),
        (
            "freight.csv",
            "运单号,发货日期,运费\nTRACK-1,2026-02-05,8.80\n".encode(),
            "freight_statement",
            "TRACK-1",
            Decimal("-8.8000"),
        ),
    ],
)
def test_operational_sources_have_explicit_keys_and_negative_expense_sign(
    name: str,
    content: bytes,
    source_type: str,
    business_key: str,
    expected_amount: Decimal,
) -> None:
    result = normalize_bytes(name, content, _payload(name, content))

    assert result.issues == ()
    assert result.rows == (
        replace(
            result.rows[0],
            source_snapshot_id=None,
        ),
    )
    row = result.rows[0]
    assert row.source_type == source_type
    assert row.business_key == business_key
    assert row.amount == expected_amount
    assert row.side is CanonicalSide.OPERATIONAL


def test_wechat_direction_is_finite_and_unknown_value_is_rejected() -> None:
    content = (
        "微信支付业务单号,记账时间,收支金额,收支类型,商户单号\n"
        "WX-1,2026-02-06 10:00:00,12.30,收入,ORDER-1\n"
        "WX-2,2026-02-06 11:00:00,1.20,支出,ORDER-2\n"
        "WX-3,2026-02-06 12:00:00,9.99,冻结,ORDER-3\n"
    ).encode()

    result = normalize_bytes("wechat.csv", content, _payload("wechat.csv", content))

    assert [row.amount for row in result.rows] == [
        Decimal("12.3000"),
        Decimal("-1.2000"),
    ]
    assert all(row.cash_bridge_key is None for row in result.rows)
    assert [(issue.code, issue.field, issue.evidence_row) for issue in result.issues] == [
        ("unknown_money_direction", "direction", 4)
    ]


def test_formula_and_undated_control_are_structured_rejections() -> None:
    formula_content = _xlsx_bytes(
        ["子订单编号", "订单创建时间", "买家实付金额"],
        [["O-1", "2026-02-01", "=1+2"]],
    )
    formula_result = normalize_bytes(
        "formula.xlsx",
        formula_content,
        _payload("formula.xlsx", formula_content),
    )
    control_content = (
        "类型,收入金额（+元）,支出金额（-元）,总金额（元）\n"
        "交易,10.00,-1.00,9.00\n"
    ).encode()
    control_result = normalize_bytes(
        "control.csv",
        control_content,
        _payload("control.csv", control_content),
    )

    assert formula_result.rows == ()
    assert isinstance(formula_result.issues[0], NormalizationIssue)
    assert formula_result.issues[0].code == "formula_not_allowed"
    assert control_result.rows == ()
    assert control_result.issues[0].code == "missing_period_key"


def test_cached_formula_output_is_not_retained_as_source_attribute() -> None:
    attributes = _base_attributes(
        {
            "business_description": _CellValue(
                text="缓存分类结果",
                formula='IFS(A2="x","分类A",TRUE,"其他")',
            ),
            "literal_note": _CellValue(text="原始备注"),
        }
    )

    assert attributes == {"literal_note": "原始备注"}


def test_historical_output_requires_confirmed_period_and_emits_metric_rows() -> None:
    content = (
        "宝贝编码,交易收款,交易退款,店铺利润\n"
        "SKU-1,10.00,1.00,2.00\n"
    ).encode()
    payload = _payload("historical.csv", content)
    unsupported = normalize_bytes("historical.csv", content, payload)
    confirmed_payload = dict(payload)
    confirmed_payload["period_key"] = "2602"
    confirmed = normalize_bytes("historical.csv", content, confirmed_payload)
    mismatched_payload = dict(payload)
    mismatched_payload["source_kind"] = "order"
    mismatched = normalize_bytes("historical.csv", content, mismatched_payload)

    assert unsupported.rows == ()
    assert unsupported.issues[0].code == "missing_period_key"
    assert confirmed.issues == ()
    assert [(row.metric, row.amount) for row in confirmed.rows] == [
        ("sales", Decimal("10.0000")),
        ("refund", Decimal("1.0000")),
        ("profit", Decimal("2.0000")),
    ]
    assert all(row.period_key == "2602" for row in confirmed.rows)
    assert all(row.sku == "SKU-1" for row in confirmed.rows)
    assert mismatched.rows == ()
    assert mismatched.issues[0].code == "route_source_kind_mismatch"


def test_control_period_must_come_from_content_and_cross_month_is_rejected() -> None:
    content = (
        "日期,明细笔数,收入金额（元）,收入笔数,"
        "支出金额（元）,支出笔数,期末余额（元）\n"
        "2026-01-31至2026-02-01,2,10.00,1,1.00,1,9.00\n"
    ).encode()

    result = normalize_bytes("wechat-control.csv", content, _payload("wechat-control.csv", content))

    assert result.rows == ()
    assert result.issues[0].code == "cross_month_period"


def test_alipay_control_uses_only_confirmed_period_and_checks_its_own_total() -> None:
    content = (
        "类型,收入金额（+元）,支出金额（-元）,总金额（元）\n"
        "交易,10.00,-1.00,9.00\n"
        "错误汇总,10.00,-1.00,8.99\n"
    ).encode()
    payload = _payload("control.csv", content)
    payload["period_key"] = "2602"

    result = normalize_bytes("control.csv", content, payload)

    assert len(result.rows) == 1
    assert result.rows[0].period_key == "2602"
    assert result.rows[0].metric == "platform_control_net"
    assert result.rows[0].amount == Decimal("9.0000")
    assert [(issue.code, issue.evidence_row) for issue in result.issues] == [
        ("control_total_mismatch", 3)
    ]


def test_wechat_control_period_and_net_come_from_content() -> None:
    content = (
        "日期,明细笔数,收入金额（元）,收入笔数,"
        "支出金额（元）,支出笔数,期末余额（元）\n"
        "202602,2,10.00,1,1.00,1,9.00\n"
    ).encode()

    result = normalize_bytes("wechat-control.csv", content, _payload("wechat-control.csv", content))

    assert result.issues == ()
    assert len(result.rows) == 1
    assert result.rows[0].period_key == "2602"
    assert result.rows[0].occurred_at.isoformat() == "2026-02-01T00:00:00"
    assert result.rows[0].amount == Decimal("9.0000")


def test_invalid_route_and_header_only_source_are_structured_issues() -> None:
    invalid = normalize_bytes("orders.csv", b"x,y\n1,2\n", {"kind": "file"})
    content = "子订单编号,订单创建时间,买家实付金额\n".encode()
    empty = normalize_bytes("orders.csv", content, _payload("orders.csv", content))
    unsupported_payload = _payload("orders.csv", content)
    unsupported_payload["template_id"] = "future_template_v1"
    unsupported = normalize_bytes("orders.csv", content, unsupported_payload)

    assert invalid.issues[0].code == "invalid_route_or_source"
    assert empty.issues[0].code == "no_data_rows"
    assert unsupported.issues[0].code == "unsupported_template"


def test_public_boundary_rejects_invalid_types_and_invalid_evidence_models() -> None:
    valid_row = CanonicalRow(
        dataset_kind="order",
        source_type="baobei_order",
        side=CanonicalSide.ORDER,
        business_key="O-1",
        cash_bridge_key=None,
        occurred_at=datetime(2026, 2, 1),
        amount=Decimal("1.0000"),
        period_key="2602",
        evidence_row=2,
        source_name="orders.csv",
    )

    with pytest.raises(ValueError, match="dataset_kind"):
        replace(valid_row, dataset_kind="")
    with pytest.raises(ValueError, match="YYMM"):
        replace(valid_row, period_key="202602")
    with pytest.raises(ValueError, match="positive"):
        replace(valid_row, evidence_row=0)
    with pytest.raises(ValueError, match="code"):
        NormalizationIssue(code="", message="bad", source_name="orders.csv")
    with pytest.raises(ValueError, match="positive"):
        NormalizationIssue(
            code="bad",
            message="bad",
            source_name="orders.csv",
            evidence_row=0,
        )
    with pytest.raises(ValueError, match="name"):
        normalize_bytes("", b"", {})
    with pytest.raises(TypeError, match="content"):
        normalize_bytes("orders.csv", "not-bytes", {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="route_payload"):
        normalize_bytes("orders.csv", b"", [])  # type: ignore[arg-type]
