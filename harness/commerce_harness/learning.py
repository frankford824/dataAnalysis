from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import duckdb

from commerce_harness.evidence_policy import (
    LEARNING_POLICY_VERSION,
    NORMALIZATION_RULE_VERSION,
    evidence_binding_digest,
)
from commerce_harness.memory import DuckDBMemory

_POLICY_VERSION = LEARNING_POLICY_VERSION
_FINAL_DECISIONS = frozenset({"approve", "explain", "reject", "replace"})
_ACCEPTED_DECISIONS = frozenset({"approve", "explain"})
_MAJOR_ERROR_KEYS = ("majorAmountError", "major_amount_error")


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return Decimal(numerator) / Decimal(denominator)


def _json_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _explicit_major_error(*payloads: object) -> bool | None:
    for payload in payloads:
        mapping = _json_mapping(payload)
        for key in _MAJOR_ERROR_KEYS:
            value = mapping.get(key)
            if isinstance(value, bool):
                return value
    return None


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    minimum_periods: int = 2
    minimum_samples: int = 20
    required_accuracy: Decimal | str = Decimal("0.995")
    policy_version: str = _POLICY_VERSION

    def __post_init__(self) -> None:
        if self.minimum_periods < 1:
            raise ValueError("minimum_periods must be positive")
        if self.minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        accuracy = _decimal(self.required_accuracy)
        if not Decimal("0") <= accuracy <= Decimal("1"):
            raise ValueError("required_accuracy must be between zero and one")
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")
        object.__setattr__(self, "required_accuracy", accuracy)


@dataclass(frozen=True, slots=True)
class LearningMetrics:
    sample_count: int
    period_count: int
    accepted_count: int
    acceptance_rate: Decimal
    accuracy_sample_count: int
    accurate_count: int
    accuracy: Decimal
    evidence_pass_count: int
    evidence_pass_rate: Decimal
    correction_count: int
    major_amount_error_count: int
    cumulative_exposure: Decimal

    def to_dict(self) -> dict[str, int | str]:
        values = asdict(self)
        return {
            key: _decimal_text(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
        }


@dataclass(frozen=True, slots=True)
class LearningEvaluation:
    evaluation_id: str
    enterprise_id: str
    category: str
    model_version: str
    eligible: bool
    reasons: tuple[str, ...]
    metrics: LearningMetrics
    source_digest: str
    policy_version: str
    current_level: str = "L0"
    proposed_level: str = "L0"
    may_write_amounts: bool = False
    may_write_ledger: bool = False
    may_publish_rules: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluationId": self.evaluation_id,
            "enterpriseId": self.enterprise_id,
            "category": self.category,
            "modelVersion": self.model_version,
            "eligible": self.eligible,
            "reason": list(self.reasons),
            "metrics": self.metrics.to_dict(),
            "sourceDigest": self.source_digest,
            "policyVersion": self.policy_version,
            "currentLevel": self.current_level,
            "proposedLevel": self.proposed_level,
            "mayWriteAmounts": self.may_write_amounts,
            "mayWriteLedger": self.may_write_ledger,
            "mayPublishRules": self.may_publish_rules,
        }


