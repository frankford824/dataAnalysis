"""Evidence-driven, conservative input adjudication policies.

This module deliberately has no orchestration side effects.  Callers provide an
initialized :class:`DuckDBMemory`; the function below evaluates already captured
snapshots and normalized artifacts, records inspectable decisions, and leaves
ambiguous candidates untouched.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from commerce_harness.evidence_policy import NORMALIZATION_RULE_VERSION
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.rules.wallet import WalletRuleSet

ReconciliationMode = Literal["platform_wallet", "bank_three_way"]

POLICY_ACTOR = "policy:evidence-auto-v1"
INPUT_POLICY_ID = "input-revision-evidence"
INPUT_POLICY_VERSION = "1.0.0"
BUSINESS_POLICY_ACTOR = "policy:conservative-defaults-v1"
BUSINESS_POLICY_VERSION = "1.0.0"
DEFAULT_NORMALIZATION_RULE_VERSION = NORMALIZATION_RULE_VERSION


@dataclass(frozen=True)
class CandidateEvidence:
    revision_id: str
    snapshot_id: str
    artifact_id: str | None
    artifact_sha256: str | None
    source_role: str
    row_count: int | None
    metadata_row_count: int | None
    amount_total: str | None
    formula_pollution_count: int | None
    business_content_sha256: str | None
    control_difference: str | None
    control_exact: bool
    control_row_count_matches: bool | None


@dataclass(frozen=True)
class ControlEvidence:
    status: str
    revision_id: str | None = None
    snapshot_id: str | None = None
    artifact_id: str | None = None
    amount_total: str | None = None
    detail_count: int | None = None


@dataclass(frozen=True)
class AdjudicationSummary:
    groups_evaluated: int
    groups_selected: int
    groups_deferred: int
    selected_revision_ids: tuple[str, ...]
    deferred_subject_keys: tuple[str, ...]
    adjudications_recorded: int
    wallet_rules_registered: int
    business_policies_decided: int
    reconciliation_mode: ReconciliationMode

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready, presentation-safe summary."""

        return asdict(self)


@dataclass(frozen=True)
class _RevisionRow:
    contract_id: str
    period_id: str
    logical_input_key: str
    source_kind: str
    revision_id: str
    snapshot_id: str
    status: str
    approved_by: str | None
    source_uri: str
    artifact_id: str | None
    artifact_sha256: str | None
    parquet_uri: str | None
    metadata_row_count: int | None


@dataclass(frozen=True)
class _PolicySpec:
    subject_kind: str
    policy_id: str
    question: str
    business_impact: str
    decision: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _format_decimal(value: Decimal) -> str:
    return format(value, ".4f")


def _source_role(source_uri: str) -> str:
    normalized = unicodedata.normalize("NFKC", source_uri).casefold()
    processed_markers = (
        "修改后数据",
        "历史加工",
        "加工后",
        "processed",
        "modified-data",
    )
    original_markers = ("原始数据", "\\raw\\", "/raw/", "\\source\\", "/source/")
    archive_markers = ("归档", "archive", "onedrive", "共享", "\\kaoshi\\")
    if any(marker in normalized for marker in processed_markers):
        return "historical_processed"
    if any(marker in normalized for marker in original_markers):
        return "original"
    if any(marker in normalized for marker in archive_markers):
        return "archive"
    return "unknown"


def _normalized_attributes(raw: object) -> tuple[str, bool]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        payload = {"_unparsed": str(raw or "")}
    if not isinstance(payload, dict):
        payload = {"_value": payload}
    formula_value = payload.get("wallet_business_description_formula_ignored")
    formula_polluted = formula_value is True or str(formula_value).casefold() == "true"
    return _canonical_json(payload), formula_polluted


