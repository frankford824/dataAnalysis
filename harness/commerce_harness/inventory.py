from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import HarnessConfig
from .workbench import WorkbenchPaths


@dataclass(frozen=True, slots=True)
class InventoryResult:
    path: Path
    sha256: str
    record_count: int
    candidate_count: int
    offline_count: int


def _connector(config: HarnessConfig) -> Any:
    try:
        from finance_agent.config import (
            DEFAULT_SENSITIVE_EXTENSIONS,
            AgentConfig,
            SourceRoot,
        )
        from finance_agent.connectors.windows_ssh import WindowsSshConnector
    except ImportError as exc:
        raise RuntimeError("finance-win 连接器不可用；请将 host-agent 加入 PYTHONPATH") from exc

    configured_roots = config.source.roots
    if not configured_roots:
        raise ValueError("必须在仓库外配置至少一个 finance-win 只读来源目录")
    roots = tuple(
        SourceRoot(
            item.path,
            item.purpose,
            tuple(item.extensions) if item.extensions else (),
            item.purpose in {"employee_master", "performance_reference"},
        )
        for item in configured_roots
    )
    return WindowsSshConnector(
        AgentConfig(
            ssh_alias=config.source.ssh_alias,
            stable_for_seconds=config.source.stable_for_seconds,
            max_materialize_bytes=config.source.max_file_bytes,
            source_roots=roots,
            sensitive_extensions=tuple(
                value
                for value in DEFAULT_SENSITIVE_EXTENSIONS
                if not (config.source.allow_zip_archives and value.casefold() == ".zip")
            ),
            recent_shortcuts_root=(
                r"C:\Users\finance_harness_ro\AppData\Roaming"
                r"\Microsoft\Windows\Recent"
            ),
        )
    )


def scan_inventory(
    config: HarnessConfig,
    workbench: WorkbenchPaths,
) -> InventoryResult:
    connector = _connector(config)
    records, issues = connector.scan_detailed()
    periods = tuple(
        value.casefold() for value in config.source.scope.resolved_periods()
    )
    scope_years = {
        f"20{period[:2]}"
        for period in periods
        if len(period) == 4 and period.isdigit()
    }
    shops = tuple(value.casefold() for value in config.source.scope.bound_shops)

    def is_within_allowed_root(record: Any) -> bool:
        path = str(PureWindowsPath(str(record.path))).casefold().rstrip("\\")
        for root in config.source.roots:
            if root.purpose != record.purpose:
                continue
            allowed = str(PureWindowsPath(root.path)).casefold().rstrip("\\")
            if path == allowed or path.startswith(f"{allowed}\\"):
                return True
        return False

    def is_candidate(record: Any) -> bool:
        path = record.path.casefold()
        annual_match = any(
            marker in path
            for year in scope_years
            for marker in (
                f"\\{year}\\",
                f"{year}年",
                f"{year[2:]}年",
            )
        )
        period_match = (
            not periods
            or any(period in path for period in periods)
            or annual_match
        )
        shop_match = bool(shops) and any(shop in path for shop in shops)
        if config.source.scope.include_all_discovered:
            shop_match = is_within_allowed_root(record)
        if record.purpose == "pbix_asset":
            return shop_match
        if record.purpose in {
            "rule_corpus",
            "employee_master",
            "responsibility_corpus",
            "performance_reference",
        }:
            # Formula/work-log corpora are governance inputs, not period data.
            # They are frozen read-only regardless of shop/month naming and
            # are never sent into normalization as financial facts.
            return True
        return period_match and shop_match

    candidates = [record for record in records if is_candidate(record)]
    payload = {
        "format": 1,
        "connector": "finance_win_ssh",
        "ssh_alias": config.source.ssh_alias,
        "read_only_required": True,
        "scanned_at": datetime.now(UTC).isoformat(),
        "scope": {
            "shop": config.source.scope.shop,
            "shops": config.source.scope.shops,
            "include_all_discovered": (config.source.scope.include_all_discovered),
            "periods": list(periods),
            "start_month": config.source.scope.start_month,
            "through_current_month": config.source.scope.through_current_month,
        },
        "counts": {
            "all": len(records),
            "candidates": len(candidates),
            "by_purpose": dict(Counter(item.purpose for item in records)),
            "candidate_by_purpose": dict(Counter(item.purpose for item in candidates)),
        },
        "records": [item.to_dict() for item in records],
        "issues": [item.to_dict() for item in issues],
        "candidate_source_ids": [item.source_id for item in candidates],
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(encoded).hexdigest()
    target = workbench.reports / "source-inventory.json"
    target.write_bytes(encoded)
    return InventoryResult(
        path=target,
        sha256=digest,
        record_count=len(records),
        candidate_count=len(candidates),
        offline_count=sum(
            1
            for item in issues
            if {value.casefold() for value in item.attributes}
            & {
                "offline",
                "recallonopen",
                "unpinned",
                "recallondataaccess",
            }
        ),
    )
