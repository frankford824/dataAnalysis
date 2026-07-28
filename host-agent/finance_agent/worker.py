from __future__ import annotations

import hashlib
import json
import logging
import shutil
import signal
import time
from dataclasses import replace
from typing import Any

from .config import AgentConfig
from .connectors.base import ReadOnlyConnector
from .control_plane import ControlPlaneClient
from .engine import RecomputeSpec, deterministic_recompute
from .models import AgentJob, FileRecord, JobKind, JobStage, safe_relative_output
from .profiling import profile_file
from .state import AgentState

logger = logging.getLogger(__name__)


class AgentWorker:
    def __init__(
        self,
        config: AgentConfig,
        connector: ReadOnlyConnector,
        state: AgentState,
        control: ControlPlaneClient,
    ):
        self.config = config
        self.connector = connector
        self.state = state
        self.control = control
        self.stop_requested = False

    def run_once(self) -> dict[str, Any] | None:
        self.control.flush_events()
        self.control.heartbeat()
        job = self.control.claim_job()
        if job is None:
            return None
        cached = self.state.completed_result(job.idempotency_key)
        if cached is not None:
            self.control.queue_terminal(job.id, True, {"result": cached, "replayed": True})
            self.control.flush_events()
            return cached

        sequence = 1
        try:
            self._emit_progress(
                job.id, sequence, JobStage.CLAIMED, "任务已由外置代理领取"
            )
            sequence += 1
            result = self._execute(job, sequence)
            self.state.mark_completed(job.idempotency_key, job.id, result)
            self.control.queue_terminal(job.id, True, {"result": result})
            self.control.flush_events()
            return result
        except Exception as exc:
            self.control.queue_terminal(
                job.id,
                False,
                {
                    "error_code": type(exc).__name__,
                    "message": str(exc),
                },
            )
            self.control.flush_events()
            raise

    def run_daemon(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        last_heartbeat = 0.0
        while not self.stop_requested:
            now = time.monotonic()
            try:
                self.control.flush_events()
                if now - last_heartbeat >= self.config.heartbeat_seconds:
                    self.control.heartbeat()
                    last_heartbeat = now
                job = self.control.claim_job()
                if job is not None:
                    self._run_claimed(job)
                    continue
            except Exception:
                # 未发送事件保留在 SQLite，下次循环重试；daemon 不因瞬时断网退出。
                logger.warning("外置代理本轮执行失败，将在下轮重试", exc_info=True)
            time.sleep(self.config.poll_seconds)

    def _run_claimed(self, job: AgentJob) -> None:
        cached = self.state.completed_result(job.idempotency_key)
        if cached is not None:
            self.control.queue_terminal(job.id, True, {"result": cached, "replayed": True})
            return
        try:
            self._emit_progress(
                job.id, 1, JobStage.CLAIMED, "任务已由外置代理领取"
            )
            result = self._execute(job, 2)
            self.state.mark_completed(job.idempotency_key, job.id, result)
            self.control.queue_terminal(job.id, True, {"result": result})
        except Exception as exc:  # noqa: BLE001 - job failures are persisted uniformly
            self.control.queue_terminal(
                job.id,
                False,
                {"error_code": type(exc).__name__, "message": str(exc)},
            )

    def _execute(self, job: AgentJob, sequence: int) -> dict[str, Any]:
        if job.kind is JobKind.SCAN:
            self._emit_progress(
                job.id, sequence, JobStage.SCANNING, "正在只读扫描允许的数据目录"
            )
            records = self.connector.scan()
            if job.payload.get("hash_pbix"):
                records = [
                    replace(record, sha256=self.connector.stable_sha256(record))
                    if record.extension == ".pbix" and record.purpose == "pbix_asset"
                    else record
                    for record in records
                ]
            return {
                "files": [record.to_dict() for record in records],
                "count": len(records),
                "inventory_checksum": _inventory_checksum(records),
            }

        record = FileRecord(**job.payload["file"])
        work_root = self.config.state_dir / "work" / job.id
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True, exist_ok=False)
        try:
            local_path = work_root / f"source{record.extension}"
            self._emit_progress(
                job.id,
                sequence,
                JobStage.MATERIALIZING,
                "正在以只读方式取得稳定文件",
            )
            self.connector.materialize(record, local_path)
            sequence += 1
            if job.kind is JobKind.PROFILE:
                self._emit_progress(
                    job.id, sequence, JobStage.PROFILING, "正在生成确定性结构画像"
                )
                profile = profile_file(
                    local_path,
                    purpose_hint=record.purpose,
                    sample_rows=int(job.payload.get("sample_rows", 200)),
                )
                return profile.to_dict()
            if job.kind is JobKind.RECOMPUTE:
                self._emit_progress(
                    job.id, sequence, JobStage.RECOMPUTING, "正在执行确定性重计算"
                )
                output_dir = self.config.state_dir / "artifacts"
                output_path = safe_relative_output(output_dir, job.id, ".parquet")
                output_path.unlink(missing_ok=True)
                return deterministic_recompute(
                    local_path,
                    RecomputeSpec.from_payload(job.payload["spec"]),
                    output_path,
                )
            raise ValueError(f"不支持的任务类型: {job.kind}")
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    def _emit_progress(
        self,
        job_id: str,
        sequence: int,
        stage: JobStage,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.control.queue_progress(
            job_id, sequence, stage, message, detail=detail
        )
        try:
            self.control.flush_events()
        except Exception:
            # 事件已持久化；控制面恢复后由下一轮 flush 补报。
            logger.warning("进度事件暂未发送，已保留到本地队列", exc_info=True)


def _inventory_checksum(records: list[FileRecord]) -> str:
    payload = [
        {
            "source_id": record.source_id,
            "path": record.path,
            "size": record.size,
            "mtime_utc": record.mtime_utc,
        }
        for record in sorted(records, key=lambda item: item.source_id)
    ]
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