@dataclass(frozen=True, slots=True)
class _ReviewedSample:
    enterprise_id: str
    category: str
    model_version: str
    suggestion_id: str
    candidate_sha256: str
    period_id: str
    amount: Decimal
    guard_status: str
    critic_status: str
    evidence_policy_version: str
    evidence_binding_sha256: str
    current_evidence_binding_sha256: str | None
    decision_id: str
    decision: str
    decision_candidate_sha256: str | None
    correction_ids: tuple[str, ...]
    correction_payloads: tuple[tuple[object, object], ...]

    @property
    def evidence_passed(self) -> bool:
        digest_matches = (
            self.decision_candidate_sha256 is None
            or self.decision_candidate_sha256 == self.candidate_sha256
        )
        return (
            self.guard_status == "passed"
            and self.critic_status == "passed"
            and self.evidence_policy_version == NORMALIZATION_RULE_VERSION
            and bool(self.evidence_binding_sha256)
            and self.evidence_binding_sha256
            == self.current_evidence_binding_sha256
            and digest_matches
        )

    @property
    def corrected(self) -> bool:
        return bool(self.correction_ids)

    @property
    def accepted(self) -> bool:
        return self.decision in _ACCEPTED_DECISIONS and not self.corrected

    @property
    def accuracy_assessed(self) -> bool:
        return self.corrected or self.decision in {"reject", "replace"} or self.evidence_passed

    @property
    def accurate(self) -> bool:
        return self.accuracy_assessed and self.accepted and self.evidence_passed

    @property
    def major_amount_error(self) -> bool:
        if not self.corrected:
            return False
        explicit = _explicit_major_error(
            *(value for payloads in self.correction_payloads for value in payloads)
        )
        if explicit is not None:
            return explicit
        return self.amount != Decimal("0")

    def digest_payload(self) -> dict[str, object]:
        return {
            "suggestionId": self.suggestion_id,
            "candidateSha256": self.candidate_sha256,
            "periodId": self.period_id,
            "amount": _decimal_text(self.amount),
            "guardStatus": self.guard_status,
            "criticStatus": self.critic_status,
            "evidencePolicyVersion": self.evidence_policy_version,
            "evidenceBindingSha256": self.evidence_binding_sha256,
            "currentEvidenceBindingSha256": self.current_evidence_binding_sha256,
            "decisionId": self.decision_id,
            "decision": self.decision,
            "decisionCandidateSha256": self.decision_candidate_sha256,
            "correctionIds": list(self.correction_ids),
            "correctionPayloads": [
                [str(feature_json), str(human_outcome_json)]
                for feature_json, human_outcome_json in self.correction_payloads
            ],
        }


