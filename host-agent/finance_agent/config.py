from __future__ import annotations

import os
import shutil
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ALLOWED_EXTENSIONS = (".pbix", ".csv", ".xlsx", ".xls", ".xlsm")
DEFAULT_SENSITIVE_EXTENSIONS = (
    ".key",
    ".pem",
    ".pfx",
    ".p12",
    ".kdbx",
    ".db",
    ".sqlite",
    ".bak",
    ".zip",
)
DEFAULT_EXCLUDED_FRAGMENTS = (
    r"\工资",
    r"\汇总",
    r"\学习",
    r"\测试",
    r"\WeChat Files",
    r"\凭证",
    r"\早期数据",
    r"\$RECYCLE.BIN",
)


@dataclass(frozen=True)
class SourceRoot:
    path: str
    purpose: str
    extensions: tuple[str, ...] = DEFAULT_ALLOWED_EXTENSIONS
    allow_excluded_fragments: bool = False


@dataclass(frozen=True)
class AgentConfig:
    control_plane_url: str = "http://127.0.0.1:8000"
    agent_name: str = field(default_factory=socket.gethostname)
    connector: str = "ssh_windows"
    ssh_alias: str = "finance-win-ro"
    ssh_binary: str = "auto"
    poll_seconds: float = 5.0
    heartbeat_seconds: float = 20.0
    request_timeout_seconds: float = 30.0
    materialize_timeout_seconds: float = 1800.0
    stable_for_seconds: int = 600
    max_materialize_bytes: int = 2 * 1024 * 1024 * 1024
    state_dir: Path = Path(".finance-agent")
    fixture_root: Path | None = None
    source_roots: tuple[SourceRoot, ...] = ()
    recent_shortcuts_root: str = (
        r"C:\Users\finance_reader\AppData\Roaming\Microsoft\Windows\Recent"
    )
    excluded_fragments: tuple[str, ...] = DEFAULT_EXCLUDED_FRAGMENTS
    sensitive_extensions: tuple[str, ...] = DEFAULT_SENSITIVE_EXTENSIONS

    @property
    def enrollment_token(self) -> str | None:
        return os.getenv("FINANCE_AGENT_ENROLLMENT_TOKEN")

    @property
    def access_token(self) -> str | None:
        return os.getenv("FINANCE_AGENT_ACCESS_TOKEN")


DEFAULT_SOURCE_ROOTS: tuple[SourceRoot, ...] = ()


def _tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(str(item) for item in value)


def load_config(path: str | Path | None = None) -> AgentConfig:
    raw: dict[str, Any] = {}
    if path:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)

    agent = raw.get("agent", {})
    windows = raw.get("windows", {})
    safety = raw.get("safety", {})
    roots_raw = raw.get("sources", [])
    roots = tuple(
        SourceRoot(
            path=str(item["path"]),
            purpose=str(item["purpose"]),
            extensions=_tuple(item.get("extensions"), DEFAULT_ALLOWED_EXTENSIONS),
            allow_excluded_fragments=bool(
                item.get("allow_excluded_fragments", False)
            ),
        )
        for item in roots_raw
    )
    if not roots:
        roots = DEFAULT_SOURCE_ROOTS

    fixture_root = agent.get("fixture_root")
    return AgentConfig(
        control_plane_url=str(
            agent.get("control_plane_url", "http://127.0.0.1:8000")
        ).rstrip("/"),
        agent_name=str(agent.get("name") or socket.gethostname()),
        connector=str(agent.get("connector", "ssh_windows")),
        ssh_alias=str(windows.get("ssh_alias", "finance-win-ro")),
        ssh_binary=str(windows.get("ssh_binary", "auto")),
        poll_seconds=float(agent.get("poll_seconds", 5)),
        heartbeat_seconds=float(agent.get("heartbeat_seconds", 20)),
        request_timeout_seconds=float(agent.get("request_timeout_seconds", 30)),
        materialize_timeout_seconds=float(
            agent.get("materialize_timeout_seconds", 1800)
        ),
        stable_for_seconds=int(safety.get("stable_for_seconds", 600)),
        max_materialize_bytes=int(
            safety.get("max_materialize_bytes", 2 * 1024 * 1024 * 1024)
        ),
        state_dir=Path(agent.get("state_dir", ".finance-agent")).expanduser(),
        fixture_root=Path(fixture_root).expanduser() if fixture_root else None,
        source_roots=roots,
        recent_shortcuts_root=str(
            windows.get(
                "recent_shortcuts_root",
                r"C:\Users\finance_reader\AppData\Roaming\Microsoft\Windows\Recent",
            )
        ),
        excluded_fragments=_tuple(
            safety.get("excluded_fragments"), DEFAULT_EXCLUDED_FRAGMENTS
        ),
        sensitive_extensions=_tuple(
            safety.get("sensitive_extensions"), DEFAULT_SENSITIVE_EXTENSIONS
        ),
    )


def resolve_ssh_binary(configured: str) -> str:
    if configured != "auto":
        return configured
    windows_ssh = Path("/mnt/c/Windows/System32/OpenSSH/ssh.exe")
    if os.name != "nt" and windows_ssh.exists():
        return str(windows_ssh)
    return shutil.which("ssh") or "ssh"
