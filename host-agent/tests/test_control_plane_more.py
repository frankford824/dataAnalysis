import httpx
import pytest

from finance_agent.config import AgentConfig
from finance_agent.control_plane import ControlPlaneClient, ControlPlaneError
from finance_agent.models import JobKind
from finance_agent.state import AgentState


def test_heartbeat_claim_and_terminal_events(tmp_path, monkeypatch):
    claimed = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"status": "online"})
        if request.url.path.endswith("/claim"):
            claimed["count"] += 1
            if claimed["count"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": "job-1",
                        "kind": "scan",
                        "payload": {},
                        "idempotency_key": "scan:1",
                    },
                )
            return httpx.Response(204)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("FINANCE_AGENT_ACCESS_TOKEN", "from-env")
    state = AgentState(tmp_path / "state")
    state.set_secret("agent_id", "agent-1")
    client = ControlPlaneClient(
        AgentConfig(state_dir=tmp_path / "state"),
        state,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.heartbeat()["status"] == "online"
        job = client.claim_job()
        assert job and job.kind is JobKind.SCAN
        assert client.claim_job() is None
        assert client.queue_terminal("job-1", True, {"result": {"count": 0}})
        assert client.queue_terminal("job-2", False, {"message": "bad"})
        assert client.flush_events() == 2
    finally:
        client.close()
        state.close()


def test_missing_token_and_http_error_are_explicit(tmp_path):
    state = AgentState(tmp_path / "state")
    client = ControlPlaneClient(
        AgentConfig(state_dir=tmp_path / "state"),
        state,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, text="offline")
        ),
    )
    try:
        with pytest.raises(ControlPlaneError, match="访问令牌"):
            client.heartbeat()
        state.set_secret("access_token", "stored")
        with pytest.raises(ControlPlaneError, match="503"):
            client.heartbeat()
    finally:
        client.close()
        state.close()
