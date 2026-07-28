from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Base, engine
from app.models import Enterprise, UserAccount
from app.reconciliation.domain import (
    DomainError,
    approve_adjustment,
    approve_certification,
    certification_eligibility,
    compile_rule_version,
    publish_contract_version,
    publish_rule_version,
    submit_adjustment,
    transition_period,
)
from app.reconciliation.models import (
    AccountingPeriod,
    AdjustmentEntry,
    AgentEnrollmentToken,
    AgentJob,
    AgentJobEvent,
    CertificationGateResult,
    CertificationHead,
    CertificationRun,
    CertificationVersion,
    ContractSourceRole,
    DiscoveredFile,
    EvidenceEdge,
    ExternalAgent,
    ReconContract,
    ReconContractVersion,
    ReconDifference,
    ReconLink,
    RestatementVersion,
    ReviewDecision,
    ReviewItem,
    RuleCompileArtifact,
    RuleDecision,
    RuleItem,
    RulePackage,
    RulePackageVersion,
    SourceConnector,
    SourceFile,
    SourceRow,
)


NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()


def _identity(db: Session) -> tuple[Enterprise, UserAccount, UserAccount]:
    enterprise = Enterprise(
        name="Reconciliation Test Enterprise",
        status="active",
        version=1,
        activation_at=NOW,
        effective_from=NOW,
        created_by="bootstrap",
    )
    db.add(enterprise)
    db.flush()

    def user(name: str, email: str) -> UserAccount:
        return UserAccount(
            enterprise_id=enterprise.id,
            name=name,
            email=email,
            role="admin",
            store_ids=[],
            password_hash="not-used-in-domain-tests",
            status="active",
            version=1,
            effective_from=NOW,
            created_by="bootstrap",
        )

    maker = user("Maker", "maker@recon.test")
    checker = user("Checker", "checker@recon.test")
    db.add_all([maker, checker])
    db.flush()
    return enterprise, maker, checker


def _contract(
    db: Session,
    enterprise: Enterprise,
    maker: UserAccount,
    logical_key: str = "domestic-three-way",
) -> ReconContractVersion:
    contract = ReconContract(
        enterprise_id=enterprise.id,
        logical_key=logical_key,
        name="Domestic three-way reconciliation",
        status="active",
        created_by=maker.id,
    )
    db.add(contract)
    db.flush()
    version = ReconContractVersion(
        enterprise_id=enterprise.id,
        contract_id=contract.id,
        version=1,
        status="draft",
        reporting_currency="cny",
        business_timezone="Asia/Shanghai",
        amount_scale=4,
        rounding_mode="half_even",
        period_cutoff_policy="calendar_day",
        refund_direction="negative",
        fee_direction="negative",
        tax_inclusion_policy="source_defined",
        matching_strategy="ordered_stages",
        matching_keys=["order_id", "transaction_id"],
        matching_window_seconds=7 * 24 * 60 * 60,
        max_candidates_per_row=20,
        amount_tolerance=Decimal("0.0100"),
        effective_from=NOW,
        created_by=maker.id,
    )
    db.add(version)
    db.flush()
    db.add_all(
        [
            ContractSourceRole(
                enterprise_id=enterprise.id,
                contract_version_id=version.id,
                role_code="order",
                source_kind="order_detail",
                required=True,
                min_files=1,
                business_key_fields=["order_id"],
            ),
            ContractSourceRole(
                enterprise_id=enterprise.id,
                contract_version_id=version.id,
                role_code="settlement",
                source_kind="platform_settlement",
                required=True,
                min_files=1,
                business_key_fields=["transaction_id"],
            ),
        ]
    )
    db.flush()
    return version


