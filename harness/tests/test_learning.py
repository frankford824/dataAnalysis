from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import duckdb

from commerce_harness.evidence_policy import (
    LEARNING_POLICY_VERSION,
    NORMALIZATION_RULE_VERSION,
    evidence_binding_digest,
)
from commerce_harness.learning import LearningEvaluator, LearningPolicy
from commerce_harness.memory import SCHEMA_VERSION, DuckDBMemory


def _seed_group(
    memory: DuckDBMemory,
    *,
    enterprise_id: str = "enterprise-a",
    category: str = "difference_explanation",
    model_version: str = "model-a:v1",
    sample_count: int = 20,
    start_index: int = 0,
    decisions: dict[int, str] | None = None,
    failed_guards: set[int] | None = None,
    failed_critics: set[int] | None = None,
    mismatched_digests: set[int] | None = None,
) -> list[str]:
    decisions = decisions or {}
    failed_guards = failed_guards or set()
    failed_critics = failed_critics or set()
    mismatched_digests = mismatched_digests or set()
    suffix = enterprise_id.replace("-", "_")
    contract_id = f"contract_{suffix}"
    period_ids = (f"period_{suffix}_1", f"period_{suffix}_2")
    run_ids = (f"run_{suffix}_1", f"run_{suffix}_2")
    memory.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        )
        VALUES (?, ?, ?, ?, 'taobao', 1, DATE '2026-01-01', 'active', '{}')
        ON CONFLICT (contract_id) DO NOTHING
        """,
        [contract_id, contract_id, enterprise_id, f"store_{suffix}"],
    )
    for index, (period_id, run_id) in enumerate(
        zip(period_ids, run_ids, strict=True),
        start=1,
    ):
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES (?, ?, ?, ?, ?, 'open')
            ON CONFLICT (period_id) DO NOTHING
            """,
            [
                period_id,
                contract_id,
                f"store_{suffix}",
                f"2026-0{index}-01",
                f"2026-0{index}-28",
            ],
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES (?, ?, ?, 'reconcile', 'succeeded')
            ON CONFLICT (run_id) DO NOTHING
            """,
            [run_id, contract_id, period_id],
        )

    suggestion_ids: list[str] = []
    for offset in range(sample_count):
        sample_index = start_index + offset
        period_index = sample_index % 2
        period_id = period_ids[period_index]
        run_id = run_ids[period_index]
        balance_id = f"balance_{suffix}_{category}_{model_version}_{sample_index}"
        unresolved_id = f"unresolved_{suffix}_{category}_{model_version}_{sample_index}"
        suggestion_id = f"suggestion_{suffix}_{category}_{model_version}_{sample_index}"
        snapshot_id = f"snapshot-{suffix}-{category}-{model_version}-{sample_index}"
        evidence_id = f"evidence-{suffix}-{category}-{model_version}-{sample_index}"
        digest = f"candidate-sha-{sample_index}"
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES (?, ?, 1, ?, ?, 'evidence.csv', current_timestamp, '{}')
            """,
            [
                snapshot_id,
                f"{sample_index:064x}",
                f"/immutable/{snapshot_id}",
                f"fixture://{snapshot_id}",
            ],
        )
        memory.execute(
            """
            INSERT INTO evidence_record (
                evidence_id, run_id, snapshot_id, evidence_kind,
                payload_json, payload_sha256
            )
            VALUES (?, ?, ?, 'source_row', '[]', ?)
            """,
            [evidence_id, run_id, snapshot_id, f"{sample_index + 1:064x}"],
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount,
                difference_amount, status
            )
            VALUES (?, ?, ?, ?, ?, 100.0000, 90.0000, 90.0000, 10.0000, 'unresolved')
            """,
            [
                balance_id,
                run_id,
                contract_id,
                period_id,
                f"key-{category}-{model_version}-{sample_index}",
            ],
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status,
                evidence_id
            )
            VALUES (?, ?, 'amount_mismatch', 10.0000, 'open', ?)
            """,
            [unresolved_id, balance_id, evidence_id],
        )
        binding = {
            "ordinal": 0,
            "snapshot_id": snapshot_id,
            "artifact_id": "",
            "source_member": "",
            "source_sheet": "",
            "row_no": 2,
            "field": "amount",
            "source_value": "10.0000",
            "normalization_version": NORMALIZATION_RULE_VERSION,
            "rule_version_id": "",
        }
        binding_sha256 = evidence_binding_digest([binding])
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, row_no, field,
                source_value, normalization_version
            )
            VALUES (?, ?, 0, ?, 2, 'amount', '10.0000', ?)
            """,
            [
                f"binding-{suffix}-{category}-{model_version}-{sample_index}",
                evidence_id,
                snapshot_id,
                NORMALIZATION_RULE_VERSION,
            ],
        )
        memory.execute(
            """
            INSERT INTO residual_suggestion (
                suggestion_id, unresolved_id, suggestion_kind, category,
                action, rationale, confidence, source_model, candidate_json,
                candidate_sha256, guard_status, critic_status,
                evidence_policy_version, evidence_binding_sha256, status
            )
            VALUES (
                ?, ?, 'difference_explanation', ?, 'accept', 'evidence-backed',
                0.990000, ?, ?, ?, ?, ?, ?, ?, 'reviewed'
            )
            """,
            [
                suggestion_id,
                unresolved_id,
                category,
                model_version,
                json.dumps(
                    {
                        "citations": [
                            {"snapshotId": f"snapshot-{sample_index}", "rowNumber": 1}
                        ]
                    }
                ),
                digest,
                "failed" if sample_index in failed_guards else "passed",
                "failed" if sample_index in failed_critics else "passed",
                NORMALIZATION_RULE_VERSION,
                binding_sha256,
            ],
        )
        memory.execute(
            """
            INSERT INTO review_decision (
                decision_id, unresolved_id, suggestion_id, decision,
                final_action, reason, decided_by, candidate_sha256
            )
            VALUES (?, ?, ?, ?, 'human-action', 'human-reviewed', 'reviewer', ?)
            """,
            [
                f"decision_{suffix}_{category}_{model_version}_{sample_index}",
                unresolved_id,
                suggestion_id,
                decisions.get(sample_index, "approve"),
                (
                    f"different-sha-{sample_index}"
                    if sample_index in mismatched_digests
                    else digest
                ),
            ],
        )
        suggestion_ids.append(suggestion_id)
    return suggestion_ids


def _add_correction(
    memory: DuckDBMemory,
    suggestion_id: str,
    *,
    major_amount_error: bool | None = None,
) -> None:
    unresolved_id = str(
        memory.fetchone_required(
            """
            SELECT unresolved_id
            FROM residual_suggestion
            WHERE suggestion_id = ?
            """,
            [suggestion_id],
        )[0]
    )
    feature: dict[str, object] = {"reasonCode": "amount_mismatch"}
    if major_amount_error is not None:
        feature["majorAmountError"] = major_amount_error
    memory.execute(
        """
        INSERT INTO correction (
            correction_id, suggestion_id, unresolved_id,
            feature_json, model_outcome_json, human_outcome_json
        )
        VALUES (?, ?, ?, ?, '{"action":"accept"}', '{"action":"replace"}')
        """,
        [
            f"correction_{suggestion_id}",
            suggestion_id,
            unresolved_id,
            json.dumps(feature),
        ],
    )


def test_eligible_evaluation_requires_two_periods_twenty_verified_samples() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory)

        evaluations = LearningEvaluator(memory).evaluate_and_persist()

        assert len(evaluations) == 1
        evaluation = evaluations[0]
        assert evaluation.eligible
        assert evaluation.reasons == ("eligible_for_governance_review_only",)
        assert evaluation.proposed_level == "L1"
        assert not evaluation.may_write_amounts
        assert not evaluation.may_write_ledger
        assert not evaluation.may_publish_rules
        assert evaluation.metrics.sample_count == 20
        assert evaluation.metrics.period_count == 2
        assert evaluation.metrics.acceptance_rate == Decimal("1")
        assert evaluation.metrics.accuracy == Decimal("1")
        assert evaluation.metrics.evidence_pass_rate == Decimal("1")
        persisted = memory.fetchone_required(
            """
            SELECT enterprise_id, category, model_version, eligible,
                   precision, sample_count, reason_json, metrics_json,
                   source_digest, policy_version, current_level, proposed_level
            FROM autonomy_evaluation
            """
        )
        assert persisted[:6] == (
            "enterprise-a",
            "difference_explanation",
            "model-a:v1",
            True,
            Decimal("1.000000"),
            20,
        )
        assert json.loads(str(persisted[6])) == [
            "eligible_for_governance_review_only"
        ]
        assert json.loads(str(persisted[7]))["acceptance_rate"] == "1"
        assert persisted[8:] == (
            evaluation.source_digest,
            LEARNING_POLICY_VERSION,
            "L0",
            "L1",
        )


def test_failed_independent_review_never_counts_as_accurate() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory, failed_critics=set(range(20)))

        evaluation = LearningEvaluator(memory).evaluate_and_persist()[0]

        assert evaluation.eligible is False
        assert evaluation.metrics.evidence_pass_count == 0
        assert evaluation.metrics.accurate_count == 0
        assert "evidence_citations_not_all_verified" in evaluation.reasons


def test_learning_revalidates_binding_digest_and_ignores_old_policy() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        suggestion_ids = _seed_group(memory, sample_count=2)
        memory.execute(
            """
            UPDATE evidence_binding SET row_no = 99
            WHERE evidence_id = (
                SELECT unresolved.evidence_id
                FROM residual_suggestion suggestion
                JOIN unresolved_balance unresolved
                  ON unresolved.unresolved_id = suggestion.unresolved_id
                WHERE suggestion.suggestion_id = ?
            )
            """,
            [suggestion_ids[0]],
        )
        evaluation = LearningEvaluator(
            memory,
            policy=LearningPolicy(minimum_samples=2),
        ).evaluate_and_persist()[0]
        assert evaluation.metrics.evidence_pass_count == 1
        assert evaluation.eligible is False

        memory.execute(
            """
            UPDATE residual_suggestion
            SET evidence_policy_version = 'finite-normalization-v4'
            """
        )
        assert LearningEvaluator(memory).evaluate_and_persist() == ()


def test_acceptance_rate_is_not_reported_as_accuracy() -> None:
    decisions = {index: "reject" for index in range(15, 20)}
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(
            memory,
            decisions=decisions,
            failed_guards=set(range(5)),
        )

        evaluation = LearningEvaluator(memory).evaluate_and_persist()[0]

        assert evaluation.metrics.accepted_count == 15
        assert evaluation.metrics.acceptance_rate == Decimal("0.75")
        assert evaluation.metrics.accuracy_sample_count == 15
        assert evaluation.metrics.accurate_count == 10
        assert evaluation.metrics.accuracy == Decimal(2) / Decimal(3)
        assert evaluation.metrics.evidence_pass_count == 15
        assert not evaluation.eligible
        assert "evidence_citations_not_all_verified" in evaluation.reasons
        assert "accuracy_below_threshold" in evaluation.reasons


def test_unassessed_accepted_sample_does_not_inflate_accuracy() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory, failed_guards={0}, mismatched_digests={1})

        evaluation = LearningEvaluator(memory).evaluate_and_persist()[0]

        assert evaluation.metrics.accepted_count == 20
        assert evaluation.metrics.acceptance_rate == Decimal("1")
        assert evaluation.metrics.accuracy_sample_count == 18
        assert evaluation.metrics.accurate_count == 18
        assert evaluation.metrics.accuracy == Decimal("1")
        assert not evaluation.eligible
        assert "evidence_citations_not_all_verified" in evaluation.reasons
        assert "accuracy_not_assessed_for_all_samples" in evaluation.reasons


def test_major_amount_correction_blocks_eligibility_without_changing_amounts() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        suggestion_ids = _seed_group(memory)
        _add_correction(memory, suggestion_ids[0])
        before = memory.fetchone_required(
            """
            SELECT count(*), sum(amount)
            FROM unresolved_balance
            """
        )

        evaluation = LearningEvaluator(memory).evaluate_and_persist()[0]

        after = memory.fetchone_required(
            """
            SELECT count(*), sum(amount)
            FROM unresolved_balance
            """
        )
        assert before == after
        assert evaluation.metrics.correction_count == 1
        assert evaluation.metrics.major_amount_error_count == 1
        assert not evaluation.eligible
        assert "major_amount_error_observed" in evaluation.reasons
        assert memory.fetchone_required("SELECT count(*) FROM rule_version") == (0,)
        assert memory.fetchone_required("SELECT count(*) FROM pnl_cell") == (0,)


def test_evaluation_is_idempotent_and_new_evidence_keeps_history() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory)
        evaluator = LearningEvaluator(memory)

        first = evaluator.evaluate_and_persist()
        second = evaluator.evaluate_and_persist()

        assert first == second
        assert memory.fetchone_required(
            "SELECT count(*) FROM autonomy_evaluation"
        ) == (1,)
        _seed_group(memory, sample_count=1, start_index=20)
        third = evaluator.evaluate_and_persist()
        assert third[0].evaluation_id != first[0].evaluation_id
        assert third[0].metrics.sample_count == 21
        assert memory.fetchone_required(
            "SELECT count(*) FROM autonomy_evaluation"
        ) == (2,)


def test_groups_are_isolated_by_enterprise_category_and_model_version() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory, enterprise_id="enterprise-a", sample_count=2)
        _seed_group(
            memory,
            enterprise_id="enterprise-b",
            category="field_mapping",
            model_version="model-b:v2",
            sample_count=2,
        )

        evaluations = LearningEvaluator(
            memory,
            policy=LearningPolicy(minimum_samples=2),
        ).evaluate_and_persist()

        assert {
            (
                evaluation.enterprise_id,
                evaluation.category,
                evaluation.model_version,
            )
            for evaluation in evaluations
        } == {
            ("enterprise-a", "difference_explanation", "model-a:v1"),
            ("enterprise-b", "field_mapping", "model-b:v2"),
        }


def test_deferred_reviews_are_not_learning_samples() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        _seed_group(memory, sample_count=1, decisions={0: "defer"})

        assert LearningEvaluator(memory).evaluate_and_persist() == ()
        assert memory.fetchone_required(
            "SELECT count(*) FROM autonomy_evaluation"
        ) == (0,)


def test_existing_autonomy_table_is_extended_without_losing_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.duckdb"
    with duckdb.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE harness_schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            )
            """
        )
        connection.execute("INSERT INTO harness_schema_version(version) VALUES (13)")
        connection.execute(
            """
            CREATE TABLE autonomy_evaluation (
                evaluation_id VARCHAR PRIMARY KEY,
                category VARCHAR NOT NULL,
                period_id VARCHAR,
                current_level VARCHAR NOT NULL,
                proposed_level VARCHAR NOT NULL,
                precision DECIMAL(8,6),
                major_error_count INTEGER NOT NULL DEFAULT 0,
                cumulative_exposure DECIMAL(38,4) NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                eligible BOOLEAN,
                rationale VARCHAR NOT NULL,
                evaluated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            )
            """
        )
        connection.execute(
            """
            INSERT INTO autonomy_evaluation (
                evaluation_id, category, current_level, proposed_level,
                eligible, rationale
            )
            VALUES (
                'legacy-evaluation', 'timing', 'L0', 'L0', true, 'legacy'
            )
            """
        )

    with DuckDBMemory(database_path) as memory:
        memory.initialize()
        columns = {
            str(row[0])
            for row in memory.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'autonomy_evaluation'
                """
            ).fetchall()
        }
        assert {
            "enterprise_id",
            "model_version",
            "eligible",
            "reason_json",
            "metrics_json",
            "source_digest",
            "policy_version",
        } <= columns
        assert memory.fetchone_required(
            """
            SELECT evaluation_id, rationale, eligible
            FROM autonomy_evaluation
            WHERE evaluation_id = 'legacy-evaluation'
            """
        ) == ("legacy-evaluation", "legacy", False)
        assert memory.fetchone_required(
            "SELECT max(version) FROM harness_schema_version"
        ) == (SCHEMA_VERSION,)