class LearningEvaluator:
    """Build and persist audit-only autonomy evaluations from human-reviewed outcomes."""

    def __init__(
        self,
        memory: DuckDBMemory,
        policy: LearningPolicy | None = None,
    ) -> None:
        self._memory = memory
        self.policy = policy or LearningPolicy()

    def evaluate_and_persist(self) -> tuple[LearningEvaluation, ...]:
        samples = self._load_samples()
        grouped: defaultdict[tuple[str, str, str], list[_ReviewedSample]] = defaultdict(list)
        for sample in samples:
            grouped[
                (sample.enterprise_id, sample.category, sample.model_version)
            ].append(sample)

        evaluations = tuple(
            self._evaluate_group(key, tuple(sorted(items, key=lambda item: item.suggestion_id)))
            for key, items in sorted(grouped.items())
        )
        if evaluations:
            with self._memory.transaction() as connection:
                for evaluation in evaluations:
                    self._persist(connection, evaluation)
        return evaluations

    def _load_samples(self) -> tuple[_ReviewedSample, ...]:
        decision_rows = self._memory.execute(
            """
            WITH latest_decision AS (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY suggestion_id
                           ORDER BY decided_at DESC, decision_id DESC
                       ) AS decision_rank
                FROM review_decision
                WHERE suggestion_id IS NOT NULL
                  AND decision IN ('approve', 'explain', 'reject', 'replace')
            )
            SELECT contract.enterprise_id,
                   suggestion.category,
                   suggestion.source_model,
                   suggestion.suggestion_id,
                   suggestion.candidate_sha256,
                   balance.period_id,
                   unresolved.amount,
                   suggestion.guard_status,
                   suggestion.critic_status,
                   suggestion.evidence_policy_version,
                   suggestion.evidence_binding_sha256,
                   unresolved.evidence_id,
                   decision.decision_id,
                   decision.decision,
                   decision.candidate_sha256
            FROM residual_suggestion suggestion
            JOIN unresolved_balance unresolved
              ON unresolved.unresolved_id = suggestion.unresolved_id
            JOIN reconciliation_balance balance
              ON balance.balance_id = unresolved.balance_id
            JOIN reconciliation_contract contract
              ON contract.contract_id = balance.contract_id
            JOIN latest_decision decision
              ON decision.suggestion_id = suggestion.suggestion_id
             AND decision.decision_rank = 1
            WHERE suggestion.evidence_policy_version = ?
            ORDER BY contract.enterprise_id,
                     suggestion.category,
                     suggestion.source_model,
                     suggestion.suggestion_id
            """,
            [NORMALIZATION_RULE_VERSION],
        ).fetchall()
        correction_rows = self._memory.execute(
            """
            SELECT suggestion_id, correction_id, feature_json, human_outcome_json
            FROM correction
            WHERE suggestion_id IS NOT NULL
            ORDER BY suggestion_id, correction_id
            """
        ).fetchall()
        corrections: defaultdict[str, list[tuple[str, object, object]]] = defaultdict(list)
        for suggestion_id, correction_id, feature_json, human_outcome_json in correction_rows:
            corrections[str(suggestion_id)].append(
                (str(correction_id), feature_json, human_outcome_json)
            )

        samples: list[_ReviewedSample] = []
        for row in decision_rows:
            suggestion_id = str(row[3])
            suggestion_corrections = corrections.get(suggestion_id, [])
            samples.append(
                _ReviewedSample(
                    enterprise_id=str(row[0]),
                    category=str(row[1]),
                    model_version=str(row[2]),
                    suggestion_id=suggestion_id,
                    candidate_sha256=str(row[4]),
                    period_id=str(row[5]),
                    amount=_decimal(row[6]),
                    guard_status=str(row[7]),
                    critic_status=str(row[8]),
                    evidence_policy_version=str(row[9] or ""),
                    evidence_binding_sha256=str(row[10] or ""),
                    current_evidence_binding_sha256=self._current_binding_digest(
                        str(row[11] or "")
                    ),
                    decision_id=str(row[12]),
                    decision=str(row[13]),
                    decision_candidate_sha256=(
                        str(row[14]) if row[14] is not None else None
                    ),
                    correction_ids=tuple(item[0] for item in suggestion_corrections),
                    correction_payloads=tuple(
                        (item[1], item[2]) for item in suggestion_corrections
                    ),
                )
            )
        return tuple(samples)

    def _current_binding_digest(self, evidence_id: str) -> str | None:
        if not evidence_id:
            return None
        rows = self._memory.execute(
            """
            SELECT binding.ordinal, binding.snapshot_id, binding.artifact_id,
                   binding.source_member, binding.source_sheet, binding.row_no,
                   binding.field, binding.source_value,
                   binding.normalization_version, binding.rule_version_id,
                   snapshot.original_name, snapshot.source_uri
            FROM evidence_binding binding
            JOIN source_snapshot snapshot
              ON snapshot.snapshot_id = binding.snapshot_id
            WHERE binding.evidence_id = ?
            ORDER BY binding.ordinal
            """,
            [evidence_id],
        ).fetchall()
        if not rows:
            return None
        bindings: list[dict[str, object]] = []
        for row in rows:
            try:
                row_no = int(row[5])
            except (TypeError, ValueError):
                return None
            source_name = str(row[3] or row[10] or row[11] or "")
            if (
                row_no <= 0
                or str(row[8] or "") != NORMALIZATION_RULE_VERSION
                or (source_name.lower().endswith(".xlsx") and not row[4])
            ):
                return None
            bindings.append(
                {
                    "ordinal": row[0],
                    "snapshot_id": row[1],
                    "artifact_id": row[2],
                    "source_member": row[3],
                    "source_sheet": row[4],
                    "row_no": row_no,
                    "field": row[6],
                    "source_value": row[7],
                    "normalization_version": row[8],
                    "rule_version_id": row[9],
                }
            )
        return evidence_binding_digest(bindings)

    def _evaluate_group(
        self,
        key: tuple[str, str, str],
        samples: Sequence[_ReviewedSample],
    ) -> LearningEvaluation:
        enterprise_id, category, model_version = key
        sample_count = len(samples)
        period_count = len({sample.period_id for sample in samples})
        accepted_count = sum(sample.accepted for sample in samples)
        accuracy_sample_count = sum(sample.accuracy_assessed for sample in samples)
        accurate_count = sum(sample.accurate for sample in samples)
        evidence_pass_count = sum(sample.evidence_passed for sample in samples)
        correction_count = sum(len(sample.correction_ids) for sample in samples)
        major_error_count = sum(sample.major_amount_error for sample in samples)
        cumulative_exposure = sum(
            (abs(sample.amount) for sample in samples),
            Decimal("0"),
        )
        metrics = LearningMetrics(
            sample_count=sample_count,
            period_count=period_count,
            accepted_count=accepted_count,
            acceptance_rate=_ratio(accepted_count, sample_count),
            accuracy_sample_count=accuracy_sample_count,
            accurate_count=accurate_count,
            accuracy=_ratio(accurate_count, accuracy_sample_count),
            evidence_pass_count=evidence_pass_count,
            evidence_pass_rate=_ratio(evidence_pass_count, sample_count),
            correction_count=correction_count,
            major_amount_error_count=major_error_count,
            cumulative_exposure=cumulative_exposure,
        )
        reasons: list[str] = []
        if sample_count < self.policy.minimum_samples:
            reasons.append("minimum_samples_not_met")
        if period_count < self.policy.minimum_periods:
            reasons.append("minimum_periods_not_met")
        if evidence_pass_count != sample_count:
            reasons.append("evidence_citations_not_all_verified")
        if accuracy_sample_count != sample_count:
            reasons.append("accuracy_not_assessed_for_all_samples")
        if metrics.accuracy < _decimal(self.policy.required_accuracy):
            reasons.append("accuracy_below_threshold")
        if major_error_count:
            reasons.append("major_amount_error_observed")
        eligible = not reasons
        if eligible:
            reasons.append("eligible_for_governance_review_only")

        source_payload = {
            "enterpriseId": enterprise_id,
            "category": category,
            "modelVersion": model_version,
            "policyVersion": self.policy.policy_version,
            "policy": {
                "minimumPeriods": self.policy.minimum_periods,
                "minimumSamples": self.policy.minimum_samples,
                "requiredAccuracy": _decimal_text(
                    _decimal(self.policy.required_accuracy)
                ),
            },
            "samples": [sample.digest_payload() for sample in samples],
        }
        source_json = json.dumps(
            source_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_digest = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        evaluation_key = "|".join(
            (
                self.policy.policy_version,
                enterprise_id,
                category,
                model_version,
                source_digest,
            )
        )
        evaluation_id = (
            "autonomy_" + hashlib.sha256(evaluation_key.encode("utf-8")).hexdigest()
        )
        return LearningEvaluation(
            evaluation_id=evaluation_id,
            enterprise_id=enterprise_id,
            category=category,
            model_version=model_version,
            eligible=eligible,
            reasons=tuple(reasons),
            metrics=metrics,
            source_digest=source_digest,
            policy_version=self.policy.policy_version,
            proposed_level="L1" if eligible else "L0",
        )

    @staticmethod
    def _persist(
        connection: duckdb.DuckDBPyConnection,
        evaluation: LearningEvaluation,
    ) -> None:
        reason_json = json.dumps(
            list(evaluation.reasons),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        metrics_json = json.dumps(
            evaluation.metrics.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rationale = json.dumps(
            evaluation.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            UPDATE autonomy_evaluation
            SET eligible = false
            WHERE enterprise_id = ?
              AND category = ?
              AND model_version = ?
              AND coalesce(policy_version, '') <> ?
            """,
            [
                evaluation.enterprise_id,
                evaluation.category,
                evaluation.model_version,
                evaluation.policy_version,
            ],
        )
        connection.execute(
            """
            INSERT INTO autonomy_evaluation (
                evaluation_id, enterprise_id, category, model_version,
                period_id, current_level, proposed_level, eligible, precision,
                major_error_count, cumulative_exposure, sample_count,
                reason_json, metrics_json, source_digest, policy_version,
                rationale
            )
            VALUES (
                ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT (evaluation_id) DO NOTHING
            """,
            [
                evaluation.evaluation_id,
                evaluation.enterprise_id,
                evaluation.category,
                evaluation.model_version,
                evaluation.current_level,
                evaluation.proposed_level,
                evaluation.eligible,
                evaluation.metrics.accuracy,
                evaluation.metrics.major_amount_error_count,
                evaluation.metrics.cumulative_exposure,
                evaluation.metrics.sample_count,
                reason_json,
                metrics_json,
                evaluation.source_digest,
                evaluation.policy_version,
                rationale,
            ],
        )
