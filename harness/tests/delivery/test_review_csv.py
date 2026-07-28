from __future__ import annotations

import csv
import io

import pytest

from commerce_harness.delivery.review_csv import (
    corrections_from_decisions,
    export_review_csv,
    import_review_csv,
)
from commerce_harness.judgment.models import EvidenceCitation, SuggestionCandidate


def candidate(*, action: str = "核对跨期") -> SuggestionCandidate:
    return SuggestionCandidate(
        suggestion_id="suggestion-1",
        residual_id="residual-1",
        kind="explanation",
        category="timing",
        action=action,
        rationale="证据支持跨期假设",
        confidence="0.82",
        citations=(
            EvidenceCitation(
                file_id="file-1",
                row_no=4,
                metric="received",
                period="2026-05",
                shop="SHOP-A",
                value="20.00",
                definition_id="received-v1",
            ),
        ),
        source_model="model-a",
    )


def exported_rows(item: SuggestionCandidate) -> tuple[list[str], list[dict[str, str]]]:
    buffer = io.StringIO()
    assert export_review_csv([item], buffer) == 1
    buffer.seek(0)
    reader = csv.DictReader(buffer)
    assert reader.fieldnames is not None
    return reader.fieldnames, list(reader)


def write_rows(fieldnames: list[str], rows: list[dict[str, str]]) -> io.StringIO:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return buffer


def test_review_csv_export_and_human_import_are_l0_only() -> None:
    item = candidate()
    fieldnames, rows = exported_rows(item)
    rows[0]["human_decision"] = "replace"
    rows[0]["replacement_action"] = "跨期挂账"
    rows[0]["human_reason"] = "实际到账属于次月"
    rows[0]["decided_by"] = "reviewer"
    decisions = import_review_csv(
        write_rows(fieldnames, rows),
        candidates={item.suggestion_id: item},
    )
    assert len(decisions) == 1
    assert decisions[0].final_action == "跨期挂账"
    assert decisions[0].decided_by_human
    assert not decisions[0].may_write_ledger
    corrections = corrections_from_decisions(
        decisions,
        candidates={item.suggestion_id: item},
    )
    assert len(corrections) == 1
    assert corrections[0].model_action == "核对跨期"
    assert corrections[0].human_action == "跨期挂账"


def test_review_import_rejects_candidate_hash_tampering() -> None:
    item = candidate()
    fieldnames, rows = exported_rows(item)
    rows[0]["candidate_hash"] = "forged"
    rows[0]["human_decision"] = "approve_suggestion"
    rows[0]["human_reason"] = "已复核"
    rows[0]["decided_by"] = "reviewer"
    with pytest.raises(ValueError, match="payload was changed"):
        import_review_csv(
            write_rows(fieldnames, rows),
            candidates={item.suggestion_id: item},
        )


def test_csv_cells_cannot_be_used_as_excel_formulas() -> None:
    item = candidate(action="=HYPERLINK(\"bad\")")
    _, rows = exported_rows(item)
    assert rows[0]["suggested_action"].startswith("'=")


def test_blank_human_decision_is_not_imported_as_a_success() -> None:
    item = candidate()
    fieldnames, rows = exported_rows(item)
    decisions = import_review_csv(
        write_rows(fieldnames, rows),
        candidates={item.suggestion_id: item},
    )
    assert decisions == ()

