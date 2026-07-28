from __future__ import annotations

import json

import pytest

from commerce_harness.delivery.models import (
    EvidenceBinding,
    MetricBinding,
    ReportMetricSlot,
    ReportSection,
    StructuredReport,
)
from commerce_harness.delivery.number_guard import MetricLedger, NumberGuard
from commerce_harness.delivery.report import (
    ReportRenderer,
    build_evidence_report,
    build_reconciliation_report,
)


def data():
    evidence = EvidenceBinding(
        "file-1",
        3,
        "refund",
        "2026-05",
        "SHOP-A",
        "refund-v1",
        "70.00",
    )
    binding = MetricBinding(
        "refund-binding",
        "refund",
        "2026-05",
        "SHOP-A",
        "refund-v1",
        "70.00",
        (evidence,),
    )
    slot = ReportMetricSlot(
        "refund-slot",
        "退款",
        binding.binding_id,
        binding.metric,
        binding.period,
        binding.shop,
        binding.definition_id,
        binding.value,
        binding.evidence,
    )
    return MetricLedger([binding]), slot


def test_reconciliation_and_evidence_templates_render_json() -> None:
    ledger, slot = data()
    reports = [
        build_reconciliation_report(
            report_id="recon",
            title="对账结果",
            period="2026-05",
            shop="SHOP-A",
            summary="差额已经完成核验",
            metrics=[slot],
        ),
        build_evidence_report(
            report_id="evidence",
            title="证据结果",
            period="2026-05",
            shop="SHOP-A",
            explanation="明细证据已经绑定",
            metrics=[slot],
        ),
    ]
    rendered = [json.loads(ReportRenderer().render_json(report, ledger)) for report in reports]
    assert [item["kind"] for item in rendered] == ["reconciliation", "evidence"]
    assert all(item["sections"][0]["metrics"][0]["value"] == "70.00" for item in rendered)


def test_number_guard_rejects_unknown_binding_and_numeric_note() -> None:
    ledger, slot = data()
    unknown = ReportMetricSlot(
        slot.slot_id,
        slot.label,
        "missing-binding",
        slot.metric,
        slot.period,
        slot.shop,
        slot.definition_id,
        slot.value,
        slot.evidence,
    )
    report = StructuredReport(
        "report",
        "owner",
        "经营结果",
        "2026-05",
        "SHOP-A",
        (ReportSection("结论", "已完成核验", (unknown,), ("发现 1 个问题",)),),
    )
    result = NumberGuard().validate_report(report, ledger)
    assert not result.valid
    assert any("unknown binding" in violation for violation in result.violations)
    assert any("note contains" in violation for violation in result.violations)


def test_metric_ledger_rejects_duplicate_dimensions() -> None:
    ledger, _ = data()
    binding = ledger.bindings[0]
    with pytest.raises(ValueError, match="definition"):
        MetricLedger([binding, MetricBinding(
            "another-id",
            binding.metric,
            binding.period,
            binding.shop,
            binding.definition_id,
            binding.value,
            binding.evidence,
        )])
