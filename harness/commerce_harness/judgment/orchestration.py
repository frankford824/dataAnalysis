from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskCategory(StrEnum):
    STRUCTURE_IDENTIFICATION = "structure_identification"
    FIELD_MAPPING = "field_mapping"
    LINKAGE_CANDIDATE = "linkage_candidate"
    DIFFERENCE_EXPLANATION = "difference_explanation"
    RULE_DRAFT = "rule_draft"


class ModelRole(StrEnum):
    PROPOSER = "proposer"
    REVIEWER = "reviewer"


class InputExposureLevel(StrEnum):
    METADATA_ONLY = "metadata_only"
    REDACTED_SAMPLE = "redacted_sample"
    REDACTED_EVIDENCE_WINDOW = "redacted_evidence_window"


class EvidenceRequirement(StrEnum):
    PROFILE_CITATION = "profile_citation"
    ROW_CITATION = "row_citation"
    ROW_AND_RULE_CITATION = "row_and_rule_citation"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceGuardStatus(StrEnum):
    NOT_RUN = "not_run"
    VERIFIED = "verified"
    FAILED = "failed"


class RegressionStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


class ReleaseDecision(StrEnum):
    SUGGEST_ONLY = "suggest_only"
    REVERSIBLE_APPLY = "reversible_apply"
    GOVERNANCE_REVIEW = "governance_review"


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    task_category: TaskCategory
    roles: tuple[ModelRole, ...]
    input_exposure: InputExposureLevel
    cloud_models_allowed: bool
    redaction_required: bool
    evidence_requirement: EvidenceRequirement
    max_row_window: int
    risk_level: RiskLevel
    reversible_apply_allowed: bool
    minimum_regression_periods: int = 2
    minimum_regression_cases: int = 20
    may_write_amounts: bool = field(default=False, init=False)
    may_write_ledger: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if set(self.roles) != {ModelRole.PROPOSER, ModelRole.REVIEWER}:
            raise ValueError("every task requires proposer and reviewer roles")
        if len(self.roles) != 2:
            raise ValueError("roles must contain proposer and reviewer exactly once")
        if self.max_row_window < 0:
            raise ValueError("max_row_window cannot be negative")
        if self.minimum_regression_periods < 1:
            raise ValueError("minimum_regression_periods must be positive")
        if self.minimum_regression_cases < 1:
            raise ValueError("minimum_regression_cases must be positive")
        if self.risk_level is RiskLevel.HIGH and self.reversible_apply_allowed:
            raise ValueError("high-risk tasks cannot be configured for reversible application")


_MODEL_ROLES = (ModelRole.PROPOSER, ModelRole.REVIEWER)

_TASK_POLICIES = {
    TaskCategory.STRUCTURE_IDENTIFICATION: TaskPolicy(
        task_category=TaskCategory.STRUCTURE_IDENTIFICATION,
        roles=_MODEL_ROLES,
        input_exposure=InputExposureLevel.METADATA_ONLY,
        cloud_models_allowed=True,
        redaction_required=True,
        evidence_requirement=EvidenceRequirement.PROFILE_CITATION,
        max_row_window=0,
        risk_level=RiskLevel.LOW,
        reversible_apply_allowed=True,
    ),
    TaskCategory.FIELD_MAPPING: TaskPolicy(
        task_category=TaskCategory.FIELD_MAPPING,
        roles=_MODEL_ROLES,
        input_exposure=InputExposureLevel.REDACTED_SAMPLE,
        cloud_models_allowed=True,
        redaction_required=True,
        evidence_requirement=EvidenceRequirement.ROW_CITATION,
        max_row_window=20,
        risk_level=RiskLevel.MEDIUM,
        reversible_apply_allowed=True,
    ),
    TaskCategory.LINKAGE_CANDIDATE: TaskPolicy(
        task_category=TaskCategory.LINKAGE_CANDIDATE,
        roles=_MODEL_ROLES,
        input_exposure=InputExposureLevel.REDACTED_EVIDENCE_WINDOW,
        cloud_models_allowed=False,
        redaction_required=True,
        evidence_requirement=EvidenceRequirement.ROW_AND_RULE_CITATION,
        max_row_window=50,
        risk_level=RiskLevel.HIGH,
        reversible_apply_allowed=False,
    ),
    TaskCategory.DIFFERENCE_EXPLANATION: TaskPolicy(
        task_category=TaskCategory.DIFFERENCE_EXPLANATION,
        roles=_MODEL_ROLES,
        input_exposure=InputExposureLevel.REDACTED_EVIDENCE_WINDOW,
        cloud_models_allowed=True,
        redaction_required=True,
        evidence_requirement=EvidenceRequirement.ROW_AND_RULE_CITATION,
        max_row_window=100,
        risk_level=RiskLevel.HIGH,
        reversible_apply_allowed=False,
    ),
    TaskCategory.RULE_DRAFT: TaskPolicy(
        task_category=TaskCategory.RULE_DRAFT,
        roles=_MODEL_ROLES,
        input_exposure=InputExposureLevel.REDACTED_SAMPLE,
        cloud_models_allowed=True,
        redaction_required=True,
        evidence_requirement=EvidenceRequirement.ROW_AND_RULE_CITATION,
        max_row_window=20,
        risk_level=RiskLevel.HIGH,
        reversible_apply_allowed=False,
    ),
}


def policy_for(task_category: TaskCategory | str) -> TaskPolicy:
    return _TASK_POLICIES[TaskCategory(task_category)]


