from __future__ import annotations

import pytest

from commerce_harness.judgment.orchestration import (
    EvidenceGuardStatus,
    EvidenceRequirement,
    InputExposureLevel,
    InvocationRequest,
    ModelConclusion,
    ModelOrchestrator,
    ModelRole,
    RegressionResult,
    RegressionStatus,
    ReleaseDecision,
    RiskLevel,
    TaskCategory,
    TaskPolicy,
    policy_for,
    task_policies,
)


def _policy(**overrides: object) -> TaskPolicy:
    values: dict[str, object] = {
        "task_category": TaskCategory.FIELD_MAPPING,
        "roles": (ModelRole.PROPOSER, ModelRole.REVIEWER),
        "input_exposure": InputExposureLevel.REDACTED_SAMPLE,
        "cloud_models_allowed": True,
        "redaction_required": True,
        "evidence_requirement": EvidenceRequirement.ROW_CITATION,
        "max_row_window": 20,
        "risk_level": RiskLevel.MEDIUM,
        "reversible_apply_allowed": True,
        "minimum_regression_periods": 2,
        "minimum_regression_cases": 20,
    }
    values.update(overrides)
    return TaskPolicy(**values)  # type: ignore[arg-type]


def test_invalid_task_policies_fail_closed() -> None:
    with pytest.raises(ValueError, match="proposer and reviewer"):
        _policy(roles=(ModelRole.PROPOSER,))
    with pytest.raises(ValueError, match="exactly once"):
        _policy(
            roles=(ModelRole.PROPOSER, ModelRole.REVIEWER, ModelRole.REVIEWER)
        )
    with pytest.raises(ValueError, match="row_window"):
        _policy(max_row_window=-1)
    with pytest.raises(ValueError, match="periods"):
        _policy(minimum_regression_periods=0)
    with pytest.raises(ValueError, match="cases"):
        _policy(minimum_regression_cases=0)
    with pytest.raises(ValueError, match="high-risk"):
        _policy(risk_level=RiskLevel.HIGH, reversible_apply_allowed=True)


def _conclusion(
    role: ModelRole,
    *,
    task: TaskCategory = TaskCategory.FIELD_MAPPING,
    model_id: str | None = None,
    conclusion_key: str = "map:order_id",
    cites_evidence: bool = True,
) -> ModelConclusion:
    return ModelConclusion(
        task_category=task,
        role=role,
        model_id=model_id or f"{role.value}-model",
        conclusion_key=conclusion_key,
        cites_required_evidence=cites_evidence,
    )


def _passing_regression() -> RegressionResult:
    return RegressionResult(
        status=RegressionStatus.PASSED,
        tested_periods=2,
        tested_cases=20,
    )


def test_policy_matrix_is_complete_and_financial_writes_are_always_forbidden() -> None:
    policies = task_policies()

    assert {policy.task_category for policy in policies} == set(TaskCategory)
    assert all(policy.roles == (ModelRole.PROPOSER, ModelRole.REVIEWER) for policy in policies)
    assert all(policy.may_write_amounts is False for policy in policies)
    assert all(policy.may_write_ledger is False for policy in policies)

    assert policy_for(TaskCategory.STRUCTURE_IDENTIFICATION).input_exposure is (
        InputExposureLevel.METADATA_ONLY
    )
    assert policy_for(TaskCategory.FIELD_MAPPING).evidence_requirement is (
        EvidenceRequirement.ROW_CITATION
    )
    linkage = policy_for(TaskCategory.LINKAGE_CANDIDATE)
    assert linkage.cloud_models_allowed is False
    assert linkage.risk_level is RiskLevel.HIGH
    assert policy_for(TaskCategory.DIFFERENCE_EXPLANATION).max_row_window == 100
    assert policy_for(TaskCategory.RULE_DRAFT).reversible_apply_allowed is False


@pytest.mark.parametrize(
    ("invocation", "expected_reason"),
    [
        (
            InvocationRequest(
                task_category=TaskCategory.FIELD_MAPPING,
                role=ModelRole.PROPOSER,
                uses_cloud_model=True,
                payload_redacted=False,
                row_window=20,
            ),
            "payload must be redacted before model exposure",
        ),
        (
            InvocationRequest(
                task_category=TaskCategory.LINKAGE_CANDIDATE,
                role=ModelRole.REVIEWER,
                uses_cloud_model=True,
                payload_redacted=True,
                row_window=50,
            ),
            "cloud models are not allowed for this task",
        ),
        (
            InvocationRequest(
                task_category=TaskCategory.DIFFERENCE_EXPLANATION,
                role=ModelRole.PROPOSER,
                uses_cloud_model=False,
                payload_redacted=True,
                row_window=101,
            ),
            "row window exceeds the task policy",
        ),
    ],
)
def test_invocation_rejects_redaction_cloud_and_window_boundary_violations(
    invocation: InvocationRequest,
    expected_reason: str,
) -> None:
    result = ModelOrchestrator().authorize_invocation(invocation)

    assert result.allowed is False
    assert expected_reason in result.reasons
    assert result.may_write_amounts is False
    assert result.may_write_ledger is False


def test_invocation_accepts_exact_window_boundary_without_granting_write_access() -> None:
    result = ModelOrchestrator().authorize_invocation(
        InvocationRequest(
            task_category=TaskCategory.FIELD_MAPPING,
            role=ModelRole.REVIEWER,
            uses_cloud_model=True,
            payload_redacted=True,
            row_window=20,
        )
    )

    assert result.allowed is True
    assert result.reasons == ()
    assert result.policy.input_exposure is InputExposureLevel.REDACTED_SAMPLE
    assert result.may_write_amounts is False
    assert result.may_write_ledger is False


