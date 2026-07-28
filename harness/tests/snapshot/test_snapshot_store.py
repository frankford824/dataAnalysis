from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from commerce_harness.snapshot import (
    BytesReader,
    LocalFileReader,
    NormalizedArtifactSpec,
    ParquetArtifactStore,
    Reader,
    ReaderMetadata,
    SnapshotSourceChangedError,
    SnapshotStore,
    SnapshotUnavailableError,
    SourceAvailability,
)


class ChangingReader(Reader):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    def stat(self) -> ReaderMetadata:
        self.calls += 1
        return ReaderMetadata(
            uri="synthetic://changing",
            size=len(self.content),
            modified_ns=self.calls,
            availability=SourceAvailability.ONLINE,
        )

    def iter_chunks(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


def test_snapshot_is_streamed_content_addressed_and_never_overwritten(tmp_path: Path) -> None:
    content = b"immutable-source-content"
    store = SnapshotStore(tmp_path, chunk_size=3)

    first = store.capture(BytesReader(content), original_name="orders.csv")
    second = store.capture(BytesReader(content), original_name="orders-copy.csv")

    assert first.content_sha256 == hashlib.sha256(content).hexdigest()
    assert first.object_path == second.object_path
    assert first.snapshot_id != second.snapshot_id
    assert Path(first.object_path).read_bytes() == content
    assert len(list((tmp_path / "manifests" / "snapshots").glob("*.json"))) == 2


def test_local_file_reader_is_online_and_can_be_captured(tmp_path: Path) -> None:
    source = tmp_path / "orders.csv"
    source.write_bytes(b"order_id,amount\nA-1,12.34\n")

    manifest = SnapshotStore(tmp_path / "snapshots").capture(LocalFileReader(source))

    assert manifest.source.availability is SourceAvailability.ONLINE
    assert Path(manifest.object_path).read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    ("availability", "stable"),
    [
        (SourceAvailability.OFFLINE, True),
        (SourceAvailability.PLACEHOLDER, True),
        (SourceAvailability.UNKNOWN, True),
        (SourceAvailability.ONLINE, False),
    ],
)
def test_snapshot_rejects_offline_placeholder_and_unstable_sources(
    tmp_path: Path,
    availability: SourceAvailability,
    stable: bool,
) -> None:
    reader = BytesReader(b"not-safe").with_metadata(
        availability=availability,
        stable=stable,
    )
    with pytest.raises(SnapshotUnavailableError):
        SnapshotStore(tmp_path).capture(reader)
    assert not list((tmp_path / "staging").glob("*.part"))


def test_snapshot_rejects_metadata_change_and_removes_staging_file(tmp_path: Path) -> None:
    with pytest.raises(SnapshotSourceChangedError):
        SnapshotStore(tmp_path, chunk_size=2).capture(ChangingReader(b"abcdef"))
    assert not list((tmp_path / "staging").glob("*.part"))
    assert not (tmp_path / "objects").exists()


def test_parquet_artifact_preserves_decimal_and_is_content_addressed(tmp_path: Path) -> None:
    money_type = pa.decimal128(38, 4)
    table = pa.table(
        {
            "order_id": pa.array(["A-1", "A-2"], type=pa.string()),
            "amount": pa.array([Decimal("12.3400"), Decimal("-0.0100")], type=money_type),
        }
    )
    spec = NormalizedArtifactSpec(
        dataset_kind="order",
        schema_version="order-v1",
        source_snapshot_sha256="a" * 64,
        rule_version="rules-v1",
        partition={"month": "2026-02", "store": "synthetic"},
    )
    store = ParquetArtifactStore(tmp_path)

    first = store.write_table(table, spec=spec)
    second = store.write_table(table, spec=spec)

    assert first.content_sha256 == second.content_sha256
    assert first.parquet_path == second.parquet_path
    assert first.row_count == 2
    restored = pq.read_table(first.parquet_path)
    assert restored.schema.field("amount").type == money_type
    assert restored.column("amount").to_pylist() == [
        Decimal("12.3400"),
        Decimal("-0.0100"),
    ]
