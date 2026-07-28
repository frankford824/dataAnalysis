from __future__ import annotations

from collections.abc import Iterable

from .models import EvidenceRecord, GuardFailure, GuardResult, SuggestionCandidate


class EvidenceLedger:
    def __init__(self, records: Iterable[EvidenceRecord]) -> None:
        self.records = tuple(records)
        self._index = {record.identity: record for record in self.records}
        if len(self._index) != len(self.records):
            raise ValueError("evidence ledger contains duplicate identities")

    def contains(self, identity: tuple[object, ...]) -> bool:
        return identity in self._index


class CiteGuard:
    """核验 file_id×row_no×metric×period×shop×value×口径完整元组。"""

    def verify(self, candidate: SuggestionCandidate, ledger: EvidenceLedger) -> GuardResult:
        failures: list[GuardFailure] = []
        seen: set[tuple[object, ...]] = set()
        for citation in candidate.citations:
            if citation.identity in seen:
                failures.append(GuardFailure(citation, "duplicate citation"))
                continue
            seen.add(citation.identity)
            if not ledger.contains(citation.identity):
                failures.append(
                    GuardFailure(
                        citation,
                        "file_id/row_no/metric/period/shop/value/definition tuple not found",
                    )
                )
        return GuardResult(
            valid=not failures,
            checked_count=len(candidate.citations),
            failures=tuple(failures),
        )

