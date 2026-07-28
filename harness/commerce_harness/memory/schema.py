from __future__ import annotations

SCHEMA_VERSION = 15

REQUIRED_TABLES = frozenset(
    {
        "accounting_period",
        "adjustment_entry",
        "adjudication",
        "baseline",
        "business_decision",
        "business_decision_event",
        "checklist_requirement",
        "checklist_result",
        "cost_asof",
        "evidence_record",
        "evidence_binding",
        "evidence_edge",
        "freight_exception",
        "harness_schema_version",
        "historical_output",
        "input_revision",
        "input_revision_state",
        "llm_call_log",
        "normalized_artifact",
        "pnl_cell",
        "person_identity",
        "person_alias",
        "canonical_product",
        "responsibility_assignment_version",
        "performance_source_import",
        "performance_policy_version",
        "performance_reference_fact",
        "performance_result",
        "performance_result_head",
        "reconciliation_balance",
        "reconciliation_contract",
        "reconciliation_item",
        "reconciliation_link",
        "reconciliation_link_member",
        "residual_suggestion",
        "review_decision",
        "rule_definition",
        "rule_decision",
        "rule_version",
        "run_log",
        "source_snapshot",
        "source_profile",
        "correction",
        "compute_job",
        "diff_finding",
        "autonomy_evaluation",
        "unresolved_balance",
    }
)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS harness_schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_snapshot (
        snapshot_id VARCHAR PRIMARY KEY,
        content_sha256 VARCHAR NOT NULL,
        byte_size UBIGINT NOT NULL CHECK (byte_size >= 0),
        object_uri VARCHAR NOT NULL,
        source_uri VARCHAR NOT NULL,
        source_modified_ns BIGINT,
        source_etag VARCHAR,
        original_name VARCHAR,
        media_type VARCHAR,
        captured_at TIMESTAMPTZ NOT NULL,
        manifest_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS normalized_artifact (
        artifact_id VARCHAR PRIMARY KEY,
        normalization_run_id VARCHAR,
        input_revision_id VARCHAR,
        content_sha256 VARCHAR NOT NULL,
        source_snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        dataset_kind VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        rule_version VARCHAR,
        row_count UBIGINT NOT NULL CHECK (row_count >= 0),
        byte_size UBIGINT NOT NULL CHECK (byte_size >= 0),
        parquet_uri VARCHAR NOT NULL,
        partition_json JSON,
        arrow_schema VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_contract (
        contract_id VARCHAR PRIMARY KEY,
        logical_key VARCHAR NOT NULL,
        enterprise_id VARCHAR NOT NULL,
        store_id VARCHAR NOT NULL,
        platform_code VARCHAR NOT NULL,
        contract_version INTEGER NOT NULL CHECK (contract_version > 0),
        effective_from DATE NOT NULL,
        effective_to DATE,
        status VARCHAR NOT NULL
            CHECK (status IN ('draft', 'active', 'retired')),
        definition_json JSON NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (logical_key, contract_version),
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounting_period (
        period_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        store_id VARCHAR NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('open', 'preclosed', 'closed', 'restated')),
        revision_no INTEGER NOT NULL DEFAULT 1 CHECK (revision_no > 0),
        closed_at TIMESTAMPTZ,
        closed_by VARCHAR,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (contract_id, period_start, revision_no),
        CHECK (period_end >= period_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS business_decision (
        decision_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        subject_kind VARCHAR NOT NULL,
        question VARCHAR NOT NULL,
        business_impact VARCHAR NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('pending', 'decided', 'superseded')),
        decision_json JSON,
        decided_by VARCHAR,
        decided_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (contract_id, subject_kind, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS business_decision_event (
        event_id VARCHAR PRIMARY KEY,
        decision_id VARCHAR NOT NULL REFERENCES business_decision(decision_id),
        action VARCHAR NOT NULL CHECK (action IN ('decide', 'reopen')),
        payload_json JSON NOT NULL,
        actor VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_log (
        run_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR REFERENCES accounting_period(period_id),
        run_kind VARCHAR NOT NULL
            CHECK (run_kind IN ('freeze', 'parse', 'reconcile', 'adjudicate', 'baseline', 'blind')),
        status VARCHAR NOT NULL
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        code_sha VARCHAR,
        input_manifest_sha256 VARCHAR,
        rule_set_sha256 VARCHAR,
        started_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        finished_at TIMESTAMPTZ,
        error_code VARCHAR,
        error_detail VARCHAR,
        metrics_json JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS compute_job (
        job_id VARCHAR PRIMARY KEY,
        cycle_id VARCHAR NOT NULL,
        job_kind VARCHAR NOT NULL
            CHECK (
                job_kind IN (
                    'inventory', 'freeze', 'profile',
                    'normalize', 'reconcile'
                )
            ),
        contract_id VARCHAR,
        period_id VARCHAR,
        store_id VARCHAR,
        period_token VARCHAR,
        status VARCHAR NOT NULL
            CHECK (
                status IN (
                    'queued', 'running', 'succeeded',
                    'failed', 'cancelled'
                )
            ),
        progress_percent INTEGER NOT NULL DEFAULT 0
            CHECK (progress_percent BETWEEN 0 AND 100),
        business_label VARCHAR NOT NULL,
        detail VARCHAR,
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        error_detail VARCHAR,
        metrics_json JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_profile (
        profile_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        parser_version VARCHAR NOT NULL,
        status VARCHAR NOT NULL
            CHECK (
                status IN (
                    'matched', 'unmatched', 'ambiguous', 'unsupported', 'failed'
                )
            ),
        source_kind VARCHAR,
        template_id VARCHAR,
        fingerprint_sha256 VARCHAR,
        route_json JSON,
        error_detail VARCHAR,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (snapshot_id, parser_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS input_revision (
        revision_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        source_kind VARCHAR NOT NULL,
        logical_input_key VARCHAR NOT NULL,
        revision_no INTEGER NOT NULL CHECK (revision_no > 0),
        snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        -- Kept as a validated soft reference because DuckDB otherwise blocks
        -- status-only updates on a revision referenced by its successor.
        supersedes_revision_id VARCHAR,
        status VARCHAR NOT NULL
            CHECK (status IN ('candidate', 'current', 'superseded', 'rejected')),
        reason VARCHAR,
        approved_by VARCHAR,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (contract_id, period_id, logical_input_key, revision_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS input_revision_state (
        revision_id VARCHAR PRIMARY KEY,
        status VARCHAR NOT NULL
            CHECK (status IN ('candidate', 'current', 'superseded', 'rejected')),
        reason VARCHAR,
        approved_by VARCHAR,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rule_definition (
        rule_id VARCHAR PRIMARY KEY,
        logical_key VARCHAR NOT NULL UNIQUE,
        rule_kind VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        description VARCHAR,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rule_version (
        rule_version_id VARCHAR PRIMARY KEY,
        rule_id VARCHAR NOT NULL REFERENCES rule_definition(rule_id),
        version INTEGER NOT NULL CHECK (version > 0),
        effective_from DATE NOT NULL,
        effective_to DATE,
        status VARCHAR NOT NULL
            CHECK (status IN ('draft', 'approved', 'retired', 'rejected')),
        definition_json JSON NOT NULL,
        checksum_sha256 VARCHAR NOT NULL,
        source_evidence_json JSON,
        approved_by VARCHAR,
        approved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (rule_id, version),
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_record (
        evidence_id VARCHAR PRIMARY KEY,
        run_id VARCHAR REFERENCES run_log(run_id),
        snapshot_id VARCHAR REFERENCES source_snapshot(snapshot_id),
        artifact_id VARCHAR REFERENCES normalized_artifact(artifact_id),
        source_locator VARCHAR,
        source_row_key VARCHAR,
        rule_version_id VARCHAR REFERENCES rule_version(rule_version_id),
        evidence_kind VARCHAR NOT NULL,
        payload_json JSON NOT NULL,
        payload_sha256 VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_binding (
        binding_id VARCHAR PRIMARY KEY,
        evidence_id VARCHAR NOT NULL REFERENCES evidence_record(evidence_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        artifact_id VARCHAR REFERENCES normalized_artifact(artifact_id),
        source_member VARCHAR,
        source_sheet VARCHAR,
        row_no BIGINT NOT NULL CHECK (row_no > 0),
        field VARCHAR,
        source_value VARCHAR,
        normalization_version VARCHAR,
        rule_version_id VARCHAR REFERENCES rule_version(rule_version_id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (evidence_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_item (
        item_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        source_kind VARCHAR NOT NULL,
        source_record_key VARCHAR NOT NULL,
        side VARCHAR NOT NULL
            CHECK (side IN ('order', 'platform', 'fund', 'cost', 'freight', 'advertising')),
        business_key VARCHAR,
        settlement_batch_key VARCHAR,
        cash_bridge_key VARCHAR,
        event_date DATE,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        amount DECIMAL(38,4) NOT NULL,
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        attributes_json JSON,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (run_id, source_kind, source_record_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_link (
        link_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        business_key VARCHAR,
        cash_bridge_key VARCHAR,
        link_scope VARCHAR
            CHECK (link_scope IN ('order_platform', 'platform_cash', 'three_way')),
        link_kind VARCHAR NOT NULL
            CHECK (link_kind IN ('one_to_one', 'one_to_many', 'many_to_one', 'many_to_many')),
        status VARCHAR NOT NULL
            CHECK (status IN ('proposed', 'confirmed', 'rejected')),
        rule_version_id VARCHAR REFERENCES rule_version(rule_version_id),
        rationale VARCHAR,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_link_member (
        link_id VARCHAR NOT NULL REFERENCES reconciliation_link(link_id),
        item_id VARCHAR NOT NULL REFERENCES reconciliation_item(item_id),
        member_role VARCHAR NOT NULL,
        allocated_amount DECIMAL(38,4),
        PRIMARY KEY (link_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_balance (
        balance_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        balance_key VARCHAR NOT NULL,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        expected_amount DECIMAL(38,4) NOT NULL,
        actual_amount DECIMAL(38,4) NOT NULL,
        matched_amount DECIMAL(38,4) NOT NULL,
        difference_amount DECIMAL(38,4) NOT NULL,
        order_amount DECIMAL(38,4),
        platform_amount DECIMAL(38,4),
        cash_amount DECIMAL(38,4),
        order_to_platform_difference DECIMAL(38,4),
        platform_to_cash_difference DECIMAL(38,4),
        status VARCHAR NOT NULL
            CHECK (status IN ('balanced', 'partial', 'unresolved')),
        evidence_json JSON,
        UNIQUE (run_id, balance_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unresolved_balance (
        unresolved_id VARCHAR PRIMARY KEY,
        balance_id VARCHAR NOT NULL REFERENCES reconciliation_balance(balance_id),
        reason_code VARCHAR NOT NULL,
        amount DECIMAL(38,4) NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('open', 'explained', 'adjusted', 'rejected')),
        owner VARCHAR,
        due_at TIMESTAMPTZ,
        explanation VARCHAR,
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        resolved_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjustment_entry (
        adjustment_id VARCHAR PRIMARY KEY,
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        original_period_id VARCHAR REFERENCES accounting_period(period_id),
        unresolved_id VARCHAR REFERENCES unresolved_balance(unresolved_id),
        amount DECIMAL(38,4) NOT NULL,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        reason VARCHAR NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('draft', 'approved', 'posted', 'reversed')),
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        approved_by VARCHAR,
        approved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checklist_requirement (
        requirement_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        source_kind VARCHAR NOT NULL,
        store_scope VARCHAR NOT NULL,
        required BOOLEAN NOT NULL DEFAULT true,
        effective_from DATE NOT NULL,
        effective_to DATE,
        expected_frequency VARCHAR NOT NULL,
        definition_json JSON,
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checklist_result (
        result_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        requirement_id VARCHAR NOT NULL REFERENCES checklist_requirement(requirement_id),
        status VARCHAR NOT NULL
            CHECK (status IN ('pending', 'present', 'missing', 'failed', 'not_applicable')),
        -- DuckDB implements UPDATE as delete+insert for referenced rows and
        -- therefore blocks status-only updates to input_revision when this is
        -- a physical FK. The Harness validates this trace reference at schema
        -- initialization instead.
        revision_id VARCHAR,
        observed_json JSON,
        checked_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (run_id, period_id, requirement_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cost_asof (
        cost_asof_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        sku_key VARCHAR NOT NULL,
        cost_version VARCHAR NOT NULL,
        valid_from TIMESTAMPTZ NOT NULL,
        valid_to TIMESTAMPTZ,
        unit_cost DECIMAL(38,4) NOT NULL,
        currency VARCHAR NOT NULL DEFAULT 'CNY',
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        adjudication_id VARCHAR,
        CHECK (valid_to IS NULL OR valid_to >= valid_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS freight_exception (
        exception_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        stable_source_key VARCHAR NOT NULL,
        target_business_key VARCHAR NOT NULL,
        valid_from DATE NOT NULL,
        valid_to DATE,
        reason VARCHAR NOT NULL,
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        approved_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        CHECK (valid_to IS NULL OR valid_to >= valid_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS historical_output (
        historical_output_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        output_kind VARCHAR NOT NULL,
        source_label VARCHAR NOT NULL,
        totals_json JSON NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('candidate', 'competing', 'adjudicated', 'rejected')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjudication (
        adjudication_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR REFERENCES accounting_period(period_id),
        subject_kind VARCHAR NOT NULL,
        subject_key VARCHAR NOT NULL,
        finding_json JSON NOT NULL,
        decision VARCHAR NOT NULL
            CHECK (
                decision IN (
                    'accept_engine', 'accept_history', 'adjust_rule', 'defer', 'reject'
                )
            ),
        rationale VARCHAR NOT NULL,
        evidence_json JSON NOT NULL,
        decided_by VARCHAR NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS baseline (
        baseline_id VARCHAR PRIMARY KEY,
        contract_id VARCHAR NOT NULL REFERENCES reconciliation_contract(contract_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        baseline_version INTEGER NOT NULL CHECK (baseline_version > 0),
        input_manifest_sha256 VARCHAR NOT NULL,
        rule_set_sha256 VARCHAR NOT NULL,
        code_sha VARCHAR NOT NULL,
        output_sha256 VARCHAR NOT NULL,
        invariant_report_json JSON NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('candidate', 'frozen', 'superseded')),
        frozen_by VARCHAR,
        frozen_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (contract_id, period_id, baseline_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rule_decision (
        rule_decision_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        item_id VARCHAR REFERENCES reconciliation_item(item_id),
        rule_version_id VARCHAR NOT NULL REFERENCES rule_version(rule_version_id),
        outcome_json JSON NOT NULL,
        evidence_id VARCHAR REFERENCES evidence_record(evidence_id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_edge (
        edge_id VARCHAR PRIMARY KEY,
        from_kind VARCHAR NOT NULL,
        from_id VARCHAR NOT NULL,
        to_kind VARCHAR NOT NULL,
        to_id VARCHAR NOT NULL,
        relationship VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (from_kind, from_id, to_kind, to_id, relationship)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pnl_cell (
        pnl_cell_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        store_id VARCHAR NOT NULL,
        sku_key VARCHAR NOT NULL,
        metric VARCHAR NOT NULL,
        definition_id VARCHAR NOT NULL,
        value DECIMAL(38,4) NOT NULL,
        evidence_json JSON NOT NULL,
        UNIQUE (run_id, store_id, sku_key, metric, definition_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_identity (
        person_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        display_name VARCHAR NOT NULL,
        department VARCHAR,
        employment_type VARCHAR,
        status VARCHAR NOT NULL
            CHECK (status IN ('active', 'provisional', 'inactive')),
        source_snapshot_id VARCHAR REFERENCES source_snapshot(snapshot_id),
        source_row_no BIGINT,
        identity_checksum VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (enterprise_id, identity_checksum)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_alias (
        alias_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        person_id VARCHAR NOT NULL REFERENCES person_identity(person_id),
        alias_text VARCHAR NOT NULL,
        normalized_alias VARCHAR NOT NULL,
        effective_from DATE NOT NULL,
        effective_to DATE,
        source_snapshot_id VARCHAR REFERENCES source_snapshot(snapshot_id),
        source_row_no BIGINT,
        status VARCHAR NOT NULL
            CHECK (status IN ('active', 'superseded', 'rejected')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS canonical_product (
        product_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        merchant_product_code VARCHAR NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('active', 'provisional', 'inactive')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (enterprise_id, merchant_product_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS responsibility_assignment_version (
        assignment_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        person_id VARCHAR NOT NULL REFERENCES person_identity(person_id),
        product_id VARCHAR NOT NULL REFERENCES canonical_product(product_id),
        store_id VARCHAR,
        store_name VARCHAR,
        responsibility_type VARCHAR NOT NULL DEFAULT 'primary_operator',
        allocation_ratio DECIMAL(8,4) NOT NULL DEFAULT 1.0000
            CHECK (allocation_ratio > 0 AND allocation_ratio <= 1),
        effective_from DATE NOT NULL,
        effective_to DATE NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        status VARCHAR NOT NULL
            CHECK (status IN ('active', 'superseded', 'conflict', 'rejected')),
        source_kind VARCHAR NOT NULL,
        source_snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        source_sheet VARCHAR,
        source_row_no BIGINT NOT NULL CHECK (source_row_no > 0),
        checksum_sha256 VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        CHECK (effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_source_import (
        import_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        source_kind VARCHAR NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        row_count UBIGINT NOT NULL DEFAULT 0,
        issue_count UBIGINT NOT NULL DEFAULT 0,
        metrics_json JSON,
        started_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        finished_at TIMESTAMPTZ,
        error_detail VARCHAR,
        UNIQUE (enterprise_id, snapshot_id, source_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_policy_version (
        policy_version_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        policy_code VARCHAR NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        effective_from DATE NOT NULL,
        effective_to DATE,
        status VARCHAR NOT NULL
            CHECK (status IN ('draft', 'approved', 'retired')),
        definition_json JSON NOT NULL,
        checksum_sha256 VARCHAR NOT NULL,
        approved_by VARCHAR,
        approved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (enterprise_id, policy_code, version),
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_reference_fact (
        reference_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR NOT NULL,
        period_token VARCHAR NOT NULL,
        store_name VARCHAR NOT NULL,
        person_id VARCHAR REFERENCES person_identity(person_id),
        product_id VARCHAR REFERENCES canonical_product(product_id),
        calculation_mode VARCHAR NOT NULL
            CHECK (calculation_mode IN ('single', 'combined')),
        collected_amount DECIMAL(38,4) NOT NULL,
        refund_amount DECIMAL(38,4) NOT NULL,
        compensation_amount DECIMAL(38,4) NOT NULL,
        software_fee DECIMAL(38,4) NOT NULL,
        marketing_fee DECIMAL(38,4) NOT NULL,
        shipping_fee DECIMAL(38,4) NOT NULL,
        product_cost DECIMAL(38,4) NOT NULL,
        reship_cost DECIMAL(38,4) NOT NULL,
        principal_commission DECIMAL(38,4) NOT NULL,
        procurement_amount DECIMAL(38,4) NOT NULL,
        gross_profit DECIMAL(38,4) NOT NULL,
        advertising_fee DECIMAL(38,4) NOT NULL,
        store_profit DECIMAL(38,4) NOT NULL,
        gross_formula_residual DECIMAL(38,4) NOT NULL,
        profit_formula_residual DECIMAL(38,4) NOT NULL,
        validation_status VARCHAR NOT NULL
            CHECK (validation_status IN ('passed', 'failed')),
        source_snapshot_id VARCHAR NOT NULL REFERENCES source_snapshot(snapshot_id),
        source_row_no BIGINT NOT NULL CHECK (source_row_no > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
        UNIQUE (source_snapshot_id, source_row_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_result (
        result_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        enterprise_id VARCHAR NOT NULL,
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        person_id VARCHAR NOT NULL REFERENCES person_identity(person_id),
        store_id VARCHAR NOT NULL,
        product_id VARCHAR REFERENCES canonical_product(product_id),
        policy_version_id VARCHAR NOT NULL
            REFERENCES performance_policy_version(policy_version_id),
        collected_amount DECIMAL(38,4) NOT NULL,
        refund_amount DECIMAL(38,4) NOT NULL,
        direct_cost DECIMAL(38,4) NOT NULL,
        allocated_cost DECIMAL(38,4) NOT NULL,
        operating_profit DECIMAL(38,4) NOT NULL,
        completeness_ratio DECIMAL(8,6) NOT NULL
            CHECK (completeness_ratio >= 0 AND completeness_ratio <= 1),
        status VARCHAR NOT NULL
            CHECK (status IN ('complete', 'incomplete', 'superseded')),
        evidence_policy_version VARCHAR,
        engine_version VARCHAR,
        evidence_json JSON NOT NULL,
        checksum_sha256 VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_result_head (
        scope_key VARCHAR PRIMARY KEY,
        result_id VARCHAR NOT NULL REFERENCES performance_result(result_id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diff_finding (
        diff_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
        period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
        metric VARCHAR NOT NULL,
        rule_version_id VARCHAR REFERENCES rule_version(rule_version_id),
        source_row_key VARCHAR,
        engine_value DECIMAL(38,4),
        historical_value DECIMAL(38,4),
        difference_value DECIMAL(38,4) NOT NULL,
        difference_kind VARCHAR NOT NULL,
        status VARCHAR NOT NULL
            CHECK (status IN ('open', 'adjudicated', 'accepted', 'rejected')),
        evidence_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS residual_suggestion (
        suggestion_id VARCHAR PRIMARY KEY,
        unresolved_id VARCHAR NOT NULL REFERENCES unresolved_balance(unresolved_id),
        suggestion_kind VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        rationale VARCHAR NOT NULL,
        confidence DECIMAL(8,6) NOT NULL,
        source_model VARCHAR NOT NULL,
        candidate_json JSON NOT NULL,
        candidate_sha256 VARCHAR NOT NULL,
        guard_status VARCHAR NOT NULL,
        critic_status VARCHAR NOT NULL,
        evidence_policy_version VARCHAR,
        evidence_binding_sha256 VARCHAR,
        status VARCHAR NOT NULL
            CHECK (status IN ('suggestion', 'reviewed', 'rejected')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_decision (
        decision_id VARCHAR PRIMARY KEY,
        unresolved_id VARCHAR NOT NULL REFERENCES unresolved_balance(unresolved_id),
        suggestion_id VARCHAR REFERENCES residual_suggestion(suggestion_id),
        decision VARCHAR NOT NULL
            CHECK (decision IN ('explain', 'defer', 'reject', 'approve', 'replace')),
        final_action VARCHAR,
        reason VARCHAR NOT NULL,
        decided_by VARCHAR NOT NULL,
        candidate_sha256 VARCHAR,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS correction (
        correction_id VARCHAR PRIMARY KEY,
        suggestion_id VARCHAR REFERENCES residual_suggestion(suggestion_id),
        unresolved_id VARCHAR NOT NULL REFERENCES unresolved_balance(unresolved_id),
        feature_json JSON NOT NULL,
        model_outcome_json JSON,
        human_outcome_json JSON NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_call_log (
        llm_call_id VARCHAR PRIMARY KEY,
        purpose VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        request_sha256 VARCHAR NOT NULL,
        response_sha256 VARCHAR,
        request_log_uri VARCHAR NOT NULL,
        response_log_uri VARCHAR,
        status VARCHAR NOT NULL,
        elapsed_ms INTEGER,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS autonomy_evaluation (
        evaluation_id VARCHAR PRIMARY KEY,
        enterprise_id VARCHAR,
        category VARCHAR NOT NULL,
        model_version VARCHAR,
        policy_version VARCHAR,
        period_id VARCHAR REFERENCES accounting_period(period_id),
        current_level VARCHAR NOT NULL CHECK (current_level IN ('L0', 'L1', 'L2')),
        proposed_level VARCHAR NOT NULL CHECK (proposed_level IN ('L0', 'L1', 'L2')),
        eligible BOOLEAN,
        precision DECIMAL(8,6),
        major_error_count INTEGER NOT NULL DEFAULT 0,
        cumulative_exposure DECIMAL(38,4) NOT NULL DEFAULT 0,
        sample_count INTEGER NOT NULL DEFAULT 0,
        reason_json JSON,
        metrics_json JSON,
        source_digest VARCHAR,
        rationale VARCHAR NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS enterprise_id VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS model_version VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS eligible BOOLEAN
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS reason_json JSON
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS metrics_json JSON
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS source_digest VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS autonomy_evaluation
    ADD COLUMN IF NOT EXISTS policy_version VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS residual_suggestion
    ADD COLUMN IF NOT EXISTS evidence_policy_version VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS residual_suggestion
    ADD COLUMN IF NOT EXISTS evidence_binding_sha256 VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS performance_result
    ADD COLUMN IF NOT EXISTS evidence_policy_version VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS performance_result
    ADD COLUMN IF NOT EXISTS engine_version VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS normalized_artifact
    ADD COLUMN IF NOT EXISTS normalization_run_id VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS normalized_artifact
    ADD COLUMN IF NOT EXISTS input_revision_id VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_item
    ADD COLUMN IF NOT EXISTS settlement_batch_key VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_item
    ADD COLUMN IF NOT EXISTS cash_bridge_key VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_link
    ADD COLUMN IF NOT EXISTS business_key VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_link
    ADD COLUMN IF NOT EXISTS cash_bridge_key VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_link
    ADD COLUMN IF NOT EXISTS link_scope VARCHAR
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_balance
    ADD COLUMN IF NOT EXISTS order_amount DECIMAL(38,4)
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_balance
    ADD COLUMN IF NOT EXISTS platform_amount DECIMAL(38,4)
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_balance
    ADD COLUMN IF NOT EXISTS cash_amount DECIMAL(38,4)
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_balance
    ADD COLUMN IF NOT EXISTS order_to_platform_difference DECIMAL(38,4)
    """,
    """
    ALTER TABLE IF EXISTS reconciliation_balance
    ADD COLUMN IF NOT EXISTS platform_to_cash_difference DECIMAL(38,4)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_item_business_key
    ON reconciliation_item(business_key)
    """,
    "CREATE INDEX IF NOT EXISTS idx_item_period_side ON reconciliation_item(period_id, side)",
    "CREATE INDEX IF NOT EXISTS idx_revision_current ON input_revision(period_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_revision_state_status ON input_revision_state(status)",
    "CREATE INDEX IF NOT EXISTS idx_unresolved_status ON unresolved_balance(status)",
    "CREATE INDEX IF NOT EXISTS idx_snapshot_digest ON source_snapshot(content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_digest ON normalized_artifact(content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_compute_job_status ON compute_job(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_compute_job_scope ON compute_job(store_id, period_token)",
)
