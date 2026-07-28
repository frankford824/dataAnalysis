from __future__ import annotations

import platform
from typing import Any

import httpx

from .config import AgentConfig
from .models import AgentJob, JobStage, utc_now_iso
from .state import AgentState


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneClient:
    def __init__(
        self,
        config: AgentConfig,
        state: AgentState,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        self.state = state
        self.client = httpx.Client(
            base_url=config.control_plane_url,
            timeout=config.request_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _access_token(self) -> str | None:
        return self.config.access_token or self.state.get_secret("access_token")

    def _headers(self, enrollment: bool = False) -> dict[str, str]:
        token = (
            self.config.enrollment_token if enrollment else self._access_token()
        )
        if not token:
            kind = "注册" if enrollment else "访问"
            raise ControlPlaneError(f"缺少{kind}令牌环境变量")
        return {"Authorization": f"Bearer {token}"}

    def register(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/agents/register",
            headers=self._headers(enrollment=True),
            json={
                "name": self.config.agent_name,
                "connector": self.config.connector,
                "platform": platform.platform(),
                "capabilities": [
                    "read_only_scan",
                    "pbix_inventory",
                    "csv_xlsx_profile",
                    "polars_duckdb_recompute",
                    "persistent_progress",
                ],
                "protocol_version": "1",
            },
        )
        self._raise(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ControlPlaneError("控制面注册响应必须是对象")
        if payload.get("access_token"):
            self.state.set_secret("access_token", str(payload["access_token"]))
        if payload.get("agent_id"):
            self.state.set_secret("agent_id", str(payload["agent_id"]))
        return payload

    def heartbeat(self, status: str = "online") -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/agents/heartbeat",
            headers=self._headers(),
            json={
                "agent_id": self.state.get_secret("agent_id"),
                "status": status,
                "at": utc_now_iso(),
            },
        )
        self._raise(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ControlPlaneError("控制面心跳响应必须是对象")
        return payload

    def claim_job(self) -> AgentJob | None:
        response = self.client.post(
            "/api/v1/agent-jobs/claim",
            headers=self._headers(),
            json={
                "agent_id": self.state.get_secret("agent_id"),
                "capabilities": [
                    "scan",
                    "profile",
                    "recompute",
                ],
            },
        )
        if response.status_code == 204:
            return None
        self._raise(response)
        payload = response.json()
        if not payload:
            return None
        if not isinstance(payload, dict):
            raise ControlPlaneError("控制面任务响应必须是对象")
        return AgentJob.from_payload(payload)

    def queue_progress(
        self,
        job_id: str,
        sequence: int,
        stage: JobStage,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        payload = {
            "sequence": sequence,
            "stage": stage.value,
            "message": message,
            "detail": detail or {},
            "occurred_at": utc_now_iso(),
        }
        return self.state.queue_event(
            f"{job_id}:progress:{sequence}",
            f"/api/v1/agent-jobs/{job_id}/progress",
            payload,
        )

    def queue_terminal(
        self,
        job_id: str,
        success: bool,
        payload: dict[str, Any],
    ) -> bool:
        action = "complete" if success else "fail"
        return self.state.queue_event(
            f"{job_id}:{action}",
            f"/api/v1/agent-jobs/{job_id}/{action}",
            payload,
        )

    def flush_events(self) -> int:
        sent = 0
        for event in self.state.pending_events():
            response = self.client.post(
                event["endpoint"],
                headers={
                    **self._headers(),
                    "Idempotency-Key": event["event_key"],
                },
                json=event["payload"],
            )
            self._raise(response)
            self.state.mark_event_sent(int(event["id"]))
            sent += 1
        return sent

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:500]
        raise ControlPlaneError(
            f"控制面请求失败: {response.status_code} {detail}"
        )
