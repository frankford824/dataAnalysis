"""Schema DDL for experiment and counterfactual tables.

Provides DDL as lists of CREATE TABLE statements **without** schema prefix;
the caller is responsible for qualifying table names with the appropriate
schema (e.g. ``counterfactual_<experiment_id>.``).

This lives in the memory layer so that schema registration never has to import
the experiment package, which sits above it.
"""

from __future__ import annotations

COUNTERFACTUAL_RESULT_TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS cf_reconciliation_item (
        item_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        contract_id VARCHAR NOT NULL,
        period_id VARCHAR NOT NULL,
        source_kind VARCHAR NOT NULL,
        source_record_key VARCHAR NOT NULL,
        side VARCHAR NOT NULL,
        business_key VARCHAR,
        settlement_batch_key VARCHAR,
        cash_bridge_key VARCHAR,
        event_date DATE,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        amount DECIMAL(38,4) NOT NULL,
        evidence_id VARCHAR,
        attributes_json JSON,
        participation VARCHAR DEFAULT 'two_sided',
        posting_target VARCHAR,
        route_rule_id VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cf_reconciliation_balance (
        balance_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        contract_id VARCHAR NOT NULL,
        period_id VARCHAR NOT NULL,
        balance_key VARCHAR NOT NULL,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        expected_amount DECIMAL(38,4) NOT NULL,
        actual_amount DECIMAL(38,4) NOT NULL,
        matched_amount DECIMAL(38,4) NOT NULL,
        difference_amount DECIMAL(38,4) NOT NULL,
        status VARCHAR NOT NULL,
        evidence_json JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cf_invariant_evaluation (
        evaluation_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        invariant_id VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        left_total DECIMAL(38,4),
        right_total DECIMAL(38,4),
        gap_amount DECIMAL(38,4),
        participating_rows BIGINT,
        is_material BOOLEAN NOT NULL DEFAULT false,
        evidence_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cf_unresolved_balance (
        unresolved_id VARCHAR PRIMARY KEY,
        balance_id VARCHAR NOT NULL,
        reason_code VARCHAR NOT NULL,
        amount DECIMAL(38,4) NOT NULL,
        status VARCHAR NOT NULL,
        explanation VARCHAR,
        evidence_id VARCHAR
    )
    """,
]

MAIN_EXPERIMENT_TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS experiment (
        experiment_id VARCHAR PRIMARY KEY,
        hypothesis_kind VARCHAR NOT NULL,
        hypothesis_json JSON NOT NULL,
        proposed_by VARCHAR NOT NULL,
        baseline_run_id VARCHAR NOT NULL,
        shadow_run_id VARCHAR,
        scope_json JSON NOT NULL,
        baseline_code_sha VARCHAR NOT NULL,
        shadow_code_sha VARCHAR NOT NULL,
        baseline_input_sha256 VARCHAR NOT NULL,
        shadow_input_sha256 VARCHAR NOT NULL,
        output_sha256 VARCHAR,
        verdict VARCHAR NOT NULL DEFAULT 'pending',
        verdict_reasons JSON NOT NULL DEFAULT '[]',
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        decided_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiment_metric (
        experiment_id VARCHAR NOT NULL,
        period_id VARCHAR,
        store_id VARCHAR,
        metric VARCHAR NOT NULL,
        before_value DECIMAL(38,4),
        after_value DECIMAL(38,4),
        delta_value DECIMAL(38,4),
        PRIMARY KEY (experiment_id, period_id, store_id, metric)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiment_delta (
        delta_id VARCHAR PRIMARY KEY,
        experiment_id VARCHAR NOT NULL,
        subject_kind VARCHAR NOT NULL,
        subject_key VARCHAR NOT NULL,
        before_amount DECIMAL(38,4),
        after_amount DECIMAL(38,4),
        is_material BOOLEAN NOT NULL DEFAULT false,
        is_reversal BOOLEAN NOT NULL DEFAULT false,
        evidence_binding_digest VARCHAR NOT NULL,
        evidence_json JSON NOT NULL
    )
    """,
]
