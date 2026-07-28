from __future__ import annotations

import json
from pathlib import Path

import pytest
from finance_agent.models import FileRecord

import commerce_harness.freeze as freeze_module
from commerce_harness.config import (
    HarnessConfig,
    SourceConfig,
    SourceScope,
    WorkspaceConfig,
)
from commerce_harness.freeze import (
    _inventory_snapshot_identity,
    freeze_candidates,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.snapshot.finance_win import FinanceWinReader
from commerce_harness.snapshot.reader import SourceAvailability
from commerce_harness.workbench import initialize


class FakeConnector:
    def __init__(self, record: FileRecord, content: bytes) -> None:
        self.record = record
        self.content = content

    def stat_record(self, _record: FileRecord) -> FileRecord:
        return self.record

    def iter_chunks(self, _record: FileRecord, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


def test_finance_win_reader_maps_stable_remote_metadata_and_streams():
    content = b"order_id,sales\nA,1.00\n"
    record = FileRecord(
        source_id="stable-version",
        path=r"D:\FinanceData\订单\orders.csv",
        purpose="orders",
        extension=".csv",
        size=len(content),
        mtime_utc="2026-01-01T00:00:00Z",
        attributes=("Archive",),
    )
    reader = FinanceWinReader(FakeConnector(record, content), record)
    metadata = reader.stat()

    assert metadata.availability is SourceAvailability.ONLINE
    assert metadata.stable is True
    assert metadata.version_id == "stable-version"
    assert b"".join(reader.iter_chunks(5)) == content


def test_finance_win_reader_rejects_offline_metadata():
    record = FileRecord(
        source_id="offline-version",
        path=r"D:\FinanceData\订单\orders.csv",
        purpose="orders",
        extension=".csv",
        size=0,
        mtime_utc="2026-01-01T00:00:00Z",
        attributes=("Offline",),
    )
    reader = FinanceWinReader(FakeConnector(record, b""), record)

    assert reader.stat().availability is SourceAvailability.OFFLINE
    assert reader.stat().stable is False


def test_inventory_snapshot_identity_matches_reader_metadata() -> None:
    record = FileRecord(
        source_id="source-1",
        path=r"D:\电商\订单.csv",
        purpose="orders",
        extension=".csv",
        size=123,
        mtime_utc="2026-07-24T12:34:56+00:00",
        attributes=(),
    )

    assert _inventory_snapshot_identity(record) == (
        r"finance-win-ro://D:\电商\订单.csv",
        1784896496000000000,
        123,
    )


def test_freeze_candidates_captures_then_reuses_inventory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"order_id,amount,date\nA-1,10.00,2026-02-01\n"
    record = FileRecord(
        source_id="source-orders",
        path=r"D:\数据\店铺\测试店铺\订单_2602.csv",
        purpose="orders",
        extension=".csv",
        size=len(content),
        mtime_utc="2026-02-28T00:00:00Z",
        attributes=("Archive",),
    )
    config = HarnessConfig(
        workspace=WorkspaceConfig(root=tmp_path),
        source=SourceConfig(
            scope=SourceScope(shop="测试店铺", periods=["2602"]),
        ),
    )
    workbench = initialize(config)
    inventory = {
        "ssh_alias": "finance-win-ro",
        "candidate_source_ids": [record.source_id],
        "records": [
            {
                "source_id": record.source_id,
                "path": record.path,
                "purpose": record.purpose,
                "extension": record.extension,
                "size": record.size,
                "mtime_utc": record.mtime_utc,
                "attributes": list(record.attributes),
                "sha256": None,
                "sheet": None,
                "recent_target": None,
            }
        ],
    }
    (workbench.reports / "source-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        freeze_module,
        "_connector",
        lambda _config: FakeConnector(record, content),
    )
    progress: list[tuple[int, int]] = []

    first = freeze_candidates(
        config,
        workbench,
        progress_callback=lambda _database, completed, total: progress.append(
            (completed, total)
        ),
    )
    second = freeze_candidates(config, workbench)

    assert (first.captured_count, first.reused_count, first.failed_count) == (
        1,
        0,
        0,
    )
    assert (second.captured_count, second.reused_count, second.failed_count) == (
        0,
        1,
        0,
    )
    assert progress == [(1, 1)]
    with DuckDBMemory(workbench.database) as database:
        assert database.fetchone_required(
            "SELECT count(*) FROM source_snapshot"
        )[0] == 1
        assert database.execute(
            """
            SELECT status FROM run_log
            WHERE run_kind = 'freeze'
            ORDER BY started_at
            """
        ).fetchall() == [("succeeded",), ("succeeded",)]
