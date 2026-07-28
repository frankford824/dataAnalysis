from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from .reader import Reader, ReaderMetadata, SourceAvailability

if TYPE_CHECKING:
    from finance_agent.connectors.windows_ssh import WindowsSshConnector
    from finance_agent.models import FileRecord


class FinanceWinReader(Reader):
    """Read one pre-scanned finance-win file through the dedicated RO account."""

    def __init__(
        self,
        connector: WindowsSshConnector,
        record: FileRecord,
    ) -> None:
        self.connector = connector
        self.record = record

    def stat(self) -> ReaderMetadata:
        current = self.connector.stat_record(self.record)
        attributes = {value.casefold() for value in current.attributes}
        unavailable = bool(
            attributes
            & {
                "offline",
                "recallonopen",
                "unpinned",
                "recallondataaccess",
            }
        )
        modified_ns = int(
            datetime.fromisoformat(
                current.mtime_utc.replace("Z", "+00:00")
            ).timestamp()
            * 1_000_000_000
        )
        return ReaderMetadata(
            uri=f"finance-win-ro://{current.path}",
            size=current.size,
            modified_ns=modified_ns,
            version_id=current.source_id,
            availability=(
                SourceAvailability.OFFLINE
                if unavailable
                else SourceAvailability.ONLINE
            ),
            stable=not unavailable,
        )

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        yield from self.connector.iter_chunks(self.record, chunk_size)
