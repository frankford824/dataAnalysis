"""Counterfactual experiment framework."""

from commerce_harness.memory.experiment_tables import (
    COUNTERFACTUAL_RESULT_TABLE_DDL,
    MAIN_EXPERIMENT_TABLE_DDL,
)

from .metrics import compute_metrics
from .runner import (
    ExperimentProposal,
    ExperimentRecord,
    ExperimentRunError,
    ExperimentRunner,
)
from .shadow import ShadowOutcome, ShadowReconciler, compare_outcomes, outcome_digest
from .verdict import decide_verdict

__all__ = [
    "COUNTERFACTUAL_RESULT_TABLE_DDL",
    "ExperimentProposal",
    "ExperimentRecord",
    "ExperimentRunError",
    "ExperimentRunner",
    "MAIN_EXPERIMENT_TABLE_DDL",
    "ShadowOutcome",
    "ShadowReconciler",
    "compare_outcomes",
    "compute_metrics",
    "decide_verdict",
    "outcome_digest",
]
