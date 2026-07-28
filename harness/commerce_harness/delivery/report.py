from __future__ import annotations

import html
import json
from collections.abc import Iterable
from dataclasses import asdict

from .models import ReportMetricSlot, ReportSection, StructuredReport
from .number_guard import MetricLedger, NumberGuard


def build_owner_report(
    *,
    report_id: str,
    title: str,
    period: str,
    shop: str,
    summary: str,
    metrics: Iterable[ReportMetricSlot],
    concerns: Iterable[str] = (),
) -> StructuredReport:
    return StructuredReport(
        report_id=report_id,
        kind="owner",
        title=title,
        period=period,
        shop=shop,
        sections=(
            ReportSection(
                heading="经营结论",
                narrative=summary,
                metrics=tuple(metrics),
                notes=tuple(concerns),
            ),
        ),
    )


def build_reconciliation_report(
    *,
    report_id: str,
    title: str,
    period: str,
    shop: str,
    summary: str,
    metrics: Iterable[ReportMetricSlot],
) -> StructuredReport:
    return StructuredReport(
        report_id=report_id,
        kind="reconciliation",
        title=title,
        period=period,
        shop=shop,
        sections=(
            ReportSection(
                heading="对账结果",
                narrative=summary,
                metrics=tuple(metrics),
            ),
        ),
    )


def build_evidence_report(
    *,
    report_id: str,
    title: str,
    period: str,
    shop: str,
    explanation: str,
    metrics: Iterable[ReportMetricSlot],
) -> StructuredReport:
    return StructuredReport(
        report_id=report_id,
        kind="evidence",
        title=title,
        period=period,
        shop=shop,
        sections=(
            ReportSection(
                heading="证据明细",
                narrative=explanation,
                metrics=tuple(metrics),
            ),
        ),
    )


class ReportRenderer:
    def __init__(self, guard: NumberGuard | None = None) -> None:
        self.guard = guard or NumberGuard()

    def render_html(self, report: StructuredReport, ledger: MetricLedger) -> str:
        self.guard.assert_valid(report, ledger)
        sections: list[str] = []
        for section in report.sections:
            rows = "".join(
                "<tr>"
                f"<th>{html.escape(slot.label)}</th>"
                f"<td>{html.escape(format(slot.value, 'f'))}</td>"
                f"<td>{html.escape(slot.definition_id)}</td>"
                "<td>已绑定</td>"
                "</tr>"
                for slot in section.metrics
            )
            notes = "".join(f"<li>{html.escape(note)}</li>" for note in section.notes)
            sections.append(
                "<section>"
                f"<h2>{html.escape(section.heading)}</h2>"
                f"<p>{html.escape(section.narrative)}</p>"
                "<table><thead><tr><th>指标</th><th>数值</th><th>口径</th><th>证据</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<ul>{notes}</ul>"
                "</section>"
            )
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(report.title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:40px auto;"
            "color:#17212b}table{border-collapse:collapse;width:100%}th,td{padding:10px;"
            "border-bottom:1px solid #d7dde3;text-align:left}td{font-variant-numeric:tabular-nums}"
            "</style></head><body>"
            f"<h1>{html.escape(report.title)}</h1>"
            f"<p>{html.escape(report.shop)} · {html.escape(report.period)}</p>"
            + "".join(sections)
            + "</body></html>"
        )

    def render_json(self, report: StructuredReport, ledger: MetricLedger) -> str:
        self.guard.assert_valid(report, ledger)
        payload = asdict(report)
        for section in payload["sections"]:
            for metric in section["metrics"]:
                metric["value"] = str(metric["value"])
                for evidence in metric["evidence"]:
                    evidence["value"] = str(evidence["value"])
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