def _rules(
    db: Session, enterprise: Enterprise, maker: UserAccount
) -> RulePackageVersion:
    package = RulePackage(
        enterprise_id=enterprise.id,
        logical_key="domestic-core",
        name="Domestic deterministic rules",
        status="active",
        created_by=maker.id,
    )
    db.add(package)
    db.flush()
    version = RulePackageVersion(
        enterprise_id=enterprise.id,
        package_id=package.id,
        version=1,
        status="draft",
        created_by=maker.id,
    )
    db.add(version)
    db.flush()
    db.add(
        RuleItem(
            enterprise_id=enterprise.id,
            rule_version_id=version.id,
            rule_key="exact-order-source",
            rule_type="exact",
            priority=10,
            enabled=True,
            configuration={"field": "source_type", "value": "order"},
        )
    )
    db.flush()
    compile_rule_version(db, version)
    publish_rule_version(db, version, maker.id)
    return version


def _period(
    db: Session, enterprise: Enterprise, maker: UserAccount
) -> AccountingPeriod:
    period = AccountingPeriod(
        enterprise_id=enterprise.id,
        period_key="2026-07",
        starts_at=NOW,
        ends_at=NOW + timedelta(days=31),
        state="open",
        changed_by=maker.id,
    )
    db.add(period)
    db.flush()
    return period


def _published_foundation(db: Session):
    enterprise, maker, checker = _identity(db)
    contract = _contract(db, enterprise, maker)
    checksum = publish_contract_version(db, contract, maker.id)
    rules = _rules(db, enterprise, maker)
    period = _period(db, enterprise, maker)
    return enterprise, maker, checker, contract, rules, period, checksum


def _certification_run(
    db: Session,
    enterprise: Enterprise,
    maker: UserAccount,
    contract: ReconContractVersion,
    rules: RulePackageVersion,
    period: AccountingPeriod,
) -> CertificationRun:
    run = CertificationRun(
        enterprise_id=enterprise.id,
        contract_version_id=contract.id,
        rule_version_id=rules.id,
        period_id=period.id,
        scope_key="store:main",
        source_set_checksum="a" * 64,
        engine_version="engine-v1",
        state="draft",
        proposed_by=maker.id,
    )
    db.add(run)
    db.flush()
    return run


def test_contract_publish_versions_currency_timezone_and_required_sources(db: Session):
    enterprise, maker, _ = _identity(db)
    contract = _contract(db, enterprise, maker)

    checksum = publish_contract_version(db, contract, maker.id)

    assert len(checksum) == 64
    assert contract.reporting_currency == "CNY"
    assert contract.status == "published"
    assert contract.checksum == checksum
    with pytest.raises(DomainError, match="only draft"):
        publish_contract_version(db, contract, maker.id)

    invalid = _contract(db, enterprise, maker, "invalid-window-version")
    invalid.matching_window_seconds = 31 * 24 * 60 * 60 + 1
    with pytest.raises(DomainError, match="31-day"):
        publish_contract_version(db, invalid, maker.id)
    invalid.matching_window_seconds = 31 * 24 * 60 * 60

    inactive = UserAccount(
        enterprise_id=enterprise.id,
        name="Inactive approver",
        email="inactive@recon.test",
        role="admin",
        store_ids=[],
        password_hash="disabled",
        status="inactive",
        version=1,
        effective_from=NOW,
        created_by="bootstrap",
    )
    db.add(inactive)
    db.flush()
    actor_guarded = _contract(db, enterprise, maker, "actor-guarded-contract")
    with pytest.raises(DomainError, match="active user"):
        publish_contract_version(db, actor_guarded, inactive.id)