def _read_candidate_artifact(
    database: DuckDBMemory,
    revision: _RevisionRow,
) -> CandidateEvidence:
    if revision.artifact_id is None or revision.parquet_uri is None:
        return CandidateEvidence(
            revision_id=revision.revision_id,
            snapshot_id=revision.snapshot_id,
            artifact_id=None,
            artifact_sha256=None,
            source_role=_source_role(revision.source_uri),
            row_count=None,
            metadata_row_count=revision.metadata_row_count,
            amount_total=None,
            formula_pollution_count=None,
            business_content_sha256=None,
            control_difference=None,
            control_exact=False,
            control_row_count_matches=None,
        )

    cursor = database.execute(
        """
        SELECT
            dataset_kind, source_type, side, business_key,
            settlement_batch_key, cash_bridge_key, occurred_at, period_key,
            amount, metric, sku, attributes_json
        FROM read_parquet(?)
        """,
        [revision.parquet_uri],
    )
    amount_total = Decimal("0.0000")
    row_hashes: list[str] = []
    row_count = 0
    formula_pollution_count = 0
    while batch := cursor.fetchmany(2_000):
        for row in batch:
            attributes, formula_polluted = _normalized_attributes(row[11])
            formula_pollution_count += int(formula_polluted)
            amount = Decimal(str(row[8]))
            amount_total += amount
            business_row = {
                "dataset_kind": row[0],
                "source_type": row[1],
                "side": row[2],
                "business_key": row[3],
                "settlement_batch_key": row[4],
                "cash_bridge_key": row[5],
                "occurred_at": row[6],
                "period_key": row[7],
                "amount": _format_decimal(amount),
                "metric": row[9],
                "sku": row[10],
                "attributes": json.loads(attributes),
            }
            row_hashes.append(
                hashlib.sha256(_canonical_json(business_row).encode("utf-8")).hexdigest()
            )
            row_count += 1

    digest = hashlib.sha256()
    for row_hash in sorted(row_hashes):
        digest.update(row_hash.encode("ascii"))
        digest.update(b"\n")
    return CandidateEvidence(
        revision_id=revision.revision_id,
        snapshot_id=revision.snapshot_id,
        artifact_id=revision.artifact_id,
        artifact_sha256=revision.artifact_sha256,
        source_role=_source_role(revision.source_uri),
        row_count=row_count,
        metadata_row_count=revision.metadata_row_count,
        amount_total=_format_decimal(amount_total),
        formula_pollution_count=formula_pollution_count,
        business_content_sha256=digest.hexdigest(),
        control_difference=None,
        control_exact=False,
        control_row_count_matches=None,
    )


def _control_source_kind(source_kind: str) -> str | None:
    if source_kind == "alipay_ledger":
        return "alipay_control_total"
    if source_kind == "wechat_ledger":
        return "wechat_control_total"
    return None


def _load_control_evidence(
    database: DuckDBMemory,
    *,
    contract_id: str,
    period_id: str,
    source_kind: str,
    normalization_rule_version: str,
) -> ControlEvidence:
    control_kind = _control_source_kind(source_kind)
    if control_kind is None:
        return ControlEvidence(status="not_applicable")
    rows = database.execute(
        """
        WITH latest_artifact AS (
            SELECT
                artifact.*,
                row_number() OVER (
                    PARTITION BY artifact.input_revision_id
                    ORDER BY artifact.created_at DESC, artifact.artifact_id DESC
                ) AS position
            FROM normalized_artifact artifact
            WHERE artifact.rule_version = ?
        )
        SELECT
            revision.revision_id,
            revision.snapshot_id,
            artifact.artifact_id,
            artifact.parquet_uri
        FROM input_revision revision
        LEFT JOIN input_revision_state state
          ON state.revision_id = revision.revision_id
        JOIN latest_artifact artifact
          ON artifact.input_revision_id = revision.revision_id
         AND artifact.position = 1
        WHERE revision.contract_id = ?
          AND revision.period_id = ?
          AND revision.source_kind = ?
          AND coalesce(state.status, revision.status) = 'current'
        ORDER BY revision.revision_id
        """,
        [
            normalization_rule_version,
            contract_id,
            period_id,
            control_kind,
        ],
    ).fetchall()
    if not rows:
        return ControlEvidence(status="missing")
    if len(rows) != 1:
        return ControlEvidence(status="ambiguous")

    revision_id, snapshot_id, artifact_id, parquet_uri = rows[0]
    control_rows = database.execute(
        "SELECT amount, attributes_json FROM read_parquet(?)",
        [str(parquet_uri)],
    ).fetchall()
    if not control_rows:
        return ControlEvidence(status="invalid")
    total = sum((Decimal(str(row[0])) for row in control_rows), Decimal("0.0000"))
    declared_counts: set[int] = set()
    for _, raw_attributes in control_rows:
        attributes, _ = _normalized_attributes(raw_attributes)
        value = json.loads(attributes).get("detail_count")
        if value not in (None, ""):
            try:
                declared_counts.add(int(str(value).replace(",", "")))
            except ValueError:
                return ControlEvidence(status="invalid")
    if len(declared_counts) > 1:
        return ControlEvidence(status="invalid")
    detail_count = next(iter(declared_counts), None)
    return ControlEvidence(
        status="available",
        revision_id=str(revision_id),
        snapshot_id=str(snapshot_id),
        artifact_id=str(artifact_id),
        amount_total=_format_decimal(total),
        detail_count=detail_count,
    )


