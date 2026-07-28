from __future__ import annotations

import io
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


class SourceAvailability(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    PLACEHOLDER = "placeholder"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReaderMetadata:
    """Metadata used to prove that a source did not change while being copied."""

    uri: str
    size: int
    modified_ns: int | None
    etag: str | None = None
    version_id: str | None = None
    availability: SourceAvailability = SourceAvailability.UNKNOWN
    stable: bool = True

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError("source size must be non-negative")

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.uri,
            self.size,
            self.modified_ns,
            self.etag,
            self.version_id,
            self.availability,
            self.stable,
        )


class Reader(ABC):
    """Streaming, side-effect-free source reader.

    Remote readers can be added without coupling the snapshot layer to SSH,
    SMB, S3, or finance-win. ``stat`` must not recall an offline placeholder.
    """

    @abstractmethod
    def stat(self) -> ReaderMetadata:
        raise NotImplementedError

    @abstractmethod
    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        raise NotImplementedError


class LocalFileReader(Reader):
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def stat(self) -> ReaderMetadata:
        info = self.path.stat(follow_symlinks=False)
        if not self.path.is_file() or self.path.is_symlink():
            return ReaderMetadata(
                uri=self.path.resolve(strict=False).as_uri(),
                size=info.st_size,
                modified_ns=info.st_mtime_ns,
                availability=SourceAvailability.PLACEHOLDER,
                stable=False,
            )
        return ReaderMetadata(
            uri=self.path.resolve().as_uri(),
            size=info.st_size,
            modified_ns=info.st_mtime_ns,
            availability=SourceAvailability.ONLINE,
            stable=True,
        )

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        with self.path.open("rb") as source:
            while chunk := source.read(chunk_size):
                yield chunk


class BytesReader(Reader):
    """In-memory reader intended for synthetic fixtures and adapters."""

    def __init__(
        self,
        content: bytes,
        *,
        uri: str = "memory://source",
        modified_ns: int = 0,
        availability: SourceAvailability = SourceAvailability.ONLINE,
        stable: bool = True,
    ) -> None:
        self._content = content
        self._metadata = ReaderMetadata(
            uri=uri,
            size=len(content),
            modified_ns=modified_ns,
            availability=availability,
            stable=stable,
        )

    def stat(self) -> ReaderMetadata:
        return self._metadata

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        source: BinaryIO = io.BytesIO(self._content)
        while chunk := source.read(chunk_size):
            yield chunk

    def with_metadata(
        self,
        *,
        availability: SourceAvailability | None = None,
        stable: bool | None = None,
    ) -> BytesReader:
        clone = BytesReader(self._content)
        clone._metadata = replace(
            self._metadata,
            availability=availability or self._metadata.availability,
            stable=self._metadata.stable if stable is None else stable,
        )
        return clone
