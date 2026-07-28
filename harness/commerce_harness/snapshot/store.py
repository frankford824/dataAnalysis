from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

from .reader import Reader, ReaderMetadata, SourceAvailability


class SnapshotError(RuntimeError):
    pass


class SnapshotUnavailableError(SnapshotError):
    pass


class SnapshotSourceChangedError(SnapshotError):
    pass


class SnapshotIntegrityError(SnapshotError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    snapshot_id: str
    content_sha256: str
    byte_size: int
    object_path: str
    source: ReaderMetadata
    captured_at: str
    original_name: str | None = None
    media_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        source = cast(dict[str, object], result["source"])
        source["availability"] = self.source.availability.value
        return result


class SnapshotStore:
    """Content-addressed immutable object store.

    Source data is copied to staging in chunks. The source metadata is checked
    before and after the stream; only then is the object linked into its
    SHA-256 path. Existing objects are verified and never overwritten.
    """

    def __init__(self, root: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.root = Path(root)
        self.chunk_size = chunk_size

    def capture(
        self,
        reader: Reader,
        *,
        original_name: str | None = None,
        media_type: str | None = None,
    ) -> SnapshotManifest:
        before = reader.stat()
        self._require_readable(before)
        staging_dir = self.root / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        byte_size = 0
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="xb",
                prefix="snapshot-",
                suffix=".part",
                dir=staging_dir,
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                for chunk in reader.iter_chunks(self.chunk_size):
                    if not isinstance(chunk, bytes):
                        raise TypeError("reader chunks must be bytes")
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
                output.flush()
                os.fsync(output.fileno())

            after = reader.stat()
            self._require_readable(after)
            if before.identity != after.identity or byte_size != before.size:
                raise SnapshotSourceChangedError(
                    "source metadata changed while the snapshot was being captured"
                )

            content_sha256 = digest.hexdigest()
            object_path = self._object_path(content_sha256)
            object_path.parent.mkdir(parents=True, exist_ok=True)
            self._publish_immutable(temporary_path, object_path, content_sha256, byte_size)
            temporary_path = None

            manifest = SnapshotManifest(
                snapshot_id=str(uuid.uuid4()),
                content_sha256=content_sha256,
                byte_size=byte_size,
                object_path=str(object_path),
                source=before,
                captured_at=datetime.now(UTC).isoformat(),
                original_name=original_name,
                media_type=media_type,
            )
            self._write_manifest(manifest)
            return manifest
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def open_object(self, content_sha256: str) -> BinaryIO:
        path = self._object_path(self._validate_digest(content_sha256))
        return path.open("rb")

    def _object_path(self, content_sha256: str) -> Path:
        digest = self._validate_digest(content_sha256)
        return self.root / "objects" / "sha256" / digest[:2] / digest

    @staticmethod
    def _validate_digest(content_sha256: str) -> str:
        if len(content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in content_sha256
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return content_sha256

    @staticmethod
    def _require_readable(metadata: ReaderMetadata) -> None:
        if metadata.availability is not SourceAvailability.ONLINE:
            raise SnapshotUnavailableError(
                f"source is not safely available: {metadata.availability.value}"
            )
        if not metadata.stable:
            raise SnapshotUnavailableError("source is marked unstable")

    def _publish_immutable(
        self,
        temporary_path: Path,
        object_path: Path,
        digest: str,
        byte_size: int,
    ) -> None:
        try:
            os.link(temporary_path, object_path)
            temporary_path.unlink()
        except FileExistsError:
            self._verify_existing(object_path, digest, byte_size)
            temporary_path.unlink()

    @staticmethod
    def _verify_existing(path: Path, expected_digest: str, expected_size: int) -> None:
        if path.stat().st_size != expected_size:
            raise SnapshotIntegrityError("existing content-addressed object has the wrong size")
        digest = hashlib.sha256()
        with path.open("rb") as existing:
            while chunk := existing.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected_digest:
            raise SnapshotIntegrityError(
                "existing content-addressed object failed hash verification"
            )

    def _write_manifest(self, manifest: SnapshotManifest) -> None:
        manifest_dir = self.root / "manifests" / "snapshots"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_dir / f"{manifest.snapshot_id}.json"
        encoded = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
