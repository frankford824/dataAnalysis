from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from .analytics import _periods_from_path, _store_from_record
from .bootstrap import StoreTarget, bootstrap_targets
from .code_identity import resolve_code_identity
from .config import HarnessConfig
from .inventory import _connector
from .memory.database import DuckDBMemory
from .snapshot.finance_win import FinanceWinReader
from .snapshot.store import SnapshotStore
from .workbench import WorkbenchPaths


@dataclass(frozen=True, slots=True)
class FreezeResult:
    run_id: str
    candidate_count: int
    captured_count: int
    reused_count: int
    failed_count: int
    input_manifest_sha256: str


_STORE_DATA_PURPOSES = {
    "orders",
    "settlement",
    "advertising",
    "product_cost",
    "shipping",
}


def _period_token(value: str) -> str:
    normalized = value.strip()
    if len(normalized) == 4 and normalized.isdigit():
        return normalized
    if (
        len(normalized) == 7
        and normalized[4] == "-"
        and normalized[:4].isdigit()
        and normalized[5:].isdigit()
    ):
        return f"{normalized[2:4]}{normalized[5:]}"
    raise ValueError(f"账期必须使用 YYMM 或 YYYY-MM：{value}")


def _configured_targets(
    config: HarnessConfig,
    records: list[dict[str, Any]],
) -> list[StoreTarget]:
    configured_periods = list(
        dict.fromkeys(
            _period_token(value)
            for value in config.source.scope.resolved_periods()
        )
    )
    bound_shops = config.source.scope.bound_shops
    if bound_shops:
        return [
            StoreTarget(name=name, period_tokens=list(configured_periods))
            for name in bound_shops
        ]

    if not config.source.scope.include_all_discovered:
        return []

    discovered_periods: dict[str, set[str]] = {}
    display_names: dict[str, str] = {}
    for record in records:
        if str(record.get("purpose") or "") not in _STORE_DATA_PURPOSES:
            continue
        name = _store_from_record(record)
        if name is None:
            continue
        key = name.casefold()
        display_names.setdefault(key, name)
        periods = discovered_periods.setdefault(key, set())
        for period in _periods_from_path(str(record.get("path") or "")):
            periods.add(_period_token(period))

    targets: list[StoreTarget] = []
    for key in sorted(display_names, key=lambda value: display_names[value]):
        selected_periods = configured_periods or sorted(discovered_periods[key])
        targets.append(
            StoreTarget(
                name=display_names[key],
                period_tokens=list(selected_periods),
            )
        )
    return targets


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_inventory(workbench: WorkbenchPaths) -> tuple[dict[str, Any], str]:
    path = workbench.reports / "source-inventory.json"
    encoded = path.read_bytes()
    payload = json.loads(encoded)
    if payload.get("ssh_alias") != "finance-win-ro":
        raise RuntimeError("冻结只允许使用 finance-win-ro 专用只读账号")
    return payload, hashlib.sha256(encoded).hexdigest()


def _inventory_snapshot_identity(record: Any) -> tuple[str, int, int]:
    modified_ns = int(
        datetime.fromisoformat(
            str(record.mtime_utc).replace("Z", "+00:00")
        ).timestamp()
        * 1_000_000_000
    )
    return (
        f"finance-win-ro://{record.path}",
        modified_ns,
        int(record.size),
    )