def _attach_control(
    candidates: list[CandidateEvidence],
    control: ControlEvidence,
) -> list[CandidateEvidence]:
    if control.status != "available" or control.amount_total is None:
        return candidates
    control_total = Decimal(control.amount_total)
    enriched: list[CandidateEvidence] = []
    for candidate in candidates:
        if candidate.amount_total is None or candidate.row_count is None:
            enriched.append(candidate)
            continue
        difference = Decimal(candidate.amount_total) - control_total
        count_matches = (
            None
            if control.detail_count is None
            else candidate.row_count == control.detail_count
        )
        enriched.append(
            CandidateEvidence(
                **{
                    **asdict(candidate),
                    "control_difference": _format_decimal(difference),
                    "control_exact": difference == 0
                    and count_matches is not False,
                    "control_row_count_matches": count_matches,
                }
            )
        )
    return enriched


def _preferred_equivalent_candidate(
    candidates: list[CandidateEvidence],
) -> CandidateEvidence:
    source_rank = {"original": 0, "archive": 1}
    return sorted(
        candidates,
        key=lambda candidate: (
            source_rank.get(candidate.source_role, 99),
            candidate.revision_id,
        ),
    )[0]


def _select_candidate(
    candidates: list[CandidateEvidence],
    control: ControlEvidence,
) -> tuple[CandidateEvidence | None, str]:
    if any(
        candidate.artifact_id is None
        or candidate.business_content_sha256 is None
        or candidate.formula_pollution_count is None
        for candidate in candidates
    ):
        return None, "至少一个候选缺少可复算的标准化产物，保留候选等待补齐证据"

    eligible = [
        candidate
        for candidate in candidates
        if candidate.source_role in {"original", "archive"}
        and candidate.formula_pollution_count == 0
    ]
    if control.status == "available":
        exact = [candidate for candidate in eligible if candidate.control_exact]
        exact_fingerprints = {
            candidate.business_content_sha256 for candidate in exact
        }
        if len(exact_fingerprints) == 1 and exact:
            selected = _preferred_equivalent_candidate(exact)
            return (
                selected,
                "控制总额与明细行数精确一致、无公式污染；"
                "等价业务内容按原始目录优先、归档目录次之",
            )
        if len(exact_fingerprints) > 1:
            return None, "多个不同业务内容均命中同一控制总额，证据不能唯一确定版本"
        return None, "没有原始或归档候选同时满足控制总额、明细行数和无公式污染条件"

    eligible_fingerprints = {
        candidate.business_content_sha256 for candidate in eligible
    }
    if len(eligible) >= 2 and len(eligible_fingerprints) == 1:
        selected = _preferred_equivalent_candidate(eligible)
        return (
            selected,
            "多个独立物理来源的业务内容完全等价；仅选择可追溯代表版本，"
            "不据此认定金额正确",
        )
    return None, "缺少唯一控制证据，且没有至少两个等价的原始或归档来源"