@pytest.mark.parametrize(
    ("guard", "regression", "expected_reason"),
    [
        (
            EvidenceGuardStatus.NOT_RUN,
            _passing_regression(),
            "evidence guard has not run",
        ),
        (
            EvidenceGuardStatus.FAILED,
            _passing_regression(),
            "evidence guard rejected at least one citation",
        ),
        (
            EvidenceGuardStatus.VERIFIED,
            RegressionResult(RegressionStatus.NOT_RUN, 0, 0),
            "regression has not run",
        ),
        (
            EvidenceGuardStatus.VERIFIED,
            RegressionResult(RegressionStatus.FAILED, 2, 20),
            "regression failed",
        ),
        (
            EvidenceGuardStatus.VERIFIED,
            RegressionResult(RegressionStatus.PASSED, 1, 20),
            "regression covers too few periods",
        ),
        (
            EvidenceGuardStatus.VERIFIED,
            RegressionResult(RegressionStatus.PASSED, 2, 19),
            "regression covers too few cases",
        ),
        (
            EvidenceGuardStatus.VERIFIED,
            RegressionResult(RegressionStatus.PASSED, 2, 20, major_error_count=1),
            "regression observed a major error",
        ),
    ],
)
def test_model_agreement_never_bypasses_evidence_or_regression(
    guard: EvidenceGuardStatus,
    regression: RegressionResult,
    expected_reason: str,
) -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER),
        _conclusion(ModelRole.REVIEWER),
        evidence_guard_status=guard,
        regression=regression,
    )

    assert result.models_agree is True
    assert result.decision is ReleaseDecision.SUGGEST_ONLY
    assert expected_reason in result.reasons
    assert result.may_write_amounts is False
    assert result.may_write_ledger is False


def test_missing_required_citation_keeps_agreed_result_as_suggestion() -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER),
        _conclusion(ModelRole.REVIEWER, cites_evidence=False),
        evidence_guard_status=EvidenceGuardStatus.VERIFIED,
        regression=_passing_regression(),
    )

    assert result.evidence_verified is False
    assert result.decision is ReleaseDecision.SUGGEST_ONLY
    assert "both conclusions must cite the required evidence" in result.reasons


def test_same_model_cannot_self_review_even_when_every_other_gate_passes() -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER, model_id="one-model"),
        _conclusion(ModelRole.REVIEWER, model_id="one-model"),
        evidence_guard_status=EvidenceGuardStatus.VERIFIED,
        regression=_passing_regression(),
    )

    assert result.independent_models is False
    assert result.decision is ReleaseDecision.SUGGEST_ONLY
    assert "proposer and reviewer must be independent models" in result.reasons


def test_disagreement_remains_suggestion_after_evidence_and_regression_pass() -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER, conclusion_key="map:order_id"),
        _conclusion(ModelRole.REVIEWER, conclusion_key="map:transaction_id"),
        evidence_guard_status=EvidenceGuardStatus.VERIFIED,
        regression=_passing_regression(),
    )

    assert result.models_agree is False
    assert result.evidence_verified is True
    assert result.regression_passed is True
    assert result.decision is ReleaseDecision.SUGGEST_ONLY


@pytest.mark.parametrize(
    "task",
    [TaskCategory.STRUCTURE_IDENTIFICATION, TaskCategory.FIELD_MAPPING],
)
def test_low_and_medium_risk_non_financial_result_can_only_apply_reversibly(
    task: TaskCategory,
) -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER, task=task),
        _conclusion(ModelRole.REVIEWER, task=task),
        evidence_guard_status=EvidenceGuardStatus.VERIFIED,
        regression=_passing_regression(),
    )

    assert result.decision is ReleaseDecision.REVERSIBLE_APPLY
    assert result.may_write_amounts is False
    assert result.may_write_ledger is False


@pytest.mark.parametrize(
    "task",
    [
        TaskCategory.LINKAGE_CANDIDATE,
        TaskCategory.DIFFERENCE_EXPLANATION,
        TaskCategory.RULE_DRAFT,
    ],
)
def test_high_risk_result_requires_governance_after_all_gates_pass(
    task: TaskCategory,
) -> None:
    result = ModelOrchestrator().decide_release(
        _conclusion(ModelRole.PROPOSER, task=task),
        _conclusion(ModelRole.REVIEWER, task=task),
        evidence_guard_status=EvidenceGuardStatus.VERIFIED,
        regression=_passing_regression(),
    )

    assert result.decision is ReleaseDecision.GOVERNANCE_REVIEW
    assert result.may_write_amounts is False
    assert result.may_write_ledger is False


def test_conclusion_pair_must_have_same_task_and_ordered_roles() -> None:
    orchestrator = ModelOrchestrator()

    with pytest.raises(ValueError, match="same task"):
        orchestrator.decide_release(
            _conclusion(ModelRole.PROPOSER, task=TaskCategory.FIELD_MAPPING),
            _conclusion(ModelRole.REVIEWER, task=TaskCategory.RULE_DRAFT),
            evidence_guard_status=EvidenceGuardStatus.VERIFIED,
            regression=_passing_regression(),
        )
    with pytest.raises(ValueError, match="first conclusion"):
        orchestrator.decide_release(
            _conclusion(ModelRole.REVIEWER),
            _conclusion(ModelRole.REVIEWER),
            evidence_guard_status=EvidenceGuardStatus.VERIFIED,
            regression=_passing_regression(),
        )
