"""Tests for experiment verdict and metrics computation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from commerce_harness.experiment.metrics import compute_metrics
from commerce_harness.experiment.runner import (
    ExperimentProposal,
    ExperimentRunError,
    ExperimentRunner,
)
from commerce_harness.experiment.shadow import ShadowOutcome
from commerce_harness.experiment.verdict import decide_verdict


def _outcome(
    *,
    unresolved: dict[str, Decimal],
    checksum: str = "checksum",
) -> ShadowOutcome:
    total = sum(unresolved.values(), Decimal("0.0000"))
    return ShadowOutcome(
        summary={
            "unresolved_count": Decimal(len(unresolved)),
            "unresolved_amount_abs": total,
            "control_total_gap": Decimal("0.0000"),
            "explained_amount_ratio": Decimal("1") if not unresolved else Decimal("0.5"),
            "amount_weighted_auto_rate": (
                Decimal("1") if not unresolved else Decimal("0.5")
            ),
        },
        checksum=checksum,
        unresolved_subjects=dict(unresolved),
        routed_count=0,
    )


class TestComputeMetrics:
    def test_basic_delta(self):
        before = {"unresolved_count": "100", "unresolved_amount_abs": "5000.00"}
        after = {"unresolved_count": "50", "unresolved_amount_abs": "2000.00"}
        metrics = compute_metrics(before, after)
        assert metrics["unresolved_count"]["delta"] == Decimal("-50.0000")
        assert metrics["unresolved_amount_abs"]["delta"] == Decimal("-3000.0000")

    def test_control_gap_delta(self):
        before = {"control_total_gap": "501.90"}
        after = {"control_total_gap": "0.00"}
        metrics = compute_metrics(before, after)
        assert metrics["control_total_gap"]["delta"] == Decimal("-501.9000")

    def test_float_forbidden(self):
        with pytest.raises(TypeError, match="float"):
            compute_metrics({"unresolved_count": 5.0}, {})

    def test_missing_keys_default_to_zero(self):
        metrics = compute_metrics({}, {})
        assert metrics["unresolved_count"]["before"] == Decimal("0.0000")
        assert metrics["unresolved_count"]["after"] == Decimal("0.0000")

    def test_safety_metrics(self):
        before = {"major_reversal_count": "0", "newly_unresolved_count": "0"}
        after = {"major_reversal_count": "0", "newly_unresolved_count": "3"}
        metrics = compute_metrics(before, after)
        assert metrics["newly_unresolved_count"]["after"] == Decimal("3.0000")


class TestDecideVerdict:
    def _base_metrics(self, **overrides):
        defaults = {
            "major_reversal_count": {
                "before": Decimal("0"),
                "after": Decimal("0"),
                "delta": Decimal("0"),
            },
            "baseline_regression_count": {
                "before": Decimal("0"),
                "after": Decimal("0"),
                "delta": Decimal("0"),
            },
            "evidence_integrity_failures": {
                "before": Decimal("0"),
                "after": Decimal("0"),
                "delta": Decimal("0"),
            },
            "unresolved_amount_abs": {
                "before": Decimal("5000"),
                "after": Decimal("2000"),
                "delta": Decimal("-3000"),
            },
            "newly_unresolved_count": {
                "before": Decimal("0"),
                "after": Decimal("0"),
                "delta": Decimal("0"),
            },
            "control_total_gap": {
                "before": Decimal("500"),
                "after": Decimal("0"),
                "delta": Decimal("-500"),
            },
        }
        defaults.update(overrides)
        return defaults

    def test_supported(self):
        verdict, reasons = decide_verdict(self._base_metrics())
        assert verdict == "supported"

    def test_rejected_major_reversal(self):
        metrics = self._base_metrics(
            major_reversal_count={
                "before": Decimal("0"), "after": Decimal("1"), "delta": Decimal("1"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "rejected"
        assert any("反转" in r for r in reasons)

    def test_rejected_baseline_regression_locked(self):
        metrics = self._base_metrics(
            baseline_regression_count={
                "before": Decimal("0"), "after": Decimal("2"), "delta": Decimal("2"),
            },
        )
        verdict, reasons = decide_verdict(metrics, period_locked=True)
        assert verdict == "rejected"

    def test_baseline_regression_not_locked_not_rejected(self):
        metrics = self._base_metrics(
            baseline_regression_count={
                "before": Decimal("0"), "after": Decimal("2"), "delta": Decimal("2"),
            },
        )
        verdict, reasons = decide_verdict(metrics, period_locked=False)
        assert verdict != "rejected"

    def test_rejected_evidence_failure(self):
        metrics = self._base_metrics(
            evidence_integrity_failures={
                "before": Decimal("0"), "after": Decimal("1"), "delta": Decimal("1"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "rejected"

    def test_rejected_checksum_unstable(self):
        verdict, reasons = decide_verdict(self._base_metrics(), checksum_stable=False)
        assert verdict == "rejected"
        assert any("哈希" in r for r in reasons)

    def test_rejected_when_unresolved_amount_rises(self):
        metrics = self._base_metrics(
            unresolved_amount_abs={
                "before": Decimal("5000"), "after": Decimal("6000"),
                "delta": Decimal("1000"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "rejected"
        assert any("未决金额上升" in reason for reason in reasons)

    def test_rejected_when_too_many_new_unresolved(self):
        metrics = self._base_metrics(
            newly_unresolved_count={
                "before": Decimal("0"), "after": Decimal("10"),
                "delta": Decimal("10"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "rejected"

    def test_rejected_when_amount_weighted_auto_rate_drops(self):
        metrics = self._base_metrics(
            unresolved_amount_abs={
                "before": Decimal("5000"), "after": Decimal("5000"),
                "delta": Decimal("0"),
            },
            control_total_gap={
                "before": Decimal("0"), "after": Decimal("0"), "delta": Decimal("0"),
            },
            amount_weighted_auto_rate={
                "before": Decimal("0.9"), "after": Decimal("0.8"),
                "delta": Decimal("-0.1"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "rejected"

    def test_supported_on_classification_only_improvement(self):
        # A hypothesis that moves no money but explains more of it is still an
        # improvement worth adopting.
        metrics = self._base_metrics(
            unresolved_amount_abs={
                "before": Decimal("5000"), "after": Decimal("5000"),
                "delta": Decimal("0"),
            },
            control_total_gap={
                "before": Decimal("0"), "after": Decimal("0"), "delta": Decimal("0"),
            },
            explained_amount_ratio={
                "before": Decimal("0.80"), "after": Decimal("0.92"),
                "delta": Decimal("0.12"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "supported"
        assert any("已解释金额占比提升" in reason for reason in reasons)

    def test_inconclusive_when_nothing_moves(self):
        metrics = self._base_metrics(
            unresolved_amount_abs={
                "before": Decimal("5000"), "after": Decimal("5000"),
                "delta": Decimal("0"),
            },
            control_total_gap={
                "before": Decimal("0"), "after": Decimal("0"), "delta": Decimal("0"),
            },
        )
        verdict, reasons = decide_verdict(metrics)
        assert verdict == "inconclusive"


class TestExperimentRunner:
    def test_propose(self):
        runner = ExperimentRunner(code_sha="abc123")
        proposal = ExperimentProposal(
            hypothesis_kind="rule_add",
            hypothesis_json={"action": "route", "posting_target": "platform_fee"},
            proposed_by="human:tester",
            baseline_run_id="run_001",
            scope={"period": "2602", "store": "store_a"},
            baseline_code_sha="abc123",
            baseline_input_sha256="input_sha",
        )
        record = runner.propose(proposal)
        assert record.verdict == "pending"
        assert record.shadow_run_id is None

    def _record(self, runner):
        return runner.propose(
            ExperimentProposal(
                hypothesis_kind="rule_add",
                hypothesis_json={"action": "route"},
                proposed_by="human:tester",
                baseline_run_id="run_001",
                scope={"period": "2602"},
                baseline_code_sha="abc123",
                baseline_input_sha256="input_sha",
            )
        )

    def test_run_without_a_shadow_implementation_is_refused(self):
        runner = ExperimentRunner(code_sha="abc123")
        with pytest.raises(ExperimentRunError, match="shadow_run"):
            runner.run(self._record(runner))

    def test_run_supported_from_measured_outcomes(self):
        before = _outcome(unresolved={"missing_side:A": Decimal("5000")})
        after = _outcome(unresolved={})
        runner = ExperimentRunner(
            shadow_run=lambda hypothesis, ctx: (before, after), code_sha="abc123",
        )
        result = runner.run(self._record(runner))
        assert result.verdict == "supported"
        assert result.output_sha256 is not None
        assert result.shadow_run_id is not None
        assert result.metrics is not None
        assert result.deltas == [
            {
                "subject_kind": "unresolved_balance",
                "subject_key": "missing_side:A",
                "before_amount": Decimal("5000"),
                "after_amount": Decimal("0.0000"),
                "is_material": True,
                "is_reversal": False,
            }
        ]

    def test_run_rejected_when_exposure_grows(self):
        before = _outcome(unresolved={"missing_side:A": Decimal("1000")})
        after = _outcome(unresolved={"missing_side:A": Decimal("9000")})
        runner = ExperimentRunner(
            shadow_run=lambda hypothesis, ctx: (before, after), code_sha="abc",
        )
        result = runner.run(self._record(runner))
        assert result.verdict == "rejected"

    def test_unstable_repeat_execution_is_rejected(self):
        before = _outcome(unresolved={"missing_side:A": Decimal("5000")})
        stable = _outcome(unresolved={})
        drifted = _outcome(unresolved={}, checksum="drifted")
        outcomes = iter([(before, stable), (before, drifted)])

        runner = ExperimentRunner(
            shadow_run=lambda hypothesis, ctx: next(outcomes), code_sha="abc",
        )
        result = runner.run(self._record(runner))
        assert result.verdict == "rejected"
        assert any("重复执行" in reason for reason in result.verdict_reasons)
