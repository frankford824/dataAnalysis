"""Immutable source snapshots and normalized Parquet artifacts."""

from .artifacts import (
    NormalizedArtifactManifest,
    NormalizedArtifactRepository,
    NormalizedArtifactSpec,
    ParquetArtifactStore,
)
from .finance_win import FinanceWinReader
from .reader import (
    BytesReader,
    LocalFileReader,
    Reader,
    ReaderMetadata,
    SourceAvailability,
)
from .store import (
    SnapshotIntegrityError,
    SnapshotManifest,
    SnapshotSourceChangedError,
    SnapshotStore,
    SnapshotUnavailableError,
)

__all__ = [
    "BytesReader",
    "FinanceWinReader",
    "LocalFileReader",
    "NormalizedArtifactManifest",
    "NormalizedArtifactRepository",
    "NormalizedArtifactSpec",
    "ParquetArtifactStore",
    "Reader",
    "ReaderMetadata",
    "SnapshotIntegrityError",
    "SnapshotManifest",
    "SnapshotSourceChangedError",
    "SnapshotStore",
    "SnapshotUnavailableError",
    "SourceAvailability",
]