def _revision_rows(
    database: DuckDBMemory,
    normalization_rule_version: str,
) -> list[_RevisionRow]:
    rows = database.execute(
        """
        WITH latest_artifact AS (
            SELECT
                artifact.*,
                row_number() OVER (
                    PARTITION BY artifact.input_revision_id
                    ORDER BY artifact.created_at DESC, artifact.artifact_id DESC
                ) AS position
            FROM normalized_artifact artifact
            WHERE artifact.rule_version = ?
        )
        SELECT
            revision.contract_id,
            revision.period_id,
            revision.logical_input_key,
            revision.source_kind,
            revision.revision_id,
            revision.snapshot_id,
            coalesce(state.status, revision.status) AS effective_status,
            state.approved_by,
            snapshot.source_uri,
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.parquet_uri,
            artifact.row_count
        FROM input_revision revision
        JOIN source_snapshot snapshot
          ON snapshot.snapshot_id = revision.snapshot_id
        LEFT JOIN input_revision_state state
          ON state.revision_id = revision.revision_id
        JOIN latest_artifact artifact
          ON artifact.input_revision_id = revision.revision_id
         AND artifact.position = 1
        WHERE coalesce(state.status, revision.status) <> 'rejected'
        ORDER BY
            revision.contract_id,
            revision.period_id,
            revision.logical_input_key,
            revision.source_kind,
            revision.revision_no,
            revision.revision_id
        """,
        [normalization_rule_version],
    ).fetchall()
    return [
        _RevisionRow(
            contract_id=str(row[0]),
            period_id=str(row[1]),
            logical_input_key=str(row[2]),
            source_kind=str(row[3]),
            revision_id=str(row[4]),
            snapshot_id=str(row[5]),
            status=str(row[6]),
            approved_by=str(row[7]) if row[7] is not None else None,
            source_uri=str(row[8]),
            artifact_id=str(row[9]) if row[9] is not None else None,
            artifact_sha256=str(row[10]) if row[10] is not None else None,
            parquet_uri=str(row[11]) if row[11] is not None else None,
            metadata_row_count=int(row[12]) if row[12] is not None else None,
        )
        for row in rows
    ]


def _record_adjudication(
    database: DuckDBMemory,
    *,
    contract_id: str,
    period_id: str,
    subject_key: str,
    candidates: list[CandidateEvidence],
    control: ControlEvidence,
    selected: CandidateEvidence | None,
    rationale: str,
) -> bool:
    evidence = {
        "policy_id": INPUT_POLICY_ID,
        "policy_version": INPUT_POLICY_VERSION,
        "candidates": [asdict(candidate) for candidate in candidates],
        "control": asdict(control),
    }
    evidence_sha256 = hashlib.sha256(
        _canonical_json(evidence).encode("utf-8")
    ).hexdigest()
    adjudication_id = _stable_id(
        "adjudication",
        INPUT_POLICY_ID,
        INPUT_POLICY_VERSION,
        subject_key,
        evidence_sha256,
    )
    exists = database.execute(
        "SELECT 1 FROM adjudication WHERE adjudication_id = ?",
        [adjudication_id],
    ).fetchone()
    finding = {
        "outcome": "selected" if selected is not None else "deferred",
        "selected_revision_id": (
            selected.revision_id if selected is not None else None
        ),
        "candidate_count": len(candidates),
        "evidence_sha256": evidence_sha256,
    }
    database.execute(
        """
        INSERT INTO adjudication (
            adjudication_id, contract_id, period_id, subject_kind, subject_key,
            finding_json, decision, rationale, evidence_json, decided_by
        )
        VALUES (?, ?, ?, 'input_revision_selection', ?, ?, ?, ?, ?, ?)
        ON CONFLICT (adjudication_id) DO NOTHING
        """,
        [
            adjudication_id,
            contract_id,
            period_id,
            subject_key,
            _canonical_json(finding),
            "accept_engine" if selected is not None else "defer",
            rationale,
            _canonical_json(evidence),
            POLICY_ACTOR,
        ],
    )
    return exists is None


def _apply_revision_selection(
    database: DuckDBMemory,
    *,
    revisions: list[_RevisionRow],
    selected: CandidateEvidence,
    rationale: str,
) -> None:
    selected_reason = f"自动裁决为当前版本：{rationale}"
    superseded_reason = f"由证据裁决版本 {selected.revision_id} 替代"
    for revision in revisions:
        status = "current" if revision.revision_id == selected.revision_id else "superseded"
        reason = selected_reason if status == "current" else superseded_reason
        database.execute(
            """
            INSERT INTO input_revision_state (
                revision_id, status, reason, approved_by, updated_at
            )
            VALUES (?, ?, ?, ?, current_timestamp)
            ON CONFLICT (revision_id) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                approved_by = excluded.approved_by,
                updated_at = now()
            """,
            [revision.revision_id, status, reason, POLICY_ACTOR],
        )