def freeze_candidates(
    config: HarnessConfig,
    workbench: WorkbenchPaths,
    *,
    progress_callback: Callable[
        [DuckDBMemory, int, int],
        None,
    ]
    | None = None,
) -> FreezeResult:
    try:
        from finance_agent.models import FileRecord
    except ImportError as exc:
        raise RuntimeError(
            "finance-win 连接器不可用；请将 host-agent 加入 PYTHONPATH"
        ) from exc

    inventory, inventory_sha = _load_inventory(workbench)
    candidate_ids = set(inventory["candidate_source_ids"])
    records = [
        item for item in inventory["records"] if item["source_id"] in candidate_ids
    ]
    if not records:
        raise RuntimeError("来源清单没有目标候选文件")

    run_id = f"freeze_{uuid.uuid4().hex}"
    captured = 0
    reused = 0
    failures: list[dict[str, str]] = []
    snapshot_by_source_id: dict[str, str] = {}
    connector = _connector(config)
    snapshot_store = SnapshotStore(workbench.snapshots)
    code_sha = resolve_code_identity().value

    with _writer_lock(workbench.locks / "duckdb-writer.lock"):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            database.execute(
                """
                INSERT INTO run_log (
                    run_id, run_kind, status, input_manifest_sha256, code_sha
                )
                VALUES (?, 'freeze', 'running', ?, ?)
                """,
                [run_id, inventory_sha, code_sha],
            )
            existing_snapshot_by_identity: dict[
                tuple[str, int, int],
                str,
            ] = {}
            for (
                source_uri,
                source_modified_ns,
                byte_size,
                snapshot_id,
            ) in database.execute(
                """
                SELECT source_uri, source_modified_ns, byte_size, snapshot_id
                FROM source_snapshot
                WHERE source_modified_ns IS NOT NULL
                ORDER BY captured_at
                """
            ).fetchall():
                existing_snapshot_by_identity.setdefault(
                    (
                        str(source_uri),
                        int(source_modified_ns),
                        int(byte_size),
                    ),
                    str(snapshot_id),
                )
            for index, item in enumerate(records, start=1):
                record = FileRecord(
                    source_id=str(item["source_id"]),
                    path=str(item["path"]),
                    purpose=str(item["purpose"]),
                    extension=str(item["extension"]),
                    size=int(item["size"]),
                    mtime_utc=str(item["mtime_utc"]),
                    attributes=tuple(str(value) for value in item["attributes"]),
                    sha256=item.get("sha256"),
                    sheet=item.get("sheet"),
                    recent_target=item.get("recent_target"),
                )
                reader = FinanceWinReader(connector, record)
                try:
                    inventory_uri, inventory_modified_ns, inventory_size = (
                        _inventory_snapshot_identity(record)
                    )
                    existing_snapshot_id = existing_snapshot_by_identity.get(
                        (
                            inventory_uri,
                            inventory_modified_ns,
                            inventory_size,
                        )
                    )
                    if existing_snapshot_id:
                        snapshot_by_source_id[record.source_id] = (
                            existing_snapshot_id
                        )
                        reused += 1
                        continue
                    manifest = snapshot_store.capture(
                        reader,
                        original_name=PureWindowsPath(record.path).name,
                        media_type=mimetypes.guess_type(record.path)[0],
                    )
                    database.register_snapshot(manifest)
                    snapshot_by_source_id[record.source_id] = manifest.snapshot_id
                    captured += 1
                except Exception as exc:
                    failures.append(
                        {
                            "source_id": record.source_id,
                            "path": record.path,
                            "error": str(exc),
                        }
                    )
                if progress_callback is not None and (
                    index == len(records) or index % 10 == 0
                ):
                    progress_callback(database, index, len(records))

            if failures:
                database.execute(
                    """
                    UPDATE run_log
                    SET status = 'failed', finished_at = current_timestamp,
                        error_code = 'snapshot_failed', error_detail = ?,
                        metrics_json = ?
                    WHERE run_id = ?
                    """,
                    [
                        f"{len(failures)} 个候选文件冻结失败",
                        json.dumps(
                            {
                                "captured": captured,
                                "reused": reused,
                                "failures": failures,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        run_id,
                    ],
                )
            else:
                try:
                    targets = _configured_targets(config, records)
                    bootstrap_targets(
                        database,
                        freeze_run_id=run_id,
                        targets=targets,
                        records=records,
                        snapshot_by_source_id=snapshot_by_source_id,
                        reconciliation_mode=config.reconciliation.mode,
                        retire_missing=True,
                    )
                except Exception as exc:
                    database.execute(
                        """
                        UPDATE run_log
                        SET status = 'failed', finished_at = current_timestamp,
                            error_code = 'bootstrap_failed', error_detail = ?,
                            metrics_json = ?
                        WHERE run_id = ?
                        """,
                        [
                            str(exc),
                            json.dumps(
                                {
                                    "candidate_count": len(records),
                                    "captured": captured,
                                    "reused": reused,
                                    "inventory_sha256": inventory_sha,
                                },
                                sort_keys=True,
                            ),
                            run_id,
                        ],
                    )
                    raise
                else:
                    database.execute(
                        """
                        UPDATE run_log
                        SET status = 'succeeded', finished_at = current_timestamp,
                            metrics_json = ?
                        WHERE run_id = ?
                        """,
                        [
                            json.dumps(
                                {
                                    "candidate_count": len(records),
                                    "captured": captured,
                                    "reused": reused,
                                    "inventory_sha256": inventory_sha,
                                },
                                sort_keys=True,
                            ),
                            run_id,
                        ],
                    )

    return FreezeResult(
        run_id=run_id,
        candidate_count=len(records),
        captured_count=captured,
        reused_count=reused,
        failed_count=len(failures),
        input_manifest_sha256=inventory_sha,
    )
