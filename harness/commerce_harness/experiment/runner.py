"""Counterfactual experiment runner.

CRITICAL: never writes to the main ``reconciliation_*`` or ``pnl_cell`` tables.
Shadow results land in the isolated ``counterfactual`` schema, and the verdict
is decided by a pure function over measured metrics.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum
from commerce_harness.memory.experiment_tables import COUNTERFACTUAL_RESULT_TABLE_DDL

from .metrics import compute_metrics
from .shadow import ShadowOutcome, compare_outcomes, outcome_digest
from .verdict import decide_verdict


@dataclass
class ExperimentProposal:
    hypothesis_kind: str
    hypothesis_json: dict[str, Any]
    proposed_by: str
    baseline_run_id: str
    scope: dict[str, Any]
    baseline_code_sha: str = ""
    baseline_input_sha256: str = ""


@dataclass
class ExperimentRecord:
    experiment_id: str
    hypothesis_kind: str
    hypothesis_json: dict[str, Any]
    proposed_by: str
    baseline_run_id: str
    shadow_run_id: str | None
    scope_json: dict[str, Any]
    baseline_code_sha: str
    shadow_code_sha: str
    baseline_input_sha256: str
    shadow_input_sha256: str
    output_sha256: str | None
    verdict: str
    verdict_reasons: list[str]
    created_at: str
    decided_at: str | None
    metrics: dict[str, dict[str, Decimal]] | None = None
    deltas: list[dict[str, Any]] | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# A shadow run takes the frozen context and returns the before/after outcomes
# of the same engine under the baseline and candidate rule sets.
ShadowRunFn = Callable[
    [dict[str, Any], dict[str, Any]],
    tuple[ShadowOutcome, ShadowOutcome],
]


class ExperimentRunError(RuntimeError):
    """Raised when an experiment cannot be executed as specified."""


class ExperimentRunner:
    """Run counterfactual experiments in isolation.

    ``shadow_run`` must return ``(before, after)`` outcomes produced by the same
    deterministic engine over the same frozen rows. There is no default: an
    experiment without a real shadow run would report a verdict it never
    measured.
    """

    def __init__(
        self,
        *,
        shadow_run: ShadowRunFn | None = None,
        code_sha: str = "",
        materiality_floor: Decimal = Decimal("500.0000"),
    ) -> None:
        self._shadow_run = shadow_run
        self._code_sha = code_sha
        self._materiality_floor = materiality_floor

    def propose(self, proposal: ExperimentProposal) -> ExperimentRecord:
        """Create a pending experiment record without running it."""
        return ExperimentRecord(
            experiment_id=str(uuid.uuid4()),
            hypothesis_kind=proposal.hypothesis_kind,
            hypothesis_json=proposal.hypothesis_json,
            proposed_by=proposal.proposed_by,
            baseline_run_id=proposal.baseline_run_id,
            shadow_run_id=None,
            scope_json=proposal.scope,
            baseline_code_sha=proposal.baseline_code_sha,
            shadow_code_sha=self._code_sha,
            baseline_input_sha256=proposal.baseline_input_sha256,
            shadow_input_sha256=proposal.baseline_input_sha256,
            output_sha256=None,
            verdict="pending",
            verdict_reasons=[],
            created_at=_now_iso(),
            decided_at=None,
        )

    def run(
        self,
        experiment: ExperimentRecord,
        *,
        context: dict[str, Any] | None = None,
        period_locked: bool = False,
    ) -> ExperimentRecord:
        """Execute the experiment: shadow run twice, measure, decide.

        The candidate side runs twice and the two digests must agree; a verdict
        that cannot be reproduced is not evidence.
        """
        if self._shadow_run is None:
            raise ExperimentRunError(
                "experiment requires a shadow_run implementation; "
                "refusing to report a verdict without a measured shadow run"
            )
        ctx = context or {}

        before, after = self._shadow_run(experiment.hypothesis_json, ctx)
        _, after_repeat = self._shadow_run(experiment.hypothesis_json, ctx)
        checksum_stable = outcome_digest(after) == outcome_digest(after_repeat)

        after_summary, deltas = compare_outcomes(
            before, after, materiality_floor=self._materiality_floor,
        )
        metrics = compute_metrics(before.summary, after_summary)

        verdict, reasons = decide_verdict(
            metrics,
            period_locked=period_locked,
            checksum_stable=checksum_stable,
        )

        experiment.shadow_run_id = f"shadow_{experiment.experiment_id}"
        experiment.output_sha256 = deterministic_checksum({
            "experiment_id": experiment.experiment_id,
            "before": outcome_digest(before),
            "after": outcome_digest(after),
        })
        experiment.verdict = verdict
        experiment.verdict_reasons = reasons
        experiment.decided_at = _now_iso()
        experiment.metrics = metrics
        experiment.deltas = deltas

        return experiment

    @staticmethod
    def counterfactual_ddl() -> list[str]:
        """Return DDL for counterfactual result tables."""
        return list(COUNTERFACTUAL_RESULT_TABLE_DDL)