def test_rule_compiler_accepts_only_typed_bounded_deterministic_rules(db: Session):
    enterprise, maker, _ = _identity(db)
    package = RulePackage(
        enterprise_id=enterprise.id,
        logical_key="all-types",
        name="All bounded rule types",
        status="active",
        created_by=maker.id,
    )
    db.add(package)
    db.flush()
    version = RulePackageVersion(
        enterprise_id=enterprise.id,
        package_id=package.id,
        version=1,
        status="draft",
        created_by=maker.id,
    )
    db.add(version)
    db.flush()
    configurations = [
        ("exact", {"field": "memo", "value": "settlement"}),
        ("contains", {"field": "memo", "value": "commission"}),
        ("prefix", {"field": "order_id", "value": "TB"}),
        ("suffix", {"field": "order_id", "value": "-R"}),
        (
            "bounded_regex",
            {
                "field": "memo",
                "pattern": r"[A-Z]{2}-[0-9]{1,16}",
                "max_input_length": 512,
            },
        ),
        ("field_mapping", {"source_field": "收入", "target_field": "revenue"}),
        (
            "order_id_extract",
            {"field": "memo", "separators": ["/", "-"], "max_length": 128},
        ),
        ("amount_direction", {"field": "fee", "direction": "negative"}),
        (
            "bounded_window_link",
            {
                "keys": ["order_id", "amount"],
                "window_seconds": 86400,
                "max_candidates": 25,
            },
        ),
    ]
    for priority, (rule_type, configuration) in enumerate(configurations, start=1):
        db.add(
            RuleItem(
                enterprise_id=enterprise.id,
                rule_version_id=version.id,
                rule_key=f"rule-{priority}",
                rule_type=rule_type,
                priority=priority,
                enabled=True,
                configuration=configuration,
            )
        )
    db.flush()

    first = compile_rule_version(db, version)
    second = compile_rule_version(db, version)

    assert first.id == second.id
    assert first.item_count == 9
    assert len(first.checksum) == 64
    assert (
        db.scalar(
            select(func.count(RuleCompileArtifact.id)).where(
                RuleCompileArtifact.rule_version_id == version.id
            )
        )
        == 1
    )

    invalid_version = RulePackageVersion(
        enterprise_id=enterprise.id,
        package_id=package.id,
        version=2,
        status="draft",
        created_by=maker.id,
    )
    db.add(invalid_version)
    db.flush()
    db.add(
        RuleItem(
            enterprise_id=enterprise.id,
            rule_version_id=invalid_version.id,
            rule_key="unsafe-regex",
            rule_type="bounded_regex",
            priority=1,
            enabled=True,
            configuration={
                "field": "memo",
                "pattern": r"(a+)+",
                "max_input_length": 4096,
            },
        )
    )
    db.flush()
    with pytest.raises(DomainError, match="quantifier"):
        compile_rule_version(db, invalid_version)


def test_accounting_period_only_moves_open_preclosed_closed(db: Session):
    enterprise, maker, checker = _identity(db)
    period = _period(db, enterprise, maker)

    with pytest.raises(DomainError, match="invalid period transition"):
        transition_period(db, period, "closed", checker.id)
    transition_period(db, period, "preclosed", checker.id)
    assert period.preclosed_at is not None
    transition_period(db, period, "closed", checker.id)
    assert period.closed_at is not None
    with pytest.raises(DomainError, match="invalid period transition"):
        transition_period(db, period, "open", checker.id)


def test_certification_requires_all_gates_and_maker_checker(db: Session):
    enterprise, maker, checker, contract, rules, period, _ = _published_foundation(db)
    run = _certification_run(db, enterprise, maker, contract, rules, period)
    completeness = CertificationGateResult(
        enterprise_id=enterprise.id,
        certification_run_id=run.id,
        gate_code="required_sources_complete",
        required=True,
        status="passed",
        actual_value="2",
        expected_value="2",
        evidence={"source_roles": ["order", "settlement"]},
    )
    balance = CertificationGateResult(
        enterprise_id=enterprise.id,
        certification_run_id=run.id,
        gate_code="three_way_balance",
        required=True,
        status="failed",
        actual_value="10.00",
        expected_value="0.00",
        difference="10.00",
        evidence={},
    )
    optional = CertificationGateResult(
        enterprise_id=enterprise.id,
        certification_run_id=run.id,
        gate_code="optional_comment",
        required=False,
        status="not_applicable",
        evidence={},
    )
    db.add_all([completeness, balance, optional])
    db.flush()

    eligible, blockers = certification_eligibility(db, run)
    assert not eligible
    assert blockers == ["three_way_balance"]
    with pytest.raises(DomainError, match="not satisfied"):
        approve_certification(db, run, checker.id, {"balanced_amount": "0.0000"})

    balance.status = "passed"
    balance.difference = "0.00"
    db.flush()
    eligible, blockers = certification_eligibility(db, run)
    assert eligible and blockers == []
    with pytest.raises(DomainError, match="maker and checker"):
        approve_certification(db, run, maker.id, {"balanced_amount": "0.0000"})

    version = approve_certification(
        db, run, checker.id, {"balanced_amount": "0.0000", "currency": "CNY"}
    )
    head = db.scalar(
        select(CertificationHead).where(
            CertificationHead.current_version_id == version.id
        )
    )
    assert version.version == 1
    assert len(version.payload_checksum) == 64
    assert head is not None
    assert run.state == "certified"


