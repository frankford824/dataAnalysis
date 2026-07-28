import json

import httpx

from finance_agent.config import AgentConfig
from finance_agent.control_plane import ControlPlaneClient
from finance_agent.models import JobStage
from finance_agent.state import AgentState


def test_registration_stores_secret_and_progress_flush_is_idempotent(
    tmp_path, monkeypatch
):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.url.path,
                request.headers.get("idempotency-key"),
                json.loads(request.content or b"{}"),
            )
        )
        if request.url.path.endswith("/register"):
            return httpx.Response(
                200, json={"agent_id": "agent-1", "access_token": "secret-access"}
            )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("FINANCE_AGENT_ENROLLMENT_TOKEN", "enroll-once")
    state = AgentState(tmp_path / "state")
    client = ControlPlaneClient(
        AgentConfig(state_dir=tmp_path / "state"),
        state,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.register()
        assert result["agent_id"] == "agent-1"
        assert state.get_secret("access_token") == "secret-access"

        assert client.queue_progress(
            "job-1", 1, JobStage.SCANNING, "扫描"
        )
        assert not client.queue_progress(
            "job-1", 1, JobStage.SCANNING, "重复事件"
        )
        assert client.flush_events() == 1
        assert client.flush_events() == 0
    finally:
        client.close()
        state.close()

    progress_calls = [item for item in calls if item[0].endswith("/progress")]
    assert progress_calls == [
        (
            "/api/v1/agent-jobs/job-1/progress",
            "job-1:progress:1",
            {
                "sequence": 1,
                "stage": "scanning",
                "message": "扫描",
                "detail": {},
                "occurred_at": progress_calls[0][2]["occurred_at"],
            },
        )
    ]


def test_completed_job_result_is_insert_only(tmp_path):
    state = AgentState(tmp_path / "state")
    try:
        state.mark_completed("same", "job-1", {"total": "1.0000"})
        state.mark_completed("same", "job-2", {"total": "999.0000"})
        assert state.completed_result("same") == {"total": "1.0000"}
    finally:
        state.close()
