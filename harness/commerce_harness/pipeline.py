"""Single-command pipeline: scan → freeze → profile → normalize → recon.

Replaces the nine-step CLI surface for day-to-day use. Individual
subcommands remain for debugging.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .auto_compute import refresh_target_plan
from .config import HarnessConfig
from .freeze import freeze_candidates
from .inventory import scan_inventory
from .phase_a import adjudicate_workspace, normalize_workspace, reconcile_period
from .profiling import profile_snapshots
from .role import ROLE_ENV, reads_customer_sources
from .workbench import WorkbenchPaths

__all__ = ["ROLE_ENV", "RunResult", "reads_customer_sources", "run_pipeline"]

_SOURCE_SKIPPED = {
    "skipped": True,
    "reason": (
        "core 角色不读取客户文件；扫描与冻结由 edge 完成后通过 HTTP 上传"
    ),
}


@dataclass(frozen=True, slots=True)
class RunResult:
    inventory: dict[str, Any]
    freeze: dict[str, Any]
    profile: dict[str, Any]
    normalize: dict[str, Any]
    adjudication: dict[str, Any]
    reconcile: list[dict[str, Any]]
    failures: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "inventory": self.inventory,
            "freeze": self.freeze,
            "profile": self.profile,
            "normalize": self.normalize,
            "adjudication": self.adjudication,
            "reconcile": self.reconcile,
            "failures": list(self.failures),
        }


def run_pipeline(
    config: HarnessConfig,
    workbench: WorkbenchPaths,
    *,
    period: str | None = None,
    store_id: str | None = None,
    read_sources: bool | None = None,
) -> RunResult:
    if read_sources is None:
        read_sources = reads_customer_sources()
    if read_sources:
        inventory = asdict(scan_inventory(config, workbench))
        inventory["path"] = str(inventory.get("path", ""))
        freeze = asdict(freeze_candidates(config, workbench))
    else:
        inventory = dict(_SOURCE_SKIPPED)
        freeze = dict(_SOURCE_SKIPPED)
    profile = asdict(profile_snapshots(workbench))
    periods = (
        (period,)
        if period
        else tuple(config.source.scope.resolved_periods())
    )
    normalize = asdict(
        normalize_workspace(
            workbench,
            periods=periods,
            store_id=store_id,
        )
    )
    adjudication = adjudicate_workspace(
        workbench,
        mode=config.reconciliation.mode,
    ).to_dict()
    refresh_target_plan(config, workbench)
    reconcile_results: list[dict[str, Any]] = []
    failures: list[str] = []
    for token in periods:
        try:
            result = reconcile_period(
                workbench,
                period_token=token,
                store_id=store_id,
                mode=config.reconciliation.mode,
            )
            reconcile_results.append(asdict(result))
        except Exception as exc:  # noqa: BLE001 - one bad period must not hide the rest
            reconcile_results.append(
                {
                    "period": token,
                    "store_id": store_id,
                    "error": str(exc),
                }
            )
            failures.append(f"{token}: {exc}")
    return RunResult(
        inventory=inventory,
        freeze=freeze,
        profile=profile,
        normalize=normalize,
        adjudication=adjudication,
        reconcile=reconcile_results,
        failures=tuple(failures),
    )