def test_closed_period_rejects_normal_certification_and_allows_adjustment_only(
    db: Session,
):
    enterprise, maker, checker, contract, rules, period, _ = _published_foundation(db)
    run = _certification_run(db, enterprise, maker, contract, rules, period)
    db.add(
        CertificationGateResult(
            enterprise_id=enterprise.id,
            certification_run_id=run.id,
            gate_code="all_required",
            required=True,
            status="passed",
            evidence={},
        )
    )
    db.flush()
    certification = approve_certification(
        db, run, checker.id, {"total": "100.0000", "currency": "CNY"}
    )
    transition_period(db, period, "preclosed", checker.id)
    transition_period(db, period, "closed", checker.id)

    late_run = _certification_run(db, enterprise, maker, contract, rules, period)
    db.add(
        CertificationGateResult(
            enterprise_id=enterprise.id,
            certification_run_id=late_run.id,
            gate_code="all_required",
            required=True,
            status="passed",
            evidence={},
        )
    )
    db.flush()
    eligible, blockers = certification_eligibility(db, late_run)
    assert not eligible
    assert "period_closed" in blockers

    adjustment = AdjustmentEntry(
        enterprise_id=enterprise.id,
        period_id=period.id,
        contract_version_id=contract.id,
        base_certification_version_id=certification.id,
        state="draft",
        reason_code="late_settlement",
        rationale="Settlement arrived after final close.",
        amount=Decimal("5.2500"),
        currency="CNY",
        payload={"source_reference": "late-file"},
        created_by=maker.id,
    )
    db.add(adjustment)
    db.flush()
    submit_adjustment(db, adjustment, maker.id)
    with pytest.raises(DomainError, match="maker and checker"):
        approve_adjustment(db, adjustment, maker.id)
    restatement = approve_adjustment(db, adjustment, checker.id)

    assert adjustment.state == "approved"
    assert restatement.version == 1
    assert restatement.payload["base_payload_checksum"] == certification.payload_checksum
    assert db.scalar(select(func.count(RestatementVersion.id))) == 1


def test_adjustment_cannot_be_used_to_bypass_an_open_period(db: Session):
    enterprise, maker, checker, contract, rules, period, _ = _published_foundation(db)
    run = _certification_run(db, enterprise, maker, contract, rules, period)
    db.add(
        CertificationGateResult(
            enterprise_id=enterprise.id,
            certification_run_id=run.id,
            gate_code="all_required",
            required=True,
            status="passed",
            evidence={},
        )
    )
    db.flush()
    certification = approve_certification(db, run, checker.id, {"total": "1.0000"})
    adjustment = AdjustmentEntry(
        enterprise_id=enterprise.id,
        period_id=period.id,
        contract_version_id=contract.id,
        base_certification_version_id=certification.id,
        state="draft",
        reason_code="not-late",
        rationale="This must use a normal run.",
        amount=Decimal("1.0000"),
        currency="CNY",
        payload={},
        created_by=maker.id,
    )
    db.add(adjustment)
    db.flush()
    with pytest.raises(DomainError, match="reserved for closed"):
        submit_adjustment(db, adjustment, maker.id)


