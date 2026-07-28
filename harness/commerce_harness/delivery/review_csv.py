from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TextIO, cast

from commerce_harness.judgment.corrections import CorrectionEntry
from commerce_harness.judgment.models import SuggestionCandidate, SuggestionKind

from .models import ReviewDecision

_COLUMNS = (
    "suggestion_id",
    "residual_id",
    "kind",
    "category",
    "suggested_action",
    "rationale",
    "confidence",
    "evidence_json",
    "candidate_hash",
    "human_decision",
    "replacement_action",
    "human_reason",
    "decided_by",
)


def _candidate_hash(candidate: SuggestionCandidate) -> str:
    canonical = json.dumps(
        candidate.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_display(value: str) -> str:
    # 防止业务人员用 Excel 打开 CSV 时触发公式。
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def _open_text_writer(target: str | Path | TextIO) -> tuple[TextIO, bool]:
    if hasattr(target, "write"):
        return target, False  # type: ignore[return-value]
    # The caller owns and closes path-backed handles after the CSV operation.
    handle = Path(target).open("w", encoding="utf-8-sig", newline="")  # noqa: SIM115
    return handle, True


def _open_text_reader(source: str | Path | TextIO) -> tuple[TextIO, bool]:
    if hasattr(source, "read"):
        return source, False  # type: ignore[return-value]
    # The caller owns and closes path-backed handles after the CSV operation.
    handle = Path(source).open(encoding="utf-8-sig", newline="")  # noqa: SIM115
    return handle, True


def export_review_csv(
    candidates: Iterable[SuggestionCandidate],
    target: str | Path | TextIO,
) -> int:
    handle, should_close = _open_text_writer(target)
    count = 0
    try:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            evidence = [
                {
                    "file_id": item.file_id,
                    "row_no": item.row_no,
                    "metric": item.metric,
                    "period": item.period,
                    "shop": item.shop,
                    "value": str(item.value),
                    "definition_id": item.definition_id,
                }
                for item in candidate.citations
            ]
            writer.writerow(
                {
                    "suggestion_id": candidate.suggestion_id,
                    "residual_id": candidate.residual_id,
                    "kind": cast(SuggestionKind, candidate.kind).value,
                    "category": _safe_display(candidate.category),
                    "suggested_action": _safe_display(candidate.action),
                    "rationale": _safe_display(candidate.rationale),
                    "confidence": str(candidate.confidence),
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    "candidate_hash": _candidate_hash(candidate),
                    "human_decision": "",
                    "replacement_action": "",
                    "human_reason": "",
                    "decided_by": "",
                }
            )
            count += 1
    finally:
        if should_close:
            handle.close()
    return count


def import_review_csv(
    source: str | Path | TextIO,
    *,
    candidates: Mapping[str, SuggestionCandidate],
) -> tuple[ReviewDecision, ...]:
    handle, should_close = _open_text_reader(source)
    decisions: list[ReviewDecision] = []
    seen: set[str] = set()
    try:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or set(_COLUMNS) - set(reader.fieldnames):
            raise ValueError("review CSV schema is incomplete")
        for row in reader:
            suggestion_id = (row.get("suggestion_id") or "").strip()
            if not suggestion_id:
                raise ValueError("suggestion_id is required")
            if suggestion_id in seen:
                raise ValueError(f"duplicate review row: {suggestion_id}")
            seen.add(suggestion_id)
            candidate = candidates.get(suggestion_id)
            if candidate is None:
                raise ValueError(f"unknown suggestion_id: {suggestion_id}")
            expected_hash = _candidate_hash(candidate)
            if row.get("candidate_hash") != expected_hash:
                raise ValueError(f"candidate payload was changed: {suggestion_id}")
            if row.get("residual_id") != candidate.residual_id:
                raise ValueError(f"residual_id was changed: {suggestion_id}")
            decision = (row.get("human_decision") or "").strip()
            if not decision:
                continue
            replacement = (row.get("replacement_action") or "").strip()
            final_action = (
                candidate.action
                if decision == "approve_suggestion"
                else replacement if decision == "replace" else ""
            )
            decisions.append(
                ReviewDecision(
                    suggestion_id=suggestion_id,
                    residual_id=candidate.residual_id,
                    decision=decision,
                    final_action=final_action,
                    human_reason=(row.get("human_reason") or "").strip(),
                    decided_by=(row.get("decided_by") or "").strip(),
                    candidate_hash=expected_hash,
                )
            )
    finally:
        if should_close:
            handle.close()
    return tuple(decisions)


def corrections_from_decisions(
    decisions: Iterable[ReviewDecision],
    *,
    candidates: Mapping[str, SuggestionCandidate],
) -> tuple[CorrectionEntry, ...]:
    corrections: list[CorrectionEntry] = []
    for decision in decisions:
        candidate = candidates[decision.suggestion_id]
        if decision.decision == "approve_suggestion":
            continue
        human_action = decision.final_action or decision.decision
        citation = candidate.citations[0]
        digest = hashlib.sha256(
            f"{candidate.suggestion_id}:{human_action}:{decision.decided_by}".encode()
        ).hexdigest()[:20]
        corrections.append(
            CorrectionEntry(
                correction_id=f"correction-{digest}",
                suggestion_id=candidate.suggestion_id,
                residual_id=candidate.residual_id,
                category=candidate.category,
                metric=citation.metric,
                period=citation.period,
                shop=citation.shop,
                model_action=candidate.action,
                human_action=human_action,
                human_reason=decision.human_reason,
                decided_by=decision.decided_by,
            )
        )
    return tuple(corrections)


def export_review_csv_text(candidates: Iterable[SuggestionCandidate]) -> str:
    buffer = io.StringIO()
    export_review_csv(candidates, buffer)
    return buffer.getvalue()
