from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig

WORKSPACE_MARKER = ".fa-workbench.json"


@dataclass(frozen=True)
class WorkbenchPaths:
    root: Path
    database: Path
    snapshots: Path
    normalized: Path
    reports: Path
    llm_logs: Path
    locks: Path


def paths(config: HarnessConfig) -> WorkbenchPaths:
    root = config.workspace.root
    return WorkbenchPaths(
        root=root,
        database=root / config.workspace.database,
        snapshots=root / config.workspace.snapshots,
        normalized=root / config.workspace.normalized,
        reports=root / config.workspace.reports,
        llm_logs=root / config.workspace.llm_logs,
        locks=root / "locks",
    )


def initialize(config: HarnessConfig) -> WorkbenchPaths:
    workbench = paths(config)
    if any((parent / ".git").exists() for parent in (workbench.root, *workbench.root.parents)):
        raise ValueError("工作台不能位于 Git 仓库内")
    for directory in (
        workbench.root,
        workbench.snapshots,
        workbench.normalized,
        workbench.reports,
        workbench.llm_logs,
        workbench.locks,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    marker = workbench.root / WORKSPACE_MARKER
    payload = {
        "format": 1,
        "purpose": "commerce-reconciliation-harness",
        "customer_data_must_not_enter_git": True,
    }
    marker.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return workbench


def require_initialized(config: HarnessConfig) -> WorkbenchPaths:
    workbench = paths(config)
    marker = workbench.root / WORKSPACE_MARKER
    if not marker.is_file():
        raise RuntimeError(f"工作台未初始化：{workbench.root}")
    return workbench
