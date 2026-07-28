import os
import time

from finance_agent.config import AgentConfig
from finance_agent.connectors.local_fixture import LocalFixtureConnector
from finance_agent.models import AgentJob, JobKind
from finance_agent.state import AgentState
from finance_agent.worker import AgentWorker


class FakeControl:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.terminals = []
        self.progress = []

    def flush_events(self):
        return 0

    def heartbeat(self):
        return {"ok": True}

    def claim_job(self):
        return self.jobs.pop(0) if self.jobs else None

    def queue_progress(self, job_id, sequence, stage, message, detail=None):
        self.progress.append((job_id, sequence, stage.value, message))
        return True

    def queue_terminal(self, job_id, success, payload):
        self.terminals.append((job_id, success, payload))
        return True


def test_worker_replays_same_idempotency_key_without_recompute(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "orders.csv"
    source.write_text("订单号,销售额\nA,1.0000\n", encoding="utf-8")
    old = time.time() - 100
    os.utime(source, (old, old))
    config = AgentConfig(
        connector="local_fixture",
        fixture_root=root,
        state_dir=tmp_path / "state",
        stable_for_seconds=0,
    )
    connector = LocalFixtureConnector(config)
    state = AgentState(config.state_dir)
    file_payload = connector.scan()[0].to_dict()
    jobs = [
        AgentJob(
            id="job-1",
            kind=JobKind.PROFILE,
            payload={"file": file_payload},
            idempotency_key="profile:source-1",
        ),
        AgentJob(
            id="job-2",
            kind=JobKind.PROFILE,
            payload={"file": file_payload},
            idempotency_key="profile:source-1",
        ),
    ]
    control = FakeControl(jobs)
    worker = AgentWorker(config, connector, state, control)
    try:
        first = worker.run_once()
        source.unlink()
        second = worker.run_once()
    finally:
        state.close()

    assert first == second
    assert control.terminals[-1][2]["replayed"] is True


def test_worker_scan_returns_inventory_checksum(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "orders.csv"
    source.write_text("订单号,销售额\nA,1.0000\n", encoding="utf-8")
    config = AgentConfig(
        connector="local_fixture",
        fixture_root=root,
        state_dir=tmp_path / "state",
        stable_for_seconds=0,
    )
    state = AgentState(config.state_dir)
    control = FakeControl(
        [
            AgentJob(
                id="scan-1",
                kind=JobKind.SCAN,
                payload={},
                idempotency_key="scan:fixture",
            )
        ]
    )
    try:
        result = AgentWorker(
            config, LocalFixtureConnector(config), state, control
        ).run_once()
    finally:
        state.close()
    assert result["count"] == 1
    assert len(result["inventory_checksum"]) == 64
    assert control.terminals[0][1] is True


def test_worker_scan_can_hash_pbix_on_explicit_job(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "model.pbix"
    source.write_bytes(b"pbix metadata fixture")
    config = AgentConfig(
        connector="local_fixture",
        fixture_root=root,
        state_dir=tmp_path / "state",
        stable_for_seconds=0,
    )
    state = AgentState(config.state_dir)
    control = FakeControl(
        [
            AgentJob(
                id="scan-hash",
                kind=JobKind.SCAN,
                payload={"hash_pbix": True},
                idempotency_key="scan:hash",
            )
        ]
    )
    connector = LocalFixtureConnector(config)
    # fixture 根文件默认 purpose=fixture；模拟控制面明确登记的 PBIX 资产范围。
    original_scan = connector.scan
    from dataclasses import replace

    connector.scan = lambda: [
        replace(item, purpose="pbix_asset") for item in original_scan()
    ]
    try:
        result = AgentWorker(config, connector, state, control).run_once()
    finally:
        state.close()
    assert len(result["files"][0]["sha256"]) == 64


def test_worker_recompute_writes_artifact_outside_source(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "orders.csv"
    source.write_text(
        "订单号,销售额\nA,1.0000\nB,2.0000\n", encoding="utf-8"
    )
    config = AgentConfig(
        connector="local_fixture",
        fixture_root=root,
        state_dir=tmp_path / "state",
        stable_for_seconds=0,
    )
    connector = LocalFixtureConnector(config)
    record = connector.scan()[0]
    state = AgentState(config.state_dir)
    control = FakeControl(
        [
            AgentJob(
                id="recompute-1",
                kind=JobKind.RECOMPUTE,
                payload={
                    "file": record.to_dict(),
                    "spec": {
                        "business_key": "订单号",
                        "amount_columns": ["销售额"],
                    },
                },
                idempotency_key="recompute:1",
            )
        ]
    )
    try:
        result = AgentWorker(config, connector, state, control).run_once()
    finally:
        state.close()
    assert result["totals"]["销售额"] == "3.0000"
    assert source.exists()
    assert (config.state_dir / "artifacts" / "recompute-1.parquet").exists()