def task_policies() -> tuple[TaskPolicy, ...]:
    return tuple(_TASK_POLICIES[category] for category in TaskCategory)


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    task_category: TaskCategory | str
    role: ModelRole | str
    uses_cloud_model: bool
    payload_redacted: bool
    row_window: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_category", TaskCategory(self.task_category))
        object.__setattr__(self, "role", ModelRole(self.role))
        if self.row_window < 0:
            raise ValueError("row_window cannot be negative")


@dataclass(frozen=True, slots=True)
class InvocationDecision:
    allowed: bool
    reasons: tuple[str, ...]
    policy: TaskPolicy
    may_write_amounts: bool = field(default=False, init=False)
    may_write_ledger: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class ModelConclusion:
    task_category: TaskCategory | str
    role: ModelRole | str
    model_id: str
    conclusion_key: str
    cites_required_evidence: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_category", TaskCategory(self.task_category))
        object.__setattr__(self, "role", ModelRole(self.role))
        if not self.model_id.strip():
            raise ValueError("model_id is required")
        if not self.conclusion_key.strip():
            raise ValueError("conclusion_key is required")


@dataclass(frozen=True, slots=True)
class RegressionResult:
    status: RegressionStatus | str
    tested_periods: int
    tested_cases: int
    major_error_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RegressionStatus(self.status))
        if min(self.tested_periods, self.tested_cases, self.major_error_count) < 0:
            raise ValueError("regression counts cannot be negative")


@dataclass(frozen=True, slots=True)
class ReleaseAssessment:
    task_category: TaskCategory
    decision: ReleaseDecision
    models_agree: bool
    independent_models: bool
    evidence_verified: bool
    regression_passed: bool
    reasons: tuple[str, ...]
    may_write_amounts: bool = field(default=False, init=False)
    may_write_ledger: bool = field(default=False, init=False)


class ModelOrchestrator:
    """Pure policy engine; it never calls a model and exposes no financial write path."""

    def authorize_invocation(self, request: InvocationRequest) -> InvocationDecision:
        policy = policy_for(request.task_category)
        reasons: list[str] = []
        if request.role not in policy.roles:
            reasons.append("role is not allowed for this task")
        if request.uses_cloud_model and not policy.cloud_models_allowed:
            reasons.append("cloud models are not allowed for this task")
        if policy.redaction_required and not request.payload_redacted:
            reasons.append("payload must be redacted before model exposure")
        if request.row_window > policy.max_row_window:
            reasons.append("row window exceeds the task policy")
        return InvocationDecision(
            allowed=not reasons,
            reasons=tuple(reasons),
            policy=policy,
        )

    def decide_release(
        self,
        proposer: ModelConclusion,
        reviewer: ModelConclusion,
        *,
        evidence_guard_status: EvidenceGuardStatus | str,
        regression: RegressionResult,
    ) -> ReleaseAssessment:
        self._validate_conclusion_pair(proposer, reviewer)
        task_category = TaskCategory(proposer.task_category)
        policy = policy_for(task_category)
        guard_status = EvidenceGuardStatus(evidence_guard_status)
        independent_models = proposer.model_id != reviewer.model_id
        models_agree = proposer.conclusion_key == reviewer.conclusion_key
        evidence_verified = (
            guard_status is EvidenceGuardStatus.VERIFIED
            and proposer.cites_required_evidence
            and reviewer.cites_required_evidence
        )
        regression_passed = (
            regression.status is RegressionStatus.PASSED
            and regression.tested_periods >= policy.minimum_regression_periods
            and regression.tested_cases >= policy.minimum_regression_cases
            and regression.major_error_count == 0
        )

        reasons: list[str] = []
        if not independent_models:
            reasons.append("proposer and reviewer must be independent models")
        if not models_agree:
            reasons.append("model conclusions disagree")
        if guard_status is EvidenceGuardStatus.NOT_RUN:
            reasons.append("evidence guard has not run")
        elif guard_status is EvidenceGuardStatus.FAILED:
            reasons.append("evidence guard rejected at least one citation")
        elif not evidence_verified:
            reasons.append("both conclusions must cite the required evidence")
        if regression.status is RegressionStatus.NOT_RUN:
            reasons.append("regression has not run")
        elif regression.status is RegressionStatus.FAILED:
            reasons.append("regression failed")
        elif regression.major_error_count:
            reasons.append("regression observed a major error")
        elif regression.tested_periods < policy.minimum_regression_periods:
            reasons.append("regression covers too few periods")
        elif regression.tested_cases < policy.minimum_regression_cases:
            reasons.append("regression covers too few cases")

        release_ready = (
            independent_models
            and models_agree
            and evidence_verified
            and regression_passed
        )
        if not release_ready:
            decision = ReleaseDecision.SUGGEST_ONLY
        elif policy.reversible_apply_allowed:
            decision = ReleaseDecision.REVERSIBLE_APPLY
            reasons.append("non-financial candidate may be applied reversibly")
        else:
            decision = ReleaseDecision.GOVERNANCE_REVIEW
            reasons.append("risk policy requires governance review")

        return ReleaseAssessment(
            task_category=task_category,
            decision=decision,
            models_agree=models_agree,
            independent_models=independent_models,
            evidence_verified=evidence_verified,
            regression_passed=regression_passed,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _validate_conclusion_pair(
        proposer: ModelConclusion,
        reviewer: ModelConclusion,
    ) -> None:
        if proposer.task_category != reviewer.task_category:
            raise ValueError("proposer and reviewer must target the same task")
        if proposer.role is not ModelRole.PROPOSER:
            raise ValueError("the first conclusion must use the proposer role")
        if reviewer.role is not ModelRole.REVIEWER:
            raise ValueError("the second conclusion must use the reviewer role")
