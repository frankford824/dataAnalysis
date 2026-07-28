from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True, slots=True)
class NormalizedArtifactSpec:
    dataset_kind: str
    schema_version: str
    source_snapshot_sha256: str
    rule_version: str | None = None
    partition: dict[str, str] | None = None

    def __post_init__(self) -> None:
        digest = self.source_snapshot_sha256
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("source_snapshot_sha256 must be a lowercase SHA-256 digest")
        if not self.dataset_kind.strip() or not self.schema_version.strip():
            raise ValueError("dataset_kind and schema_version are required")


@dataclass(frozen=True, slots=True)
class NormalizedArtifactManifest:
    artifact_id: str
    content_sha256: str
    byte_size: int
    row_count: int
    parquet_path: str
    arrow_schema: str
    spec: NormalizedArtifactSpec
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class NormalizedArtifactRepository(Protocol):
    def write_batches(
        self,
        batches: Iterable[pa.RecordBatch],
        *,
        schema: pa.Schema,
        spec: NormalizedArtifactSpec,
    ) -> NormalizedArtifactManifest: ...


class ParquetArtifactStore:
    """Streaming Parquet writer backed by immutable content-addressed files."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def write_table(
        self,
        table: pa.Table,
        *,
        spec: NormalizedArtifactSpec,
    ) -> NormalizedArtifactManifest:
        return self.write_batches(table.to_batches(), schema=table.schema, spec=spec)

    def write_batches(
        self,
        batches: Iterable[pa.RecordBatch],
        *,
        schema: pa.Schema,
        spec: NormalizedArtifactSpec,
    ) -> NormalizedArtifactManifest:
        staging = self.root / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        row_count = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb",
                prefix="normalized-",
                suffix=".parquet.part",
                dir=staging,
                delete=False,
            ) as output:
                temporary_path = Path(output.name)

            with pq.ParquetWriter(
                temporary_path,
                schema,
                compression="zstd",
                use_dictionary=True,
                write_statistics=True,
            ) as writer:
                for batch in batches:
                    if batch.schema != schema:
                        raise ValueError("all record batches must use the declared Arrow schema")
                    writer.write_batch(batch)
                    row_count += batch.num_rows

            content_sha256, byte_size = self._hash_file(temporary_path)
            target = (
                self.root
                / "parquet"
                / "sha256"
                / content_sha256[:2]
                / f"{content_sha256}.parquet"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary_path, target)
                temporary_path.unlink()
            except FileExistsError:
                existing_hash, existing_size = self._hash_file(target)
                if existing_hash != content_sha256 or existing_size != byte_size:
                    raise RuntimeError(
                        "existing Parquet artifact failed integrity verification"
                    ) from None
                temporary_path.unlink()
            temporary_path = None

            manifest = NormalizedArtifactManifest(
                artifact_id=str(uuid.uuid4()),
                content_sha256=content_sha256,
                byte_size=byte_size,
                row_count=row_count,
                parquet_path=str(target),
                arrow_schema=str(schema),
                spec=spec,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._write_manifest(manifest)
            return manifest
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _write_manifest(self, manifest: NormalizedArtifactManifest) -> None:
        directory = self.root / "manifests" / "normalized"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{manifest.artifact_id}.json"
        payload = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with path.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
