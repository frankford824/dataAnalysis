from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import UserAccount

from .models import (
    RULE_TYPES,
    AccountingPeriod,
    AdjustmentEntry,
    CertificationGateResult,
    CertificationHead,
    CertificationRun,
    CertificationVersion,
    ContractSourceRole,
    ReconContractVersion,
    RestatementVersion,
    RuleCompileArtifact,
    RuleItem,
    RulePackageVersion,
)


COMPILER_VERSION = "reconciliation-rules-v1"
MAX_RULE_ITEMS = 1_000
MAX_RULE_CONFIG_BYTES = 16_384
MAX_PATTERN_LENGTH = 256
MAX_TEXT_VALUE_LENGTH = 2_048
MAX_FIELD_LENGTH = 128
MAX_MATCH_WINDOW_SECONDS = 31 * 24 * 60 * 60
MAX_CANDIDATES = 100


class DomainError(ValueError):
    """A deterministic domain invariant was violated."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _same_enterprise(*records: Any) -> str:
    enterprise_ids = {record.enterprise_id for record in records}
    if len(enterprise_ids) != 1:
        raise DomainError("domain records must belong to the same enterprise")
    return enterprise_ids.pop()


def _assert_actor(session: Session, actor_id: str, enterprise_id: str) -> None:
    actor = session.get(UserAccount, actor_id)
    if actor is None or actor.enterprise_id != enterprise_id or actor.status != "active":
        raise DomainError("actor is not an active user in the domain enterprise")


def _required_text(configuration: dict[str, Any], key: str, max_length: int) -> str:
    value = configuration.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{key} must be a non-empty string")
    if len(value) > max_length:
        raise DomainError(f"{key} exceeds {max_length} characters")
    return value


def _validate_contract(version: ReconContractVersion) -> None:
    if len(version.reporting_currency) != 3 or not version.reporting_currency.isalpha():
        raise DomainError("reporting_currency must be a three-letter ISO currency code")
    try:
        ZoneInfo(version.business_timezone)
    except ZoneInfoNotFoundError as exc:
        raise DomainError("business_timezone must be a valid IANA timezone") from exc
    if not 0 <= version.amount_scale <= 4:
        raise DomainError("amount_scale must be between 0 and 4")
    if version.matching_window_seconds < 0 or version.matching_window_seconds > MAX_MATCH_WINDOW_SECONDS:
        raise DomainError("matching window exceeds the 31-day deterministic bound")
    if not 1 <= version.max_candidates_per_row <= MAX_CANDIDATES:
        raise DomainError("max candidates must be between 1 and 100")
    if not version.matching_keys:
        raise DomainError("at least one matching key is required")
    if any(not isinstance(key, str) or not key.strip() or len(key) > MAX_FIELD_LENGTH for key in version.matching_keys):
        raise DomainError("matching keys must be non-empty bounded field names")
    if Decimal(version.amount_tolerance) < 0:
        raise DomainError("amount tolerance cannot be negative")


def publish_contract_version(
    session: Session, contract_version: ReconContractVersion, actor_id: str
) -> str:
    """Validate and publish an immutable reconciliation contract version."""

    if contract_version.status != "draft":
        raise DomainError("only draft contract versions can be published")
    _assert_actor(session, actor_id, contract_version.enterprise_id)
    _assert_actor(session, contract_version.created_by, contract_version.enterprise_id)
    _validate_contract(contract_version)
    roles = list(
        session.scalars(
            select(ContractSourceRole)
            .where(ContractSourceRole.contract_version_id == contract_version.id)
            .order_by(ContractSourceRole.role_code)
        )
    )
    if not roles or not any(role.required for role in roles):
        raise DomainError("at least one required source role is required")
    if any(role.enterprise_id != contract_version.enterprise_id for role in roles):
        raise DomainError("source roles must belong to the contract enterprise")
    payload = {
        "contract_id": contract_version.contract_id,
        "version": contract_version.version,
        "reporting_currency": contract_version.reporting_currency.upper(),
        "business_timezone": contract_version.business_timezone,
        "amount_scale": contract_version.amount_scale,
        "rounding_mode": contract_version.rounding_mode,
        "period_cutoff_policy": contract_version.period_cutoff_policy,
        "refund_direction": contract_version.refund_direction,
        "fee_direction": contract_version.fee_direction,
        "tax_inclusion_policy": contract_version.tax_inclusion_policy,
        "matching_strategy": contract_version.matching_strategy,
        "matching_keys": contract_version.matching_keys,
        "matching_window_seconds": contract_version.matching_window_seconds,
        "max_candidates_per_row": contract_version.max_candidates_per_row,
        "amount_tolerance": str(contract_version.amount_tolerance),
        "effective_from": contract_version.effective_from.isoformat(),
        "effective_to": (
            contract_version.effective_to.isoformat()
            if contract_version.effective_to
            else None
        ),
        "source_roles": [
            {
                "role_code": role.role_code,
                "source_kind": role.source_kind,
                "required": role.required,
                "min_files": role.min_files,
                "business_key_fields": role.business_key_fields,
            }
            for role in roles
        ],
    }
    contract_version.reporting_currency = contract_version.reporting_currency.upper()
    contract_version.checksum = _checksum(payload)
    contract_version.status = "published"
    contract_version.published_by = actor_id
    contract_version.published_at = utcnow()
    session.flush()
    return contract_version.checksum


def _validate_regex(configuration: dict[str, Any]) -> None:
    _required_text(configuration, "field", MAX_FIELD_LENGTH)
    pattern = _required_text(configuration, "pattern", MAX_PATTERN_LENGTH)
    max_input_length = configuration.get("max_input_length")
    if not isinstance(max_input_length, int) or not 1 <= max_input_length <= 4096:
        raise DomainError("bounded_regex max_input_length must be between 1 and 4096")
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise DomainError("lookarounds, special groups, and backreferences are forbidden")
    if re.search(r"([+*?]|\{\d+(?:,\d*)?\})(?:[+*?]|\{\d+(?:,\d*)?\})", pattern):
        raise DomainError("nested or repeated quantifiers are forbidden")
    if re.search(r"\([^)]*[+*?][^)]*\)(?:[+*?]|\{\d+(?:,\d*)?\})", pattern):
        raise DomainError("nested or repeated quantifiers are forbidden")
    for lower, upper in re.findall(r"\{(\d+)(?:,(\d*))?\}", pattern):
        upper_value = int(upper) if upper else int(lower)
        if upper_value > 256:
            raise DomainError("regex repetition is bounded to 256")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise DomainError(f"invalid bounded regular expression: {exc}") from exc


def _validate_rule_item(item: RuleItem) -> dict[str, Any]:
    if item.rule_type not in RULE_TYPES:
        raise DomainError(f"unsupported rule type: {item.rule_type}")
    if not isinstance(item.configuration, dict):
        raise DomainError("rule configuration must be an object")
    if len(_canonical_bytes(item.configuration)) > MAX_RULE_CONFIG_BYTES:
        raise DomainError("rule configuration exceeds 16 KiB")

    config = item.configuration
    if item.rule_type in {"exact", "contains", "prefix", "suffix"}:
        _required_text(config, "field", MAX_FIELD_LENGTH)
        _required_text(config, "value", MAX_TEXT_VALUE_LENGTH)
    elif item.rule_type == "bounded_regex":
        _validate_regex(config)
    elif item.rule_type == "field_mapping":
        _required_text(config, "source_field", MAX_FIELD_LENGTH)
        _required_text(config, "target_field", MAX_FIELD_LENGTH)
    elif item.rule_type == "order_id_extract":
        _required_text(config, "field", MAX_FIELD_LENGTH)
        separators = config.get("separators", [])
        if not isinstance(separators, list) or len(separators) > 16:
            raise DomainError("order_id_extract separators must be a list of at most 16")
        if any(not isinstance(value, str) or len(value) > 8 for value in separators):
            raise DomainError("order_id_extract separators must be short strings")
        max_length = config.get("max_length", 128)
        if not isinstance(max_length, int) or not 1 <= max_length <= 256:
            raise DomainError("order_id_extract max_length must be between 1 and 256")
    elif item.rule_type == "amount_direction":
        _required_text(config, "field", MAX_FIELD_LENGTH)
        if config.get("direction") not in {"positive", "negative"}:
            raise DomainError("amount_direction direction must be positive or negative")
    elif item.rule_type == "bounded_window_link":
        keys = config.get("keys")
        if (
            not isinstance(keys, list)
            or not keys
            or len(keys) > 8
            or any(
                not isinstance(key, str)
                or not key.strip()
                or len(key) > MAX_FIELD_LENGTH
                for key in keys
            )
        ):
            raise DomainError("bounded_window_link requires one to eight bounded keys")
        window = config.get("window_seconds")
        candidates = config.get("max_candidates")
        if not isinstance(window, int) or not 0 <= window <= MAX_MATCH_WINDOW_SECONDS:
            raise DomainError("bounded_window_link window exceeds 31 days")
        if not isinstance(candidates, int) or not 1 <= candidates <= MAX_CANDIDATES:
            raise DomainError("bounded_window_link max_candidates must be 1 to 100")

    return {
        "rule_key": item.rule_key,
        "rule_type": item.rule_type,
        "priority": item.priority,
        "enabled": item.enabled,
        "configuration": item.configuration,
    }


def compile_rule_version(
    session: Session, rule_version: RulePackageVersion
) -> RuleCompileArtifact:
    """Compile only the bounded, typed rule vocabulary into canonical JSON."""

    if rule_version.status not in {"draft", "compiled"}:
        raise DomainError("published or retired rule versions are immutable")
    items = list(
        session.scalars(
            select(RuleItem)
            .where(RuleItem.rule_version_id == rule_version.id)
            .order_by(RuleItem.priority, RuleItem.rule_key)
        )
    )
    if not items:
        raise DomainError("a rule version must contain at least one rule")
    if len(items) > MAX_RULE_ITEMS:
        raise DomainError("a rule version cannot exceed 1000 rules")
    if any(item.enterprise_id != rule_version.enterprise_id for item in items):
        raise DomainError("rules must belong to the package enterprise")
    canonical_rules = [_validate_rule_item(item) for item in items]
    payload = {
        "compiler_version": COMPILER_VERSION,
        "package_id": rule_version.package_id,
        "version": rule_version.version,
        "rules": canonical_rules,
    }
    checksum = _checksum(payload)
    artifact = session.scalar(
        select(RuleCompileArtifact).where(
            RuleCompileArtifact.rule_version_id == rule_version.id,
            RuleCompileArtifact.checksum == checksum,
        )
    )
    if artifact is None:
        artifact = RuleCompileArtifact(
            enterprise_id=rule_version.enterprise_id,
            rule_version_id=rule_version.id,
            checksum=checksum,
            compiler_version=COMPILER_VERSION,
            item_count=len(items),
            canonical_rules=canonical_rules,
        )
        session.add(artifact)
    rule_version.compiled_checksum = checksum
    rule_version.status = "compiled"
    session.flush()
    return artifact


def publish_rule_version(
    session: Session, rule_version: RulePackageVersion, actor_id: str
) -> None:
    if rule_version.status != "compiled" or not rule_version.compiled_checksum:
        raise DomainError("a rule version must compile successfully before publishing")
    _assert_actor(session, actor_id, rule_version.enterprise_id)
    artifact = session.scalar(
        select(RuleCompileArtifact).where(
            RuleCompileArtifact.rule_version_id == rule_version.id,
            RuleCompileArtifact.checksum == rule_version.compiled_checksum,
        )
    )
    if artifact is None:
        raise DomainError("compiled artifact is missing")
    rule_version.status = "published"
    rule_version.published_by = actor_id
    rule_version.published_at = utcnow()
    session.flush()


PERIOD_TRANSITIONS = {"open": "preclosed", "preclosed": "closed"}


def transition_period(
    session: Session, period: AccountingPeriod, target_state: str, actor_id: str
) -> None:
    expected = PERIOD_TRANSITIONS.get(period.state)
    if expected != target_state:
        raise DomainError(
            f"invalid period transition {period.state!r} -> {target_state!r}"
        )
    _assert_actor(session, actor_id, period.enterprise_id)
    now = utcnow()
    period.state = target_state
    period.changed_by = actor_id
    if target_state == "preclosed":
        period.preclosed_at = now
    else:
        period.closed_at = now
    session.flush()


def certification_eligibility(
    session: Session, certification_run: CertificationRun
) -> tuple[bool, list[str]]:
    """Return deterministic certification eligibility and blocking gate codes."""

    period = session.get(AccountingPeriod, certification_run.period_id)
    contract = session.get(ReconContractVersion, certification_run.contract_version_id)
    rules = session.get(RulePackageVersion, certification_run.rule_version_id)
    if period is None or contract is None or rules is None:
        raise DomainError("certification references are incomplete")
    _same_enterprise(certification_run, period, contract, rules)

    blockers: list[str] = []
    if period.state == "closed":
        blockers.append("period_closed")
    if contract.status != "published":
        blockers.append("contract_not_published")
    if rules.status != "published":
        blockers.append("rules_not_published")
    gates = list(
        session.scalars(
            select(CertificationGateResult)
            .where(
                CertificationGateResult.certification_run_id == certification_run.id
            )
            .order_by(CertificationGateResult.gate_code)
        )
    )
    required = [gate for gate in gates if gate.required]
    if not required:
        blockers.append("required_gates_missing")
    blockers.extend(
        gate.gate_code for gate in required if gate.status != "passed"
    )
    eligible = not blockers
    if certification_run.state in {"draft", "submitted", "eligible"}:
        certification_run.state = "eligible" if eligible else "submitted"
        if certification_run.submitted_at is None:
            certification_run.submitted_at = utcnow()
    session.flush()
    return eligible, blockers


def approve_certification(
    session: Session,
    certification_run: CertificationRun,
    approver_id: str,
    payload: dict[str, Any],
) -> CertificationVersion:
    """Approve an eligible run and atomically advance its certification head."""

    eligible, blockers = certification_eligibility(session, certification_run)
    if not eligible:
        raise DomainError(f"certification gates are not satisfied: {', '.join(blockers)}")
    _assert_actor(session, approver_id, certification_run.enterprise_id)
    _assert_actor(
        session, certification_run.proposed_by, certification_run.enterprise_id
    )
    if approver_id == certification_run.proposed_by:
        raise DomainError("maker and checker must be different users")
    period = session.get(AccountingPeriod, certification_run.period_id)
    contract_version = session.get(
        ReconContractVersion, certification_run.contract_version_id
    )
    if period is None or contract_version is None:
        raise DomainError("certification references are incomplete")
    if period.state == "closed":
        raise DomainError("closed periods accept adjustments only")

    head = session.scalar(
        select(CertificationHead).where(
            CertificationHead.enterprise_id == certification_run.enterprise_id,
            CertificationHead.contract_id == contract_version.contract_id,
            CertificationHead.period_id == certification_run.period_id,
            CertificationHead.scope_key == certification_run.scope_key,
        )
    )
    next_version = 1
    if head is not None:
        current = session.get(CertificationVersion, head.current_version_id)
        if current is None:
            raise DomainError("certification head points to a missing version")
        next_version = current.version + 1

    version = CertificationVersion(
        enterprise_id=certification_run.enterprise_id,
        certification_run_id=certification_run.id,
        version=next_version,
        contract_version_id=certification_run.contract_version_id,
        rule_version_id=certification_run.rule_version_id,
        period_id=certification_run.period_id,
        scope_key=certification_run.scope_key,
        payload=payload,
        payload_checksum=_checksum(payload),
        approved_by=approver_id,
    )
    session.add(version)
    session.flush()
    if head is None:
        head = CertificationHead(
            enterprise_id=certification_run.enterprise_id,
            contract_id=contract_version.contract_id,
            period_id=certification_run.period_id,
            scope_key=certification_run.scope_key,
            current_version_id=version.id,
        )
        session.add(head)
    else:
        head.current_version_id = version.id
    certification_run.state = "certified"
    session.flush()
    return version


def submit_adjustment(
    session: Session, adjustment: AdjustmentEntry, submitter_id: str
) -> None:
    period = session.get(AccountingPeriod, adjustment.period_id)
    if period is None:
        raise DomainError("accounting period does not exist")
    _same_enterprise(adjustment, period)
    _assert_actor(session, submitter_id, adjustment.enterprise_id)
    _assert_actor(session, adjustment.created_by, adjustment.enterprise_id)
    if period.state != "closed":
        raise DomainError("adjustments are reserved for closed periods")
    if adjustment.state != "draft":
        raise DomainError("only draft adjustments can be submitted")
    adjustment.state = "submitted"
    adjustment.submitted_by = submitter_id
    session.flush()


def approve_adjustment(
    session: Session, adjustment: AdjustmentEntry, approver_id: str
) -> RestatementVersion:
    period = session.get(AccountingPeriod, adjustment.period_id)
    base = session.get(
        CertificationVersion, adjustment.base_certification_version_id
    )
    if period is None or base is None:
        raise DomainError("adjustment references are incomplete")
    _same_enterprise(adjustment, period, base)
    _assert_actor(session, approver_id, adjustment.enterprise_id)
    if period.state != "closed":
        raise DomainError("only a closed period may produce a restatement")
    if adjustment.state != "submitted" or adjustment.submitted_by is None:
        raise DomainError("adjustment must be submitted before approval")
    if approver_id == adjustment.submitted_by:
        raise DomainError("maker and checker must be different users")
    if base.period_id != adjustment.period_id:
        raise DomainError("adjustment base certification belongs to another period")

    latest = session.scalar(
        select(func.max(RestatementVersion.version)).where(
            RestatementVersion.base_certification_version_id == base.id
        )
    )
    version_number = int(latest or 0) + 1
    payload = {
        "base_certification_version_id": base.id,
        "base_payload_checksum": base.payload_checksum,
        "adjustment_id": adjustment.id,
        "amount": str(adjustment.amount),
        "currency": adjustment.currency,
        "reason_code": adjustment.reason_code,
        "rationale": adjustment.rationale,
        "adjustment_payload": adjustment.payload,
    }
    restatement = RestatementVersion(
        enterprise_id=adjustment.enterprise_id,
        adjustment_entry_id=adjustment.id,
        base_certification_version_id=base.id,
        version=version_number,
        payload=payload,
        payload_checksum=_checksum(payload),
        approved_by=approver_id,
    )
    session.add(restatement)
    adjustment.state = "approved"
    adjustment.approved_by = approver_id
    session.flush()
    return restatement