def _register_wallet_rule_metadata(database: DuckDBMemory) -> int:
    ruleset = WalletRuleSet()
    specs = (
        (
            "alipay_classification",
            "deterministic_wallet_classification",
            "支付宝流水确定性分类",
            "仅登记 wallet.py 已执行的有限分类规则；未命中时保持未分类。",
            [rule.rule_id for rule in ruleset.classification_rules],
        ),
        (
            "order_id_extraction",
            "deterministic_order_key_extraction",
            "钱包流水订单号确定性提取",
            "仅登记 wallet.py 已执行的有限订单键规则；歧义或未命中时阻断猜测。",
            [rule.rule_id for rule in ruleset.order_key_rules],
        ),
    )
    changed = 0
    for logical_key, rule_kind, title, description, rule_ids in specs:
        definition_row = database.execute(
            "SELECT rule_id FROM rule_definition WHERE logical_key = ?",
            [logical_key],
        ).fetchone()
        rule_id = (
            str(definition_row[0])
            if definition_row is not None
            else _stable_id("rule", logical_key)
        )
        if definition_row is None:
            database.execute(
                """
                INSERT INTO rule_definition (
                    rule_id, logical_key, rule_kind, title, description
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [rule_id, logical_key, rule_kind, title, description],
            )
        version_row = database.execute(
            """
            SELECT rule_version_id, checksum_sha256, status
            FROM rule_version
            WHERE rule_id = ? AND version = 1
            """,
            [rule_id],
        ).fetchone()
        definition = {
            "executor": "commerce_harness.rules.wallet.WalletRuleSet",
            "logical_key": logical_key,
            "ruleset_id": ruleset.ruleset_id,
            "ruleset_version": ruleset.version,
            "ruleset_checksum_sha256": ruleset.checksum,
            "member_rule_ids": rule_ids,
            "money_behavior": "amount_unchanged",
            "unmatched_behavior": "fail_closed",
        }
        source_evidence = {
            "module": "commerce_harness.rules.wallet",
            "ruleset_id": ruleset.ruleset_id,
            "ruleset_version": ruleset.version,
            "ruleset_checksum_sha256": ruleset.checksum,
        }
        if version_row is not None:
            if str(version_row[1]) != ruleset.checksum:
                raise RuntimeError(
                    f"wallet rule metadata drift for {logical_key}: "
                    "stored checksum does not match wallet.py"
                )
            if str(version_row[2]) != "approved":
                database.execute(
                    """
                    UPDATE rule_version
                    SET status = 'approved',
                        definition_json = ?,
                        source_evidence_json = ?,
                        approved_by = ?,
                        approved_at = current_timestamp
                    WHERE rule_version_id = ?
                    """,
                    [
                        _canonical_json(definition),
                        _canonical_json(source_evidence),
                        POLICY_ACTOR,
                        str(version_row[0]),
                    ],
                )
                changed += 1
            continue
        database.execute(
            """
            INSERT INTO rule_version (
                rule_version_id, rule_id, version, effective_from, status,
                definition_json, checksum_sha256, source_evidence_json,
                approved_by, approved_at
            )
            VALUES (?, ?, 1, ?, 'approved', ?, ?, ?, ?, current_timestamp)
            """,
            [
                _stable_id("rule_version", logical_key, ruleset.version),
                rule_id,
                date(1970, 1, 1),
                _canonical_json(definition),
                ruleset.checksum,
                _canonical_json(source_evidence),
                POLICY_ACTOR,
            ],
        )
        changed += 1
    return changed


def _business_policies(mode: ReconciliationMode) -> tuple[_PolicySpec, ...]:
    fund_decision: dict[str, Any]
    if mode == "platform_wallet":
        fund_decision = {
            "answer": "not_applicable",
            "reconciliation_mode": mode,
            "bank_account_mapping": "not_applicable",
            "reason": "平台钱包模式以支付宝和微信确定性流水为资金证据，不声称银行三方闭环。",
        }
    else:
        fund_decision = {
            "answer": "explicit_effective_dated_mapping_required",
            "reconciliation_mode": mode,
            "bank_account_mapping": "required",
            "required_fields": [
                "account_identity",
                "store_scope",
                "effective_from",
                "effective_to",
            ],
            "missing_mapping_behavior": "block_reconciliation",
        }
    return (
        _PolicySpec(
            subject_kind="freight_period_attribution",
            policy_id="freight-business-date",
            question="跨期到达的运费应计入哪个账期？",
            business_impact="防止按文件夹或到达日期静默改写历史账期。",
            decision={
                "answer": "business_occurrence_date",
                "open_period_behavior": "post_to_original_period",
                "closed_period_behavior": "create_adjustment_linked_to_original_period",
                "forbidden_basis": ["folder_name", "file_name", "arrival_date"],
            },
        ),
        _PolicySpec(
            subject_kind="shared_cost_attribution",
            policy_id="shared-cost-positive-net-sales",
            question="无法直接归属的共享成本如何分配？",
            business_impact="避免无依据平均分摊或在零销售时制造金额。",
            decision={
                "answer": "direct_then_positive_net_sales_share",
                "first_step": "direct_order_or_sku_attribution",
                "residual_allocation_basis": "positive_net_sales_by_store_within_period",
                "zero_denominator_behavior": "block_and_keep_unresolved",
                "negative_or_zero_sales_weight": "excluded",
            },
        ),
        _PolicySpec(
            subject_kind="fund_account_effectivity",
            policy_id="fund-account-effectivity",
            question="资金账户与店铺的生效关系如何处理？",
            business_impact="防止资金账户归属缺失或跨期错配时产生伪三方平衡。",
            decision=fund_decision,
        ),
    )


def _decide_business_policies(
    database: DuckDBMemory,
    mode: ReconciliationMode,
) -> int:
    contracts = [
        str(row[0])
        for row in database.execute(
            """
            SELECT contract_id
            FROM reconciliation_contract
            WHERE status = 'active'
            ORDER BY contract_id
            """
        ).fetchall()
    ]
    events_created = 0
    for contract_id in contracts:
        for policy in _business_policies(mode):
            payload = {
                "policy_id": policy.policy_id,
                "policy_version": BUSINESS_POLICY_VERSION,
                **policy.decision,
            }
            existing_rows = database.execute(
                """
                SELECT decision_id, status, decided_by, decision_json
                FROM business_decision
                WHERE contract_id = ? AND subject_kind = ?
                ORDER BY
                    CASE status WHEN 'decided' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                    created_at,
                    decision_id
                """,
                [contract_id, policy.subject_kind],
            ).fetchall()
            managed_row = next(
                (
                    row
                    for row in existing_rows
                    if str(row[1]) == "pending"
                    or (
                        str(row[1]) == "decided"
                        and str(row[2] or "").startswith("policy:")
                    )
                ),
                None,
            )
            if managed_row is None and any(
                str(row[1]) == "decided" for row in existing_rows
            ):
                continue
            decision_id = (
                str(managed_row[0])
                if managed_row is not None
                else _stable_id("business_decision", contract_id, policy.subject_kind)
            )
            stored_payload = (
                _canonical_json(json.loads(str(managed_row[3])))
                if managed_row is not None and managed_row[3] is not None
                else None
            )
            if managed_row is None:
                database.execute(
                    """
                    INSERT INTO business_decision (
                        decision_id, contract_id, subject_kind, question,
                        business_impact, status, decision_json, decided_by, decided_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'decided', ?, ?, current_timestamp)
                    """,
                    [
                        decision_id,
                        contract_id,
                        policy.subject_kind,
                        policy.question,
                        policy.business_impact,
                        _canonical_json(payload),
                        BUSINESS_POLICY_ACTOR,
                    ],
                )
            elif str(managed_row[1]) == "pending":
                database.execute(
                    """
                    UPDATE business_decision
                    SET question = ?,
                        business_impact = ?,
                        status = 'decided',
                        decision_json = ?,
                        decided_by = ?,
                        decided_at = current_timestamp
                    WHERE decision_id = ?
                    """,
                    [
                        policy.question,
                        policy.business_impact,
                        _canonical_json(payload),
                        BUSINESS_POLICY_ACTOR,
                        decision_id,
                    ],
                )
            elif stored_payload != _canonical_json(payload):
                raise RuntimeError(
                    f"business policy drift for {policy.subject_kind}: "
                    "the immutable decided record differs from the requested policy"
                )
            event_id = _stable_id(
                "business_event",
                decision_id,
                policy.policy_id,
                BUSINESS_POLICY_VERSION,
                _canonical_json(payload),
            )
            exists = database.execute(
                "SELECT 1 FROM business_decision_event WHERE event_id = ?",
                [event_id],
            ).fetchone()
            database.execute(
                """
                INSERT INTO business_decision_event (
                    event_id, decision_id, action, payload_json, actor
                )
                VALUES (?, ?, 'decide', ?, ?)
                ON CONFLICT (event_id) DO NOTHING
                """,
                [
                    event_id,
                    decision_id,
                    _canonical_json(payload),
                    BUSINESS_POLICY_ACTOR,
                ],
            )
            events_created += int(exists is None)
    return events_created


def apply_evidence_driven_adjudication(
    database: DuckDBMemory,
    *,
    reconciliation_mode: ReconciliationMode = "platform_wallet",
    normalization_rule_version: str = DEFAULT_NORMALIZATION_RULE_VERSION,
) -> AdjudicationSummary:
    """Apply conservative evidence policies and persist an inspectable audit trail.

    The function never derives or certifies monetary results.  It only chooses a
    physical input representative when normalized evidence uniquely proves that
    choice, registers metadata for already-executed deterministic wallet rules,
    and records three explicit conservative business policies.
    """

    if reconciliation_mode not in {"platform_wallet", "bank_three_way"}:
        raise ValueError(f"unsupported reconciliation mode: {reconciliation_mode}")

    groups_evaluated = 0
    groups_selected = 0
    groups_deferred = 0
    adjudications_recorded = 0
    selected_revision_ids: list[str] = []
    deferred_subject_keys: list[str] = []

    with database.transaction():
        wallet_rules_registered = _register_wallet_rule_metadata(database)
        grouped: dict[tuple[str, str, str, str], list[_RevisionRow]] = defaultdict(list)
        for revision in _revision_rows(database, normalization_rule_version):
            grouped[
                (
                    revision.contract_id,
                    revision.period_id,
                    revision.logical_input_key,
                    revision.source_kind,
                )
            ].append(revision)

        for group_key, revisions in sorted(grouped.items()):
            if len(revisions) < 2:
                continue
            if not any(revision.status == "candidate" for revision in revisions):
                continue
            if any(revision.approved_by is not None for revision in revisions):
                continue
            contract_id, period_id, logical_input_key, source_kind = group_key
            subject_key = "|".join(group_key)
            groups_evaluated += 1
            candidates = [
                _read_candidate_artifact(database, revision) for revision in revisions
            ]
            control = _load_control_evidence(
                database,
                contract_id=contract_id,
                period_id=period_id,
                source_kind=source_kind,
                normalization_rule_version=normalization_rule_version,
            )
            candidates = _attach_control(candidates, control)
            selected, rationale = _select_candidate(candidates, control)
            adjudications_recorded += int(
                _record_adjudication(
                    database,
                    contract_id=contract_id,
                    period_id=period_id,
                    subject_key=subject_key,
                    candidates=candidates,
                    control=control,
                    selected=selected,
                    rationale=rationale,
                )
            )
            if selected is None:
                groups_deferred += 1
                deferred_subject_keys.append(subject_key)
                continue
            _apply_revision_selection(
                database,
                revisions=revisions,
                selected=selected,
                rationale=rationale,
            )
            groups_selected += 1
            selected_revision_ids.append(selected.revision_id)

        business_policies_decided = _decide_business_policies(
            database,
            reconciliation_mode,
        )

    return AdjudicationSummary(
        groups_evaluated=groups_evaluated,
        groups_selected=groups_selected,
        groups_deferred=groups_deferred,
        selected_revision_ids=tuple(sorted(selected_revision_ids)),
        deferred_subject_keys=tuple(sorted(deferred_subject_keys)),
        adjudications_recorded=adjudications_recorded,
        wallet_rules_registered=wallet_rules_registered,
        business_policies_decided=business_policies_decided,
        reconciliation_mode=reconciliation_mode,
    )