def test_evidence_lineage_review_and_external_agent_control_records(db: Session):
    enterprise, maker, checker, contract, rules, period, _ = _published_foundation(db)
    agent = ExternalAgent(
        enterprise_id=enterprise.id,
        machine_key="finance-win",
        display_name="Finance read-only agent",
        agent_key_hash="b" * 64,
        status="online",
        capabilities=["directory_scan", "deterministic_reconciliation"],
        version="1.0.0",
        last_heartbeat_at=NOW,
    )
    db.add(agent)
    db.flush()
    token = AgentEnrollmentToken(
        enterprise_id=enterprise.id,
        token_hash="c" * 64,
        expires_at=NOW + timedelta(hours=1),
        created_by=maker.id,
    )
    connector = SourceConnector(
        enterprise_id=enterprise.id,
        agent_id=agent.id,
        logical_key="finance-win-settlement",
        connector_type="directory",
        purpose="settlement",
        root_path=r"D:\KAOSHI\OneDrive\内贸\支付宝收支",
        read_policy={
            "open_mode": "read_only",
            "skip_offline": True,
            "stable_for_seconds": 600,
        },
        enabled=True,
    )
    db.add_all([token, connector])
    db.flush()
    job = AgentJob(
        enterprise_id=enterprise.id,
        agent_id=agent.id,
        connector_id=connector.id,
        job_type="scan_and_profile",
        state="running",
        priority=10,
        payload={"period": "2026-07"},
        result={},
        progress_current=1,
        progress_total=2,
        lease_owner="finance-win/worker-1",
        lease_expires_at=NOW + timedelta(minutes=5),
        started_at=NOW,
    )
    db.add(job)
    db.flush()
    event = AgentJobEvent(
        enterprise_id=enterprise.id,
        job_id=job.id,
        sequence=1,
        event_type="file_discovered",
        message="A stable settlement file was discovered.",
        details={"offline": False},
    )
    discovered = DiscoveredFile(
        enterprise_id=enterprise.id,
        connector_id=connector.id,
        path_key="d" * 64,
        full_path=r"D:\KAOSHI\OneDrive\内贸\支付宝收支\2026-07.xlsx",
        file_name="2026-07.xlsx",
        extension=".xlsx",
        size_bytes=3_500_000_000,
        observed_mtime=NOW,
        sha256="e" * 64,
        status="stable",
        safety_flags=[],
        last_seen_at=NOW,
    )
    source_file = SourceFile(
        enterprise_id=enterprise.id,
        connector_id=connector.id,
        sha256="e" * 64,
        object_key="raw/finance-win/2026-07.xlsx",
        original_path=discovered.full_path,
        size_bytes=3_500_000_000,
        observed_mtime=NOW,
        source_kind="settlement",
        raw_metadata={"agent_job_id": job.id},
    )
    db.add_all([event, discovered, source_file])
    db.flush()
    left = SourceRow(
        enterprise_id=enterprise.id,
        source_file_id=source_file.id,
        sheet_name="Sheet1",
        row_locator="Sheet1!2",
        row_number=2,
        row_hash="f" * 64,
        raw_amount_text="100.00",
        raw_currency="CNY",
        normalized_amount=Decimal("100.0000"),
        normalization_rule_version_id=rules.id,
        occurred_at=NOW,
        business_key="ORDER-1",
        payload={"order_id": "ORDER-1"},
    )
    right = SourceRow(
        enterprise_id=enterprise.id,
        source_file_id=source_file.id,
        sheet_name="Sheet1",
        row_locator="Sheet1!3",
        row_number=3,
        row_hash="0" * 64,
        raw_amount_text="-1.00",
        raw_currency="CNY",
        normalized_amount=Decimal("-1.0000"),
        normalization_rule_version_id=rules.id,
        occurred_at=NOW,
        business_key="ORDER-1",
        payload={"order_id": "ORDER-1"},
    )
    db.add_all([left, right])
    db.flush()
    rule_item = db.scalar(
        select(RuleItem).where(RuleItem.rule_version_id == rules.id)
    )
    assert rule_item is not None
    decision = RuleDecision(
        enterprise_id=enterprise.id,
        source_row_id=left.id,
        rule_item_id=rule_item.id,
        outcome="classified",
        output={"account": "sales"},
        engine_version="engine-v1",
    )
    link = ReconLink(
        enterprise_id=enterprise.id,
        contract_version_id=contract.id,
        period_id=period.id,
        left_row_id=left.id,
        right_row_id=right.id,
        link_type="order_to_settlement",
        match_key="ORDER-1",
        amount_difference=Decimal("99.0000"),
        state="ambiguous",
    )
    difference = ReconDifference(
        enterprise_id=enterprise.id,
        contract_version_id=contract.id,
        period_id=period.id,
        source_row_id=left.id,
        difference_type="amount_mismatch",
        amount=Decimal("99.0000"),
        status="open",
        details={"tolerance": "0.0100"},
    )
    db.add_all([decision, link, difference])
    db.flush()
    review = ReviewItem(
        enterprise_id=enterprise.id,
        subject_type="recon_difference",
        subject_id=difference.id,
        status="claimed",
        risk_level="high",
        assigned_to=checker.id,
        requested_by=maker.id,
    )
    db.add(review)
    db.flush()
    review_decision = ReviewDecision(
        enterprise_id=enterprise.id,
        review_item_id=review.id,
        decision="escalate",
        reason_code="amount_outside_tolerance",
        rationale="Requires a separate checker.",
        decided_by=checker.id,
        disposition={"apply_to_current_occurrence_only": True},
    )
    db.add(review_decision)
    db.flush()
    edges = [
        EvidenceEdge(
            enterprise_id=enterprise.id,
            source_type="source_row",
            source_id=left.id,
            target_type="source_file",
            target_id=source_file.id,
            relation="originated_from",
            ordinal=0,
        ),
        EvidenceEdge(
            enterprise_id=enterprise.id,
            source_type="rule_decision",
            source_id=decision.id,
            target_type="source_row",
            target_id=left.id,
            relation="evaluated",
            ordinal=0,
        ),
        EvidenceEdge(
            enterprise_id=enterprise.id,
            source_type="review_decision",
            source_id=review_decision.id,
            target_type="recon_difference",
            target_id=difference.id,
            relation="disposed",
            ordinal=0,
        ),
    ]
    db.add_all(edges)
    db.flush()

    assert discovered.size_bytes == 3_500_000_000
    assert job.progress_current == 1
    assert event.sequence == 1
    assert db.scalar(select(func.count(EvidenceEdge.id))) == 3
    assert review_decision.disposition["apply_to_current_occurrence_only"] is True


def test_all_reconciliation_tables_are_registered_without_shared_model_edits():
    expected = {
        "recon_contracts",
        "recon_contract_versions",
        "recon_contract_source_roles",
        "recon_rule_packages",
        "recon_rule_package_versions",
        "recon_rule_items",
        "recon_rule_compile_artifacts",
        "recon_accounting_periods",
        "recon_source_files",
        "recon_source_rows",
        "recon_rule_decisions",
        "recon_links",
        "recon_differences",
        "recon_review_items",
        "recon_review_decisions",
        "recon_certification_runs",
        "recon_certification_gate_results",
        "recon_certification_versions",
        "recon_certification_heads",
        "recon_adjustment_entries",
        "recon_restatement_versions",
        "recon_evidence_edges",
        "recon_external_agents",
        "recon_agent_enrollment_tokens",
        "recon_source_connectors",
        "recon_agent_jobs",
        "recon_agent_job_events",
        "recon_discovered_files",
    }
    assert expected <= set(Base.metadata.tables)
