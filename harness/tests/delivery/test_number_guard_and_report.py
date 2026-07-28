from __future__ import annotations

from dataclasses import replace

import pytest

from commerce_harness.delivery.models import EvidenceBinding, MetricBinding, ReportMetricSlot
from commerce_harness.delivery.number_guard import MetricLedger, NumberGuard
from commerce_harness.delivery.report import ReportRenderer, build_owner_report


def evidence() -> EvidenceBinding:
    return EvidenceBinding(
        file_id="file-1",
        row_no=12,
        metric="profit",
        period="2026-05",
        shop="SHOP-A",
        definition_id="profit-v1",
        value="333.00",
    )


def binding() -> MetricBinding:
    return MetricBinding(
        binding_id="binding-profit",
        metric="profit",
        period="2026-05",
        shop="SHOP-A",
        definition_id="profit-v1",
        value="333.00",
        evidence=(evidence(),),
    )


def slot() -> ReportMetricSlot:
    source = binding()
    return ReportMetricSlot(
        slot_id="profit-slot",
        label="经营利润",
        binding_id=source.binding_id,
        metric=source.metric,
        period=source.period,
        shop=source.shop,
        definition_id=source.definition_id,
        value=source.value,
        evidence=source.evidence,
    )


def report(metric_slot: ReportMetricSlot | None = None, *, narrative: str = "本期经营结果已核验"):
    return build_owner_report(
        report_id="report-1",
        title="经营结果",
        period="2026-05",
        shop="SHOP-A",
        summary=narrative,
        metrics=[metric_slot or slot()],
    )


def test_number_guard_accepts_only_full_dimension_and_evidence_binding() -> None:
    ledger = MetricLedger([binding()])
    result = NumberGuard().validate_report(report(), ledger)
    assert result.valid
    assert result.checked_slots == 1


def test_same_number_from_another_shop_or_definition_is_rejected() -> None:
    ledger = MetricLedger([binding()])
    wrong_shop = replace(slot(), shop="SHOP-B")
    wrong_definition = replace(slot(), definition_id="profit-v2")
    assert not NumberGuard().validate_report(report(wrong_shop), ledger).valid
    assert not NumberGuard().validate_report(report(wrong_definition), ledger).valid


def test_same_number_with_different_evidence_is_rejected() -> None:
    ledger = MetricLedger([binding()])
    forged = replace(evidence(), row_no=99)
    wrong = replace(slot(), evidence=(forged,))
    result = NumberGuard().validate_report(report(wrong), ledger)
    assert not result.valid
    assert any("evidence" in item for item in result.violations)


def test_free_text_numbers_are_forbidden_and_renderer_is_gated() -> None:
    ledger = MetricLedger([binding()])
    unsafe = report(narrative="利润为三百三十三，增长 5%")
    with pytest.raises(ValueError, match="unbound number"):
        ReportRenderer().render_html(unsafe, ledger)


def test_structured_report_renders_only_after_guard_passes() -> None:
    ledger = MetricLedger([binding()])
    rendered = ReportRenderer().render_html(report(), ledger)
    assert "333.00" in rendered
    assert "profit-v1" in rendered
    assert "已绑定" in rendered

