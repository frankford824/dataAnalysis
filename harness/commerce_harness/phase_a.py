from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from .adjudication_policy import (
    AdjudicationSummary,
    apply_evidence_driven_adjudication,
)
from .code_identity import require_committed_code, resolve_code_identity
from .evidence_policy import NORMALIZATION_RULE_VERSION
from .freeze import _writer_lock
from .kernel.contract import (
    ReconciliationSide,
    platform_wallet_contract,
    taobao_three_way_contract,
)
from .kernel.diff import ComparableCell, DiffFinding, DiffKind, compare_cells
from .kernel.invariants import deterministic_checksum
from .kernel.recon import (
    BalanceScope,
    EvidenceRef,
    ReconciliationResult,
    SettlementCashBridge,
    make_item,
    reconcile_items,
)
from .memory.database import DuckDBMemory
from .normalization import CanonicalRow, IssueSeverity, normalize_bytes
from .snapshot.artifacts import (
    NormalizedArtifactSpec,
    ParquetArtifactStore,
)
from .snapshot.store import SnapshotStore
from .spec.evaluate import evaluate as _evaluate_invariants
from .spec.invariant import InvariantDefinition as _InvariantDefinition
from .spec.invariant import load_invariants_from_json_path as _load_invariants_json
from .spec.rule import RouteDecision as _RouteDecision
from .spec.rule import RuleDefinition as _RuleDefinition
from .spec.rule import decide_route as _decide_route
from .spec.rule import parse_rule as _parse_rule
from .spec.rule import route_rules_only as _route_rules_only
from .trust_tier import TrustTier, decide_trust_tier
from .workbench import WorkbenchPaths


@dataclass(frozen=True, slots=True)
class NormalizeRunResult:
    run_id: str
    snapshots_considered: int
    artifacts_written: int
    rows_written: int
    rejected_rows: int
    candidate_revisions: int
    current_revisions: int


@dataclass(frozen=True, slots=True)
class ReconcileRunResult:
    run_id: str
    period_id: str
    item_count: int
    link_count: int
    balance_count: int
    unresolved_count: int
    checksum_sha256: str
    certifiable: bool
    trust_tier: str = "blocked"


@dataclass(frozen=True, slots=True)
class DiffRunResult:
    run_id: str
    period_id: str
    finding_count: int
    true_difference_count: int
    historical_output_count: int


def _read_snapshot_bytes(
    workbench: WorkbenchPaths,
    *,
    content_sha256: str,
    object_uri: object,
) -> bytes:
    """Read an immutable snapshot even when the workbench mount point moved."""

    recorded_path = Path(str(object_uri))
    if recorded_path.is_file():
        return recorded_path.read_bytes()
    with SnapshotStore(workbench.snapshots).open_object(content_sha256) as reader:
        return reader.read()


@dataclass(frozen=True, slots=True)
class BaselineRunResult:
    run_id: str
    baseline_id: str
    period_id: str
    baseline_version: int
    status: str
    output_sha256: str
    code_sha: str


@dataclass(frozen=True, slots=True)
class _NormalizationCandidate:
    logical_key: str
    dataset_kind: str
    period_key: str
    snapshot_id: str
    snapshot_sha256: str
    business_sha256: str
    source_uri: str
    source_modified_ns: int | None
    purpose: str
    original_name: str
    rows: tuple[CanonicalRow, ...]


_PURPOSE_BY_DATASET = {
    "order": "orders",
    "platform_ledger": "settlement",
    "control_total": "settlement",
    "cost": "product_cost",
    "advertising": "advertising",
    "freight": "shipping",
}
_DATE_TOKEN = re.compile(r"(?<!\d)(?:20)?(2\d)(0[1-9]|1[0-2])(?!\d)")


def _period_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        sorted({f"{match.group(1)}{match.group(2)}" for match in _DATE_TOKEN.finditer(value)})
    )


def _enrich_route_period(
    route: dict[str, Any],
    *,
    original_name: str,
    allowed_periods: set[str],
) -> dict[str, Any]:
    if route.get("period_key"):
        return route
    matched = tuple(
        token for token in _period_tokens(original_name) if token in allowed_periods
    )
    if len(matched) == 1:
        return {**route, "period_key": matched[0]}
    return route


def _inventory_purposes(workbench: WorkbenchPaths) -> dict[str, str]:
    path = workbench.reports / "source-inventory.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    alias = str(payload.get("ssh_alias") or "finance-win-ro")
    return {
        f"{alias}://{record['path']}": str(record.get("purpose") or "")
        for record in payload.get("records", [])
        if isinstance(record, dict) and record.get("path")
    }


def _source_belongs_to_store(source_uri: str, store_name: str) -> bool:
    path = source_uri.split("://", 1)[-1]
    expected = store_name.strip().casefold()
    return expected in {
        part.strip().casefold()
        for part in re.split(r"[\\/]+", path)
        if part.strip()
    }


def _source_group(source_uri: str) -> str:
    parent = source_uri.rsplit("\\", 1)[0].rsplit("/", 1)[0]
    return hashlib.sha256(parent.encode("utf-8")).hexdigest()[:12]


def _file_family(original_name: str) -> str:
    stem = Path(original_name).stem
    without_period = re.sub(r"(?:20)?2\d(?:0[1-9]|1[0-2])", "", stem)
    normalized = re.sub(r"[\W_]+", "-", without_period, flags=re.UNICODE).strip("-")
    return normalized or hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]


def _export_batch(original_name: str) -> str:
    """Return a stable batch discriminator without depending on its directory.

    Platform-fee exports may contain late rows for earlier business periods.
    The export month therefore belongs to the input identity: a 2603 export
    carrying 2602 adjustments must coexist with the original 2602 export
    instead of being presented as an either/or revision.
    """

    period_tokens = _period_tokens(original_name)
    if period_tokens:
        return period_tokens[-1]
    stem = Path(original_name).stem
    normalized = re.sub(r"[\W_]+", "-", stem, flags=re.UNICODE).strip("-")
    return normalized or hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]


def _logical_input_key(
    *,
    row: CanonicalRow,
    source_uri: str,
    original_name: str,
) -> str:
    base = f"{row.dataset_kind}:{row.period_key}"
    if row.dataset_kind == "historical_pnl":
        return f"{base}:group-{_source_group(source_uri)}"
    if row.dataset_kind == "platform_fee":
        return (
            f"{base}:family-{_file_family(original_name)}"
            f":export-{_export_batch(original_name)}"
        )
    return base


def _input_revision_id(logical_key: str, snapshot_id: str) -> str:
    return (
        "revision_"
        + hashlib.sha256(f"{logical_key}|{snapshot_id}".encode()).hexdigest()[:24]
    )


def _canonical_business_sha256(rows: tuple[CanonicalRow, ...]) -> str:
    """Hash normalized business content while excluding source-container evidence.

    A ZIP member and an extracted XLSX can be byte-different snapshots of the
    same ledger. They are equivalent revisions only when every normalized
    business field is identical and in the same deterministic row order.
    """

    encoded_rows: list[bytes] = []
    for row in rows:
        payload = (
            row.dataset_kind,
            row.source_type,
            row.side.value,
            row.business_key,
            row.settlement_batch_id,
            row.cash_bridge_key,
            row.occurred_at.isoformat(),
            format(row.amount, ".4f"),
            row.period_key,
            row.metric,
            row.sku,
            tuple(sorted(row.attributes.items())),
        )
        encoded_rows.append(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    digest = hashlib.sha256()
    for encoded in sorted(encoded_rows):
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_arrow_table(rows: tuple[CanonicalRow, ...]) -> pa.Table:
    schema = pa.schema(
        [
            pa.field("dataset_kind", pa.string(), nullable=False),
            pa.field("source_type", pa.string(), nullable=False),
            pa.field("side", pa.string(), nullable=False),
            pa.field("business_key", pa.string(), nullable=False),
            pa.field("settlement_batch_key", pa.string()),
            pa.field("cash_bridge_key", pa.string()),
            pa.field("occurred_at", pa.string(), nullable=False),
            pa.field("period_key", pa.string(), nullable=False),
            pa.field("amount", pa.decimal128(38, 4), nullable=False),
            pa.field("metric", pa.string()),
            pa.field("sku", pa.string()),
            pa.field("evidence_file_id", pa.string(), nullable=False),
            pa.field("evidence_row_no", pa.int64(), nullable=False),
            pa.field("source_member", pa.string()),
            pa.field("source_sheet", pa.string()),
            pa.field("attributes_json", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_arrays(
        [
            pa.array([row.dataset_kind for row in rows], type=pa.string()),
            pa.array([row.source_type for row in rows], type=pa.string()),
            pa.array([row.side.value for row in rows], type=pa.string()),
            pa.array([row.business_key for row in rows], type=pa.string()),
            pa.array(
                [row.settlement_batch_id for row in rows],
                type=pa.string(),
            ),
            pa.array([row.cash_bridge_key for row in rows], type=pa.string()),
            pa.array(
                [row.occurred_at.isoformat() for row in rows],
                type=pa.string(),
            ),
            pa.array([row.period_key for row in rows], type=pa.string()),
            pa.array(
                [row.amount for row in rows],
                type=pa.decimal128(38, 4),
            ),
            pa.array([row.metric for row in rows], type=pa.string()),
            pa.array([row.sku for row in rows], type=pa.string()),
            pa.array(
                [row.source_snapshot_id or row.source_name for row in rows],
                type=pa.string(),
            ),
            pa.array([row.evidence_row for row in rows], type=pa.int64()),
            pa.array([row.source_member for row in rows], type=pa.string()),
            pa.array([row.source_sheet for row in rows], type=pa.string()),
            pa.array(
                [
                    json.dumps(
                        dict(row.attributes),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for row in rows
                ],
                type=pa.string(),
            ),
        ],
        schema=schema,
    )


def _select_single_source_stream(
    candidates: list[_NormalizationCandidate],
) -> tuple[set[str], dict[str, str]]:
    by_digest: dict[str, list[_NormalizationCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_digest[candidate.business_sha256].append(candidate)
    representatives: list[_NormalizationCandidate] = []
    for digest in sorted(by_digest):
        duplicates = by_digest[digest]
        preferred_purpose = _PURPOSE_BY_DATASET.get(duplicates[0].dataset_kind)
        representatives.append(
            sorted(
                duplicates,
                key=lambda candidate: (
                    candidate.purpose != preferred_purpose,
                    candidate.source_uri,
                ),
            )[0]
        )
    if len(representatives) == 1:
        current = {representatives[0].snapshot_id}
        return current, {
            representatives[0].snapshot_id: "唯一内容版本，自动设为当前输入"
        }
    preferred_purpose = _PURPOSE_BY_DATASET.get(representatives[0].dataset_kind)
    preferred = [
        candidate
        for candidate in representatives
        if preferred_purpose and candidate.purpose == preferred_purpose
    ]
    if len(preferred) == 1:
        current = {preferred[0].snapshot_id}
        reasons = {
            candidate.snapshot_id: (
                f"来源用途 {preferred_purpose} 被配置为当前业务来源"
                if candidate.snapshot_id in current
                else f"保留为历史候选；当前来源用途为 {preferred_purpose}"
            )
            for candidate in representatives
        }
        return current, reasons
    return set(), {
        candidate.snapshot_id: "同一逻辑输入存在多个不同内容版本，需人工选择当前版本"
        for candidate in representatives
    }


def _select_current_candidates(
    candidates: list[_NormalizationCandidate],
) -> tuple[set[str], dict[str, str]]:
    """Select one current revision per actual source stream.

    Alipay and WeChat ledgers are parallel sources, not revisions of each
    other. They may share a broad dataset kind and period, so selection must
    partition by the normalized ``source_type`` before applying revision
    precedence.
    """

    by_source_type: dict[str, list[_NormalizationCandidate]] = defaultdict(list)
    for candidate in candidates:
        source_types = {row.source_type for row in candidate.rows}
        if len(source_types) != 1:
            raise ValueError("one normalization candidate must contain one source type")
        by_source_type[next(iter(source_types))].append(candidate)

    current: set[str] = set()
    reasons: dict[str, str] = {}
    for source_type in sorted(by_source_type):
        selected, stream_reasons = _select_single_source_stream(
            by_source_type[source_type]
        )
        current.update(selected)
        reasons.update(stream_reasons)
    return current, reasons


def _historical_totals(
    rows: tuple[CanonicalRow, ...],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CanonicalRow]] = defaultdict(list)
    for row in rows:
        if row.metric:
            grouped[row.metric].append(row)
    return {
        metric: {
            "value": format(sum((row.amount for row in metric_rows), Decimal("0")), ".4f"),
            "rule_version": "historical-output-v1",
            "evidence": [
                {
                    "file_id": row.source_snapshot_id or row.source_name,
                    "row_no": row.evidence_row,
                    "field": row.metric or "",
                }
                for row in metric_rows
            ],
        }
        for metric, metric_rows in sorted(grouped.items())
    }


def normalize_workspace(
    workbench: WorkbenchPaths,
    *,
    periods: tuple[str, ...] = (),
    store_id: str | None = None,
) -> NormalizeRunResult:
    identity = resolve_code_identity()
    run_id = f"normalize_{uuid.uuid4().hex}"
    allowed_periods = set(periods)
    purpose_by_uri = _inventory_purposes(workbench)
    rejected_rows = 0
    snapshots_considered = 0
    candidates_by_key: dict[str, list[_NormalizationCandidate]] = defaultdict(list)
    issue_counts_by_snapshot: dict[str, Counter[str]] = defaultdict(Counter)
    issue_samples_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)

    with _writer_lock(workbench.locks / "duckdb-writer.lock"):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            contracts = database.execute(
                """
                SELECT contract_id, store_id, definition_json
                FROM reconciliation_contract
                WHERE status = 'active'
                  AND (? IS NULL OR store_id = ?)
                ORDER BY created_at
                LIMIT 2
                """,
                [store_id, store_id],
            ).fetchall()
            if not contracts:
                scope = f"：{store_id}" if store_id else ""
                raise RuntimeError(f"没有可用的店铺核对合同{scope}")
            if len(contracts) > 1:
                raise RuntimeError(
                    "当前存在多个店铺；标准化前必须明确选择店铺，系统不会混合处理"
                )
            selected_contract_id = str(contracts[0][0])
            selected_store_id = str(contracts[0][1])
            contract_definition = json.loads(str(contracts[0][2]))
            selected_store_name = (
                str(contract_definition.get("store_name") or "").strip()
                or None
            )
            profile_run = database.execute(
                """
                SELECT run_id, input_manifest_sha256
                FROM run_log
                WHERE run_kind = 'parse' AND status = 'succeeded'
                  AND coalesce(cast(metrics_json AS VARCHAR), '')
                      NOT LIKE '%"stage": "normalize"%'
                ORDER BY finished_at DESC, started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if profile_run is None:
                raise RuntimeError("尚无成功的有限模板识别运行")
            profile_run_id = str(profile_run[0])
            input_manifest_sha = str(profile_run[1] or "")
            profile_rows = database.execute(
                """
                SELECT p.snapshot_id, p.route_json, s.content_sha256,
                       s.object_uri, s.source_uri, s.source_modified_ns,
                       s.original_name
                FROM source_profile p
                JOIN source_snapshot s ON s.snapshot_id = p.snapshot_id
                WHERE p.status = 'matched'
                QUALIFY row_number() OVER (
                    PARTITION BY p.snapshot_id
                    ORDER BY p.created_at DESC, p.profile_id DESC
                ) = 1
                ORDER BY s.source_uri, p.snapshot_id
                """
            ).fetchall()
            if not profile_rows:
                raise RuntimeError("当前有效画像没有可标准化的匹配快照")
            database.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, run_kind, status, code_sha,
                    input_manifest_sha256, rule_set_sha256, metrics_json
                )
                VALUES (?, ?, 'parse', 'running', ?, ?, ?, ?)
                """,
                [
                    run_id,
                    selected_contract_id,
                    identity.value,
                    input_manifest_sha,
                    _rule_set_sha256(database),
                    json.dumps({"stage": "normalize", "profile_run_id": profile_run_id}),
                ],
            )
            try:
                for (
                    snapshot_id,
                    route_json,
                    snapshot_sha,
                    object_uri,
                    source_uri,
                    source_modified_ns,
                    original_name,
                ) in profile_rows:
                    if selected_store_name and not _source_belongs_to_store(
                        str(source_uri), selected_store_name
                    ):
                        continue
                    snapshots_considered += 1
                    route = _enrich_route_period(
                        json.loads(str(route_json)),
                        original_name=str(original_name),
                        allowed_periods=allowed_periods,
                    )
                    normalized = normalize_bytes(
                        str(original_name),
                        _read_snapshot_bytes(
                            workbench,
                            content_sha256=str(snapshot_sha),
                            object_uri=object_uri,
                        ),
                        route,
                    )
                    rejected_rows += sum(
                        issue.severity == IssueSeverity.REJECTED
                        for issue in normalized.issues
                    )
                    snapshot_key = str(snapshot_id)
                    for issue in normalized.issues:
                        issue_counts_by_snapshot[snapshot_key].update(
                            [
                                "|".join(
                                    (
                                        issue.severity.value,
                                        issue.code,
                                        issue.field or "",
                                    )
                                )
                            ]
                        )
                        if len(issue_samples_by_snapshot[snapshot_key]) >= 20:
                            continue
                        issue_samples_by_snapshot[snapshot_key].append(
                            {
                            "code": issue.code,
                            "message": issue.message,
                            "severity": issue.severity.value,
                            "row": issue.evidence_row,
                            "field": issue.field,
                            }
                        )
                    attached = tuple(
                        dataclass_replace(
                            row,
                            source_snapshot_id=str(snapshot_id),
                        )
                        for row in normalized.rows
                        if not allowed_periods or row.period_key in allowed_periods
                    )
                    grouped_rows: dict[
                        tuple[str, str, str], list[CanonicalRow]
                    ] = defaultdict(list)
                    for row in attached:
                        logical_key = _logical_input_key(
                            row=row,
                            source_uri=str(source_uri),
                            original_name=str(original_name),
                        )
                        grouped_rows[
                            (logical_key, row.dataset_kind, row.period_key)
                        ].append(row)
                    for (
                        logical_key,
                        dataset_kind,
                        period_key,
                    ), rows in grouped_rows.items():
                        candidate_rows = tuple(rows)
                        candidates_by_key[logical_key].append(
                            _NormalizationCandidate(
                                logical_key=logical_key,
                                dataset_kind=dataset_kind,
                                period_key=period_key,
                                snapshot_id=str(snapshot_id),
                                snapshot_sha256=str(snapshot_sha),
                                business_sha256=_canonical_business_sha256(
                                    candidate_rows
                                ),
                                source_uri=str(source_uri),
                                source_modified_ns=(
                                    int(source_modified_ns)
                                    if source_modified_ns is not None
                                    else None
                                ),
                                purpose=purpose_by_uri.get(str(source_uri), ""),
                                original_name=str(original_name),
                                rows=candidate_rows,
                            )
                        )
            except Exception as exc:
                database.execute(
                    """
                    UPDATE run_log
                    SET status = 'failed', finished_at = current_timestamp,
                        error_code = 'normalization_failed', error_detail = ?
                    WHERE run_id = ?
                    """,
                    [str(exc), run_id],
                )
                raise

            artifacts_written = 0
            rows_written = 0
            candidate_revisions = 0
            current_revisions = 0
            artifact_store = ParquetArtifactStore(workbench.normalized)
            for logical_key in sorted(candidates_by_key):
                candidates = candidates_by_key[logical_key]
                current_ids, reasons = _select_current_candidates(candidates)
                first_candidate = candidates[0]
                logical_period_id, logical_contract_id, *_ = _period_row(
                    database,
                    first_candidate.period_key,
                    store_id=selected_store_id,
                )
                source_type_by_revision: dict[str, str] = {}
                for candidate in candidates:
                    source_types = {row.source_type for row in candidate.rows}
                    if len(source_types) != 1:
                        raise ValueError(
                            "one normalization candidate must contain one source type"
                        )
                    revision_id = _input_revision_id(
                        logical_key,
                        candidate.snapshot_id,
                    )
                    source_type = next(iter(source_types))
                    source_type_by_revision[revision_id] = source_type
                    # Early builds stored the broad dataset kind here. This
                    # column identifies the actual independent input stream,
                    # so existing revisions are corrected in place as they
                    # are observed again.
                    database.execute(
                        """
                        UPDATE input_revision
                        SET source_kind = ?
                        WHERE revision_id = ?
                          AND source_kind <> ?
                        """,
                        [source_type, revision_id, source_type],
                    )
                approved_rows = database.execute(
                    """
                    SELECT revision.source_kind, revision.revision_id
                    FROM input_revision revision
                    JOIN input_revision_state state
                      ON state.revision_id = revision.revision_id
                    WHERE revision.contract_id = ?
                      AND revision.period_id = ?
                      AND revision.logical_input_key = ?
                      AND state.status = 'current'
                      AND state.approved_by IS NOT NULL
                    ORDER BY state.updated_at DESC, revision.revision_no DESC
                    """,
                    [logical_contract_id, logical_period_id, logical_key],
                ).fetchall()
                approved_by_source_type: dict[str, str] = {}
                for source_kind, revision_id in approved_rows:
                    source_type = str(source_kind)
                    if source_type in approved_by_source_type:
                        raise RuntimeError(
                            "同一输入流存在多个已人工确认的当前版本"
                        )
                    approved_by_source_type[source_type] = str(revision_id)
                # A rerun may choose a different physical representative for
                # equivalent business content. Reset the mutable overlay for
                # this logical stream first so stale candidate/current states
                # cannot survive merely because their duplicate artifact was
                # not rewritten. Rows bearing approved_by are an explicit
                # human decision and are never reset by normalization.
                database.execute(
                    """
                    UPDATE input_revision_state
                    SET status = 'superseded',
                        reason = '被当前标准业务内容判定替代',
                        updated_at = current_timestamp
                    WHERE revision_id IN (
                        SELECT revision_id
                        FROM input_revision
                        WHERE contract_id = ?
                          AND period_id = ?
                          AND logical_input_key = ?
                    )
                      AND approved_by IS NULL
                    """,
                    [logical_contract_id, logical_period_id, logical_key],
                )
                current_business_digests = {
                    candidate.business_sha256
                    for candidate in candidates
                    if candidate.snapshot_id in current_ids
                }
                current_source_types = {
                    candidate.rows[0].source_type
                    for candidate in candidates
                    if candidate.snapshot_id in current_ids
                }
                reason_by_business_digest = {
                    candidate.business_sha256: reason
                    for candidate in candidates
                    if (reason := reasons.get(candidate.snapshot_id)) is not None
                }
                unique_by_digest: dict[str, _NormalizationCandidate] = {}
                for candidate in sorted(
                    candidates,
                    key=lambda value: (
                        value.snapshot_sha256,
                        value.source_uri,
                    ),
                ):
                    existing = unique_by_digest.get(candidate.snapshot_sha256)
                    if existing is None or candidate.snapshot_id in current_ids:
                        unique_by_digest[candidate.snapshot_sha256] = candidate
                ordered = sorted(
                    unique_by_digest.values(),
                    key=lambda candidate: (
                        candidate.source_modified_ns or 0,
                        candidate.snapshot_sha256,
                    ),
                )
                current_representatives: dict[str, str] = {}
                for business_digest in sorted(current_business_digests):
                    matching = [
                        candidate
                        for candidate in ordered
                        if candidate.business_sha256 == business_digest
                    ]
                    if not matching:
                        continue
                    preferred_purpose = _PURPOSE_BY_DATASET.get(
                        matching[0].dataset_kind
                    )
                    representative = sorted(
                        matching,
                        key=lambda candidate: (
                            candidate.purpose != preferred_purpose,
                            candidate.source_uri,
                            candidate.snapshot_id,
                        ),
                    )[0]
                    current_representatives[business_digest] = (
                        representative.snapshot_id
                    )
                for candidate in ordered:
                    period_id, contract_id, _store_id, _start, _end = _period_row(
                        database,
                        candidate.period_key,
                        store_id=selected_store_id,
                    )
                    revision_id = _input_revision_id(
                        logical_key,
                        candidate.snapshot_id,
                    )
                    source_type = source_type_by_revision[revision_id]
                    existing_revision_row = database.execute(
                        """
                        SELECT revision_no
                        FROM input_revision
                        WHERE revision_id = ?
                        """,
                        [revision_id],
                    ).fetchone()
                    if existing_revision_row is None:
                        revision_no = int(
                            database.fetchone_required(
                                """
                                SELECT coalesce(max(revision_no), 0) + 1
                                FROM input_revision
                                WHERE contract_id = ?
                                  AND period_id = ?
                                  AND logical_input_key = ?
                                """,
                                [contract_id, period_id, logical_key],
                            )[0]
                        )
                    else:
                        revision_no = int(existing_revision_row[0])
                    automatic_status = (
                        "current"
                        if current_representatives.get(
                            candidate.business_sha256
                        )
                        == candidate.snapshot_id
                        else (
                            "superseded"
                            if source_type in current_source_types
                            else "candidate"
                        )
                    )
                    existing_state_row = database.execute(
                        """
                        SELECT status, reason, approved_by
                        FROM input_revision_state
                        WHERE revision_id = ?
                        """,
                        [revision_id],
                    ).fetchone()
                    approved_revision_id = approved_by_source_type.get(source_type)
                    if approved_revision_id == revision_id:
                        status = "current"
                    elif approved_revision_id is not None:
                        status = (
                            str(existing_state_row[0])
                            if existing_state_row is not None
                            and existing_state_row[2] is not None
                            else "candidate"
                        )
                    else:
                        status = automatic_status
                    if status == "current":
                        current_revisions += 1
                    elif status == "candidate":
                        candidate_revisions += 1
                    reason = reasons.get(
                        candidate.snapshot_id,
                        reason_by_business_digest.get(
                            candidate.business_sha256,
                            "相同标准业务内容已去重，原始快照仍保留",
                        ),
                    )
                    if existing_revision_row is None:
                        database.execute(
                            """
                            INSERT INTO input_revision (
                                revision_id, contract_id, period_id, source_kind,
                                logical_input_key, revision_no, snapshot_id, status,
                                reason
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                revision_id,
                                contract_id,
                                period_id,
                                source_type,
                                logical_key,
                                revision_no,
                                candidate.snapshot_id,
                                status,
                                reason,
                            ],
                        )
                    else:
                        database.execute(
                            """
                            UPDATE input_revision
                            SET source_kind = ?
                            WHERE revision_id = ?
                              AND source_kind <> ?
                            """,
                            [source_type, revision_id, source_type],
                        )
                    if existing_state_row is None:
                        database.execute(
                            """
                            INSERT INTO input_revision_state(
                                revision_id, status, reason
                            )
                            VALUES (?, ?, ?)
                            """,
                            [revision_id, status, reason],
                        )
                    elif existing_state_row[2] is not None:
                        # Preserve the complete manual overlay, including its
                        # actor and reason. A normalization rerun is data
                        # processing, not an approval event.
                        pass
                    else:
                        database.execute(
                            """
                            UPDATE input_revision_state
                            SET status = ?, reason = ?,
                                updated_at = current_timestamp
                            WHERE revision_id = ?
                            """,
                            [status, reason, revision_id],
                        )
                    existing_artifact = database.execute(
                        """
                        SELECT artifact_id
                        FROM normalized_artifact
                        WHERE source_snapshot_id = ?
                          AND dataset_kind = ?
                          AND json_extract_string(partition_json, '$.period') = ?
                          AND rule_version = ?
                        ORDER BY created_at
                        LIMIT 1
                        """,
                        [
                            candidate.snapshot_id,
                            candidate.dataset_kind,
                            candidate.period_key,
                            NORMALIZATION_RULE_VERSION,
                        ],
                    ).fetchone()
                    if existing_artifact is None:
                        table = _canonical_arrow_table(candidate.rows)
                        artifact = artifact_store.write_table(
                            table,
                            spec=NormalizedArtifactSpec(
                                dataset_kind=candidate.dataset_kind,
                                schema_version="canonical-v1",
                                source_snapshot_sha256=candidate.snapshot_sha256,
                                rule_version=NORMALIZATION_RULE_VERSION,
                                partition={"period": candidate.period_key},
                            ),
                        )
                        database.register_artifact(
                            artifact,
                            source_snapshot_id=candidate.snapshot_id,
                            normalization_run_id=run_id,
                            input_revision_id=revision_id,
                        )
                        artifacts_written += 1
                        rows_written += len(candidate.rows)
                    if candidate.dataset_kind == "historical_pnl":
                        historical_id = (
                            "history_"
                            + hashlib.sha256(
                                f"{candidate.snapshot_id}|{candidate.period_key}".encode()
                            ).hexdigest()[:24]
                        )
                        database.execute(
                            """
                            INSERT INTO historical_output (
                                historical_output_id, contract_id, period_id,
                                snapshot_id, output_kind, source_label,
                                totals_json, status
                            )
                            VALUES (?, ?, ?, ?, 'pnl_16', ?, ?, 'competing')
                            ON CONFLICT (historical_output_id) DO NOTHING
                            """,
                            [
                                historical_id,
                                contract_id,
                                period_id,
                                candidate.snapshot_id,
                                f"history-{_source_group(candidate.source_uri)}",
                                json.dumps(
                                    _historical_totals(candidate.rows),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            ],
                        )
            metrics = {
                "stage": "normalize",
                "profile_run_id": profile_run_id,
                "snapshots_considered": snapshots_considered,
                "artifacts_written": artifacts_written,
                "rows_written": rows_written,
                "rejected_rows": rejected_rows,
                "candidate_revisions": candidate_revisions,
                "current_revisions": current_revisions,
                "issue_counts_by_snapshot": {
                    snapshot_id: dict(sorted(counts.items()))
                    for snapshot_id, counts in sorted(
                        issue_counts_by_snapshot.items()
                    )
                },
                "issue_samples_by_snapshot": {
                    snapshot_id: samples
                    for snapshot_id, samples in sorted(
                        issue_samples_by_snapshot.items()
                    )
                },
            }
            database.execute(
                """
                UPDATE run_log
                SET status = 'succeeded', finished_at = current_timestamp,
                    metrics_json = ?
                WHERE run_id = ?
                """,
                [
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    run_id,
                ],
            )
    return NormalizeRunResult(
        run_id=run_id,
        snapshots_considered=snapshots_considered,
        artifacts_written=artifacts_written,
        rows_written=rows_written,
        rejected_rows=rejected_rows,
        candidate_revisions=candidate_revisions,
        current_revisions=current_revisions,
    )


def adjudicate_workspace(
    workbench: WorkbenchPaths,
    *,
    mode: Literal["platform_wallet", "bank_three_way"] = "platform_wallet",
) -> AdjudicationSummary:
    """Apply versioned evidence policies without requiring a manual file choice."""

    with (
        _writer_lock(workbench.locks / "duckdb-writer.lock"),
        DuckDBMemory(workbench.database) as database,
    ):
        database.initialize()
        return apply_evidence_driven_adjudication(
            database,
            reconciliation_mode=mode,
            normalization_rule_version=NORMALIZATION_RULE_VERSION,
        )


def _period_row(
    database: DuckDBMemory,
    period_token: str,
    *,
    store_id: str | None = None,
) -> tuple[str, str, str, date, date]:
    token = period_token.strip()
    if len(token) == 4 and token.isdigit():
        year = 2000 + int(token[:2])
        month = int(token[2:])
    elif len(token) == 7 and token[4] == "-":
        year = int(token[:4])
        month = int(token[5:])
    else:
        raise ValueError("账期必须使用 YYMM 或 YYYY-MM")
    if month < 1 or month > 12:
        raise ValueError("账期月份无效")
    rows = database.execute(
        """
        SELECT period_id, contract_id, store_id, period_start, period_end
        FROM accounting_period
        WHERE year(period_start) = ? AND month(period_start) = ?
          AND (? IS NULL OR store_id = ?)
        ORDER BY revision_no DESC, created_at DESC
        LIMIT 2
        """,
        [year, month, store_id, store_id],
    ).fetchall()
    if not rows:
        scope = f"（店铺 {store_id}）" if store_id else ""
        raise RuntimeError(f"账期未初始化：{year:04d}-{month:02d}{scope}")
    if len(rows) > 1:
        raise RuntimeError(
            f"账期 {year:04d}-{month:02d} 对应多个店铺；"
            "必须明确选择店铺后再处理，系统不会猜测归属"
        )
    row = rows[0]
    return (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        row[3],
        row[4],
    )


def _rule_set_sha256(database: DuckDBMemory) -> str:
    rows = database.execute(
        """
        SELECT rule_version_id, checksum_sha256
        FROM rule_version
        WHERE status = 'approved'
        ORDER BY rule_version_id
        """
    ).fetchall()
    encoded = "\n".join(f"{row[0]}:{row[1]}" for row in rows).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_rows(rows: Iterable[tuple[Any, ...]]) -> list[list[Any]]:
    return [[None if value is None else str(value) for value in row] for row in rows]


def _evidence_payload(evidence: Iterable[EvidenceRef]) -> list[dict[str, Any]]:
    return [
        {
            "file_id": entry.file_id,
            "row_no": entry.row_no,
            "field": entry.field,
            "rule_version": entry.rule_version,
            "source_value": entry.source_value,
            "artifact_id": entry.artifact_id,
            "source_member": entry.source_member,
            "source_sheet": entry.source_sheet,
            "rule_version_id": entry.rule_version_id,
        }
        for entry in sorted(set(evidence))
    ]


def _evidence_record(
    *,
    run_id: str,
    logical_id: str,
    evidence: Iterable[EvidenceRef],
    kind: str,
) -> tuple[str, tuple[Any, ...], list[tuple[Any, ...]]]:
    payload = _evidence_payload(evidence)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_id = f"evidence_{run_id}_{logical_id}"
    snapshot_ids = {str(row["file_id"]) for row in payload if row["file_id"]}
    artifact_ids = {str(row["artifact_id"]) for row in payload if row["artifact_id"]}
    formal_rule_ids = {
        str(row["rule_version_id"]) for row in payload if row["rule_version_id"]
    }
    bindings = [
        (
            f"{evidence_id}:{ordinal}",
            evidence_id,
            ordinal,
            str(row["file_id"]),
            str(row["artifact_id"]) or None,
            str(row["source_member"]) or None,
            str(row["source_sheet"]) or None,
            int(row["row_no"]),
            str(row["field"]) or None,
            str(row["source_value"]) or None,
            str(row["rule_version"]) or None,
            str(row["rule_version_id"]) or None,
        )
        for ordinal, row in enumerate(payload)
    ]
    return (
        evidence_id,
        (
            evidence_id,
            run_id,
            next(iter(snapshot_ids)) if len(snapshot_ids) == 1 else None,
            next(iter(artifact_ids)) if len(artifact_ids) == 1 else None,
            next(iter(snapshot_ids)) if len(snapshot_ids) == 1 else "multiple_sources",
            ",".join(f'{row["file_id"]}:{row["row_no"]}' for row in payload),
            next(iter(formal_rule_ids)) if len(formal_rule_ids) == 1 else None,
            kind,
            encoded,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        ),
        bindings,
    )


def _matched_amount(left: Decimal, right: Decimal) -> Decimal:
    magnitude = min(abs(left), abs(right))
    return -magnitude if left < 0 and right < 0 else magnitude


def _bulk_insert(
    connection: Any,
    *,
    target: str,
    columns: tuple[str, ...],
    rows: list[tuple[Any, ...]],
) -> None:
    if not rows:
        return
    identifiers = (target, *columns)
    if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", value) for value in identifiers):
        raise ValueError("bulk insert identifiers must be static SQL names")
    records = [
        dict(zip(columns, row, strict=True))
        for row in rows
    ]
    batch_name = f"_arrow_batch_{uuid.uuid4().hex}"
    connection.register(batch_name, pa.Table.from_pylist(records))
    column_sql = ", ".join(columns)
    try:
        connection.execute(
            f"INSERT INTO {target} ({column_sql}) "
            f"SELECT {column_sql} FROM {batch_name}"
        )
    finally:
        connection.unregister(batch_name)


_ROUTED_SIDE_BY_METRIC = {
    "cost": "cost",
    "freight": "freight",
    "advertising": "advertising",
}


def _routed_db_side(row: dict[str, Any]) -> str:
    """Map a canonical side onto the ``reconciliation_item.side`` vocabulary.

    ``operational`` has no direct counterpart, so the metric decides which
    P&L carrier the routed row belongs to.
    """
    canonical_side = str(row.get("side") or "")
    if canonical_side == "cash":
        return "fund"
    if canonical_side in ("order", "platform"):
        return canonical_side
    metric = str(row.get("metric") or "")
    return _ROUTED_SIDE_BY_METRIC.get(metric, "platform")


@dataclass(frozen=True, slots=True)
class _RoutedItem:
    """A row deliberately kept out of two-sided matching, still evidence-bound."""

    logical_id: str
    source_type: str
    source_record_key: str
    side: str
    business_key: str | None
    settlement_batch_key: str | None
    cash_bridge_key: str | None
    event_date: date
    amount: Decimal
    evidence: tuple[EvidenceRef, ...]
    attributes: dict[str, Any]
    participation: str
    posting_target: str | None
    rule_id: str | None


def _routed_item_from_row(
    row: dict[str, Any],
    *,
    route: _RouteDecision,
    source_type: str,
    occurred_at: datetime,
    evidence: tuple[EvidenceRef, ...],
    attributes: dict[str, Any],
) -> _RoutedItem:
    source_record_key = str(attributes["source_record_key"])
    routed_attributes = dict(attributes)
    routed_attributes["route_participation"] = route.participation
    if route.posting_target:
        routed_attributes["route_posting_target"] = route.posting_target
    if route.rule_id:
        routed_attributes["route_rule_id"] = route.rule_id
    return _RoutedItem(
        logical_id="routed:" + hashlib.sha256(
            source_record_key.encode("utf-8")
        ).hexdigest()[:24],
        source_type=source_type,
        source_record_key=source_record_key,
        side=_routed_db_side(row),
        business_key=str(row.get("business_key") or "") or None,
        settlement_batch_key=(
            str(row["settlement_batch_key"])
            if row.get("settlement_batch_key")
            else None
        ),
        cash_bridge_key=(
            str(row["cash_bridge_key"]) if row.get("cash_bridge_key") else None
        ),
        event_date=occurred_at.date(),
        amount=Decimal(str(row["amount"])),
        evidence=evidence,
        attributes=routed_attributes,
        participation=route.participation,
        posting_target=route.posting_target,
        rule_id=route.rule_id,
    )


def _persist_reconciliation_result(
    database: DuckDBMemory,
    *,
    run_id: str,
    contract_id: str,
    period_id: str,
    result: ReconciliationResult,
    routed_items: Sequence[_RoutedItem] = (),
) -> None:
    balance_keys = [
        (balance.scope.value, balance.business_key) for balance in result.balances
    ]
    if len(balance_keys) != len(set(balance_keys)):
        raise ValueError("reconciliation result contains duplicate scoped balance keys")

    item_ids: dict[str, str] = {}
    item_sides: dict[str, str] = {}
    balance_ids: dict[str, str] = {}
    balance_evidence: dict[str, str] = {}
    with database.transaction() as connection:
        evidence_rows: list[tuple[Any, ...]] = []
        evidence_binding_rows: list[tuple[Any, ...]] = []
        item_rows: list[tuple[Any, ...]] = []
        for item in result.items:
            stored_id = f"{run_id}:{item.item_id}"
            item_ids[item.item_id] = stored_id
            side = (
                "fund"
                if item.side == ReconciliationSide.CASH
                else item.side.value
            )
            item_sides[item.item_id] = side
            evidence_id, evidence_row, binding_rows = _evidence_record(
                run_id=run_id,
                logical_id=item.item_id,
                evidence=item.evidence,
                kind="reconciliation_item",
            )
            evidence_rows.append(evidence_row)
            evidence_binding_rows.extend(binding_rows)
            attributes = dict(item.attributes)
            attributes["kernel_item_id"] = item.item_id
            item_rows.append(
                (
                    stored_id,
                    run_id,
                    contract_id,
                    period_id,
                    item.source_type,
                    str(
                        item.attributes.get("source_record_key")
                        or item.item_id
                    ),
                    side,
                    item.business_key,
                    item.settlement_batch_key,
                    item.cash_bridge_key,
                    item.occurred_at.date(),
                    item.amount,
                    evidence_id,
                    json.dumps(
                        attributes,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "two_sided",
                    None,
                )
            )
        for routed in routed_items:
            evidence_id, evidence_row, binding_rows = _evidence_record(
                run_id=run_id,
                logical_id=routed.logical_id,
                evidence=routed.evidence,
                kind="routed_item",
            )
            evidence_rows.append(evidence_row)
            evidence_binding_rows.extend(binding_rows)
            item_rows.append(
                (
                    f"{run_id}:{routed.logical_id}",
                    run_id,
                    contract_id,
                    period_id,
                    routed.source_type,
                    routed.source_record_key,
                    routed.side,
                    routed.business_key,
                    routed.settlement_batch_key,
                    routed.cash_bridge_key,
                    routed.event_date,
                    routed.amount,
                    evidence_id,
                    json.dumps(
                        routed.attributes,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    routed.participation,
                    routed.posting_target,
                )
            )
        _bulk_insert(
            connection,
            target="evidence_record",
            columns=(
                "evidence_id",
                "run_id",
                "snapshot_id",
                "artifact_id",
                "source_locator",
                "source_row_key",
                "rule_version_id",
                "evidence_kind",
                "payload_json",
                "payload_sha256",
            ),
            rows=evidence_rows,
        )
        _bulk_insert(
            connection,
            target="evidence_binding",
            columns=(
                "binding_id",
                "evidence_id",
                "ordinal",
                "snapshot_id",
                "artifact_id",
                "source_member",
                "source_sheet",
                "row_no",
                "field",
                "source_value",
                "normalization_version",
                "rule_version_id",
            ),
            rows=evidence_binding_rows,
        )
        _bulk_insert(
            connection,
            target="reconciliation_item",
            columns=(
                "item_id",
                "run_id",
                "contract_id",
                "period_id",
                "source_kind",
                "source_record_key",
                "side",
                "business_key",
                "settlement_batch_key",
                "cash_bridge_key",
                "event_date",
                "amount",
                "evidence_id",
                "attributes_json",
                "participation",
                "posting_target",
            ),
            rows=item_rows,
        )

        link_rows: list[tuple[Any, ...]] = []
        link_member_rows: list[tuple[Any, ...]] = []
        for link in result.links:
            stored_id = f"{run_id}:{link.link_id}"
            link_rows.append(
                (
                    stored_id,
                    run_id,
                    contract_id,
                    period_id,
                    link.business_key,
                    "|".join(link.cash_bridge_keys) or None,
                    link.scope.value,
                    link.kind.value,
                    "confirmed",
                    json.dumps(
                        {
                            "kernel_link_id": link.link_id,
                            "rule_version": link.rule_version,
                            "bridge_ids": list(link.bridge_ids),
                            "evidence": _evidence_payload(link.evidence),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
            for logical_item_id in link.item_ids:
                link_member_rows.append(
                    (
                        stored_id,
                        item_ids[logical_item_id],
                        item_sides[logical_item_id],
                    )
                )
        _bulk_insert(
            connection,
            target="reconciliation_link",
            columns=(
                "link_id",
                "run_id",
                "contract_id",
                "period_id",
                "business_key",
                "cash_bridge_key",
                "link_scope",
                "link_kind",
                "status",
                "rationale",
            ),
            rows=link_rows,
        )
        _bulk_insert(
            connection,
            target="reconciliation_link_member",
            columns=("link_id", "item_id", "member_role"),
            rows=link_member_rows,
        )

        balance_evidence_rows: list[tuple[Any, ...]] = []
        balance_evidence_binding_rows: list[tuple[Any, ...]] = []
        balance_rows: list[tuple[Any, ...]] = []
        for balance in result.balances:
            stored_id = f"{run_id}:{balance.balance_id}"
            balance_ids[balance.balance_id] = stored_id
            evidence_id, evidence_row, binding_rows = _evidence_record(
                run_id=run_id,
                logical_id=balance.balance_id,
                evidence=balance.evidence,
                kind="reconciliation_balance",
            )
            balance_evidence_rows.append(evidence_row)
            balance_evidence_binding_rows.extend(binding_rows)
            balance_evidence[balance.balance_id] = evidence_id
            if balance.scope == BalanceScope.ORDER_PLATFORM:
                expected = balance.order_amount
                actual = balance.platform_amount
                difference = balance.order_to_platform_difference
            else:
                expected = balance.platform_amount
                actual = balance.cash_amount
                difference = balance.platform_to_cash_difference
            balance_rows.append(
                (
                    stored_id,
                    run_id,
                    contract_id,
                    period_id,
                    f"{balance.scope.value}:{balance.business_key}",
                    expected,
                    actual,
                    _matched_amount(expected, actual),
                    difference,
                    balance.order_amount,
                    balance.platform_amount,
                    balance.cash_amount,
                    balance.order_to_platform_difference,
                    balance.platform_to_cash_difference,
                    balance.status.value,
                    json.dumps(
                        {
                            "kernel_balance_id": balance.balance_id,
                            "scope": balance.scope.value,
                            "item_ids": list(balance.item_ids),
                            "settlement_batch_keys": list(
                                balance.settlement_batch_keys
                            ),
                            "cash_bridge_keys": list(balance.cash_bridge_keys),
                            "bridge_ids": list(balance.bridge_ids),
                            "evidence": _evidence_payload(balance.evidence),
                            "evidence_id": evidence_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
        _bulk_insert(
            connection,
            target="evidence_record",
            columns=(
                "evidence_id",
                "run_id",
                "snapshot_id",
                "artifact_id",
                "source_locator",
                "source_row_key",
                "rule_version_id",
                "evidence_kind",
                "payload_json",
                "payload_sha256",
            ),
            rows=balance_evidence_rows,
        )
        _bulk_insert(
            connection,
            target="evidence_binding",
            columns=(
                "binding_id",
                "evidence_id",
                "ordinal",
                "snapshot_id",
                "artifact_id",
                "source_member",
                "source_sheet",
                "row_no",
                "field",
                "source_value",
                "normalization_version",
                "rule_version_id",
            ),
            rows=balance_evidence_binding_rows,
        )
        _bulk_insert(
            connection,
            target="reconciliation_balance",
            columns=(
                "balance_id",
                "run_id",
                "contract_id",
                "period_id",
                "balance_key",
                "expected_amount",
                "actual_amount",
                "matched_amount",
                "difference_amount",
                "order_amount",
                "platform_amount",
                "cash_amount",
                "order_to_platform_difference",
                "platform_to_cash_difference",
                "status",
                "evidence_json",
            ),
            rows=balance_rows,
        )

        unresolved_rows: list[tuple[Any, ...]] = []
        for unresolved in result.unresolved:
            unresolved_rows.append(
                (
                    f"{run_id}:{unresolved.unresolved_id}",
                    balance_ids[unresolved.balance_id],
                    unresolved.kind.value,
                    unresolved.absolute_exposure,
                    "open",
                    balance_evidence[unresolved.balance_id],
                    json.dumps(
                        {
                            "scope": unresolved.scope.value,
                            "missing_sides": [
                                side.value for side in unresolved.missing_sides
                            ],
                            "settlement_batch_keys": list(
                                unresolved.settlement_batch_keys
                            ),
                            "cash_bridge_keys": list(
                                unresolved.cash_bridge_keys
                            ),
                            "bridge_ids": list(unresolved.bridge_ids),
                            "rule_versions": list(unresolved.rule_versions),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
        _bulk_insert(
            connection,
            target="unresolved_balance",
            columns=(
                "unresolved_id",
                "balance_id",
                "reason_code",
                "amount",
                "status",
                "evidence_id",
                "explanation",
            ),
            rows=unresolved_rows,
        )


def _current_canonical_rows(
    database: DuckDBMemory,
    *,
    period_id: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    artifacts = database.execute(
        """
        SELECT artifact_id, source_snapshot_id, parquet_uri, content_sha256,
               dataset_kind, rule_version
        FROM (
            SELECT a.artifact_id, a.source_snapshot_id, a.parquet_uri,
                   a.content_sha256, a.dataset_kind, a.rule_version,
                   row_number() OVER (
                       PARTITION BY a.input_revision_id
                       ORDER BY a.created_at DESC, a.artifact_id DESC
                   ) AS artifact_rank
            FROM normalized_artifact a
            JOIN input_revision r ON r.revision_id = a.input_revision_id
            LEFT JOIN input_revision_state state
              ON state.revision_id = r.revision_id
            WHERE r.period_id = ?
              AND coalesce(state.status, r.status) = 'current'
              AND a.rule_version = ?
        ) current_artifact
        WHERE artifact_rank = 1
        ORDER BY dataset_kind, content_sha256
        """,
        [period_id, NORMALIZATION_RULE_VERSION],
    ).fetchall()
    rows: list[dict[str, Any]] = []
    manifest: list[tuple[str, str, str]] = []
    for (
        artifact_id,
        source_snapshot_id,
        parquet_uri,
        content_sha,
        dataset_kind,
        artifact_rule_version,
    ) in artifacts:
        path = Path(str(parquet_uri))
        if not path.is_file():
            raise RuntimeError(f"标准化产物缺失：{path.name}")
        table = pq.read_table(path)
        artifact_rows = table.to_pylist()
        for row in artifact_rows:
            row["_artifact_id"] = str(artifact_id)
            row["_artifact_source_snapshot_id"] = str(source_snapshot_id)
            row["_artifact_rule_version"] = str(artifact_rule_version or "")
        rows.extend(artifact_rows)
        manifest.append((str(dataset_kind), str(content_sha), str(parquet_uri)))
    return _deduplicate_incremental_rows(rows), manifest


def _incremental_row_fingerprint(row: dict[str, Any]) -> str:
    """Identify the same business fact repeated by overlapping exports.

    Platform fee exports are incremental batches: later exports may legitimately
    add late rows for an earlier period, but they can also repeat rows already
    delivered by an earlier batch. Evidence/container fields are deliberately
    excluded so the same fact from two read-only snapshots is counted once.
    """

    attributes_value = row.get("attributes_json") or "{}"
    try:
        attributes = json.loads(str(attributes_value))
    except json.JSONDecodeError:
        attributes = {"unparsed": str(attributes_value)}
    stable_attributes = {
        key: attributes.get(key)
        for key in (
            "category",
            "direction",
            "main_order_id",
            "sub_order_id",
            "merchant_order_id",
            "business_key_kind",
        )
        if attributes.get(key) not in (None, "")
    }
    payload = {
        "dataset_kind": row.get("dataset_kind"),
        "source_type": row.get("source_type"),
        "side": row.get("side"),
        "business_key": row.get("business_key"),
        "settlement_batch_key": row.get("settlement_batch_key"),
        "cash_bridge_key": row.get("cash_bridge_key"),
        "occurred_at": row.get("occurred_at"),
        "period_key": row.get("period_key"),
        "amount": format(Decimal(str(row.get("amount") or "0")), ".4f"),
        "metric": row.get("metric"),
        "sku": row.get("sku"),
        "stable_attributes": stable_attributes,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _deduplicate_incremental_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate only source types whose contract is incremental."""

    result: list[dict[str, Any]] = []
    first_source_by_fingerprint: dict[str, str] = {}
    for row in rows:
        if str(row.get("dataset_kind") or "") != "platform_fee":
            result.append(row)
            continue
        fingerprint = _incremental_row_fingerprint(row)
        source_id = str(row.get("evidence_file_id") or "")
        first_source = first_source_by_fingerprint.get(fingerprint)
        if first_source is not None and first_source != source_id:
            continue
        first_source_by_fingerprint.setdefault(fingerprint, source_id)
        result.append(row)
    return result


def _control_differences(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.0000"))
    for row in rows:
        source_type = str(row.get("source_type") or "")
        value = row.get("amount")
        if value is None:
            continue
        totals[source_type] += Decimal(str(value))
    pairs = {
        "alipay": ("alipay_ledger", "alipay_control_total"),
        "wechat": ("wechat_ledger", "wechat_control_total"),
    }
    differences: dict[str, str] = {}
    for name, (detail_kind, control_kind) in pairs.items():
        if detail_kind not in totals or control_kind not in totals:
            continue
        differences[name] = format(
            totals[detail_kind] - totals[control_kind],
            ".4f",
        )
    return differences


PROFIT_COMPONENTS = (
    "sales",
    "refund",
    "platform_fee",
    "freight",
    "cost",
    "advertising",
)

# P&L components are derived, not raw: ``net_order_amount`` carries both the
# gross sales and the refund leg, so coverage must be judged on the source
# metrics that feed each component rather than on the component names.
_PROFIT_COMPONENT_SOURCE_METRICS: dict[str, tuple[str, ...]] = {
    "sales": ("net_order_amount",),
    "refund": ("net_order_amount",),
    "platform_fee": ("platform_fee",),
    "freight": ("freight",),
    "cost": ("cost",),
    "advertising": ("advertising",),
}


def _missing_profit_components(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Components with no source metric present in the period's rows."""
    present_metrics = {str(row.get("metric") or "") for row in rows}
    return [
        component
        for component in PROFIT_COMPONENTS
        if not any(
            metric in present_metrics
            for metric in _PROFIT_COMPONENT_SOURCE_METRICS[component]
        )
    ]


def _one_sided_amount_basis(rows: Iterable[dict[str, Any]]) -> Decimal:
    """Scale of the largest single side.

    Summing every row would count both sides of the same economic event, which
    halves any ratio computed against it.
    """
    per_side: dict[str, Decimal] = defaultdict(lambda: Decimal("0.0000"))
    for row in rows:
        raw = row.get("amount")
        if raw is None:
            continue
        per_side[str(row.get("side") or "")] += abs(Decimal(str(raw)))
    if not per_side:
        return Decimal("0.0000")
    return max(per_side.values())


def _insert_certified_pnl(
    database: DuckDBMemory,
    *,
    run_id: str,
    period_id: str,
    store_id: str,
    rows: Iterable[dict[str, Any]],
    trust_tier: str,
) -> dict[str, object]:
    materialized_rows = list(rows)
    values: dict[str, list[Decimal]] = defaultdict(list)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    product_values: dict[str, dict[str, list[Decimal]]] = defaultdict(
        lambda: defaultdict(list)
    )
    product_evidence: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    order_skus: dict[str, set[str]] = defaultdict(set)
    for row in materialized_rows:
        if str(row.get("metric") or "") != "net_order_amount":
            continue
        sku = str(row.get("sku") or "").strip()
        business_key = str(row.get("business_key") or "").strip().casefold()
        if sku and business_key:
            order_skus[business_key].add(sku)
    known_order_skus = {
        sku for candidates in order_skus.values() for sku in candidates
    }

    def resolved_sku(
        row: dict[str, Any],
        attributes: dict[str, Any],
        metric: str,
    ) -> str | None:
        explicit = str(row.get("sku") or attributes.get("sku") or "").strip()
        if metric == "net_order_amount":
            return explicit
        keys = {
            str(row.get("business_key") or "").strip().casefold(),
            str(attributes.get("order_id") or "").strip().casefold(),
            str(attributes.get("main_order_id") or "").strip().casefold(),
            str(attributes.get("sub_order_id") or "").strip().casefold(),
            str(attributes.get("merchant_order_id") or "").strip().casefold(),
        }
        matches = {
            sku
            for key in keys
            if key
            for sku in order_skus.get(key, set())
        }
        if len(matches) == 1:
            return next(iter(matches))
        if explicit and explicit in known_order_skus:
            return explicit
        return None

    direct_metric_map = {
        "platform_fee": "platform_fee",
        "freight": "freight",
        "cost": "cost",
        "advertising": "advertising",
    }
    for row in materialized_rows:
        metric = str(row.get("metric") or "")
        attributes = json.loads(str(row.get("attributes_json") or "{}"))
        snapshot_id = str(row.get("evidence_file_id") or "").strip()
        raw_row_no = row.get("evidence_row_no")
        try:
            row_no = int(raw_row_no) if raw_row_no is not None else 0
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "经营结果缺少可定位的原始行，已停止生成正式损益"
            ) from exc
        if not snapshot_id or row_no <= 0:
            raise RuntimeError(
                "经营结果缺少可定位的原始文件或行，已停止生成正式损益"
            )
        row_evidence = {
            "snapshot_id": snapshot_id,
            "row_no": row_no,
            "field": metric,
            "rule_version": NORMALIZATION_RULE_VERSION,
            "source_member": str(row.get("source_member") or ""),
            "source_sheet": str(row.get("source_sheet") or ""),
        }
        product_sku = resolved_sku(row, attributes, metric)
        if metric == "net_order_amount":
            sales = Decimal(str(attributes.get("gross_paid_amount") or "0"))
            refund = -abs(Decimal(str(attributes.get("refund_amount") or "0")))
            values["sales"].append(sales)
            values["refund"].append(refund)
            evidence["sales"].append(row_evidence)
            evidence["refund"].append(row_evidence)
            if product_sku:
                product_values[product_sku]["sales"].append(sales)
                product_values[product_sku]["refund"].append(refund)
                product_evidence[product_sku]["sales"].append(row_evidence)
                product_evidence[product_sku]["refund"].append(row_evidence)
        elif metric in direct_metric_map:
            target = direct_metric_map[metric]
            amount_value = Decimal(str(row["amount"]))
            values[target].append(amount_value)
            evidence[target].append(row_evidence)
            if product_sku:
                product_values[product_sku][target].append(amount_value)
                product_evidence[product_sku][target].append(row_evidence)

    totals = {
        metric: sum(metric_values, Decimal("0.0000"))
        for metric, metric_values in values.items()
    }
    profit_components = PROFIT_COMPONENTS
    missing_profit_components = [
        metric for metric in profit_components if metric not in values
    ]
    if not missing_profit_components:
        totals["profit"] = sum(
            (totals[metric] for metric in profit_components),
            Decimal("0.0000"),
        )
        evidence["profit"] = [
            item
            for metric in profit_components
            for item in evidence.get(metric, [])
        ]
    for metric, value in sorted(totals.items()):
        database.execute(
            """
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json, trust_tier
            )
            VALUES (?, ?, ?, ?, '__store_total__', ?, 'pnl-store-total-v1',
                    ?, ?, ?)
            """,
            [
                f"{run_id}:pnl:{metric}",
                run_id,
                period_id,
                store_id,
                metric,
                value,
                json.dumps(
                    evidence.get(metric, []),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                trust_tier,
            ],
        )
    product_summaries: dict[str, dict[str, Decimal]] = {}
    complete_product_count = 0
    for sku, metric_values in sorted(product_values.items()):
        product_totals = {
            metric: sum(items, Decimal("0.0000"))
            for metric, items in metric_values.items()
        }
        product_missing = [
            metric for metric in profit_components if metric not in metric_values
        ]
        if not product_missing:
            product_totals["profit"] = sum(
                (product_totals[metric] for metric in profit_components),
                Decimal("0.0000"),
            )
            product_evidence[sku]["profit"] = [
                item
                for metric in profit_components
                for item in product_evidence[sku].get(metric, [])
            ]
            complete_product_count += 1
        product_summaries[sku] = product_totals
        sku_digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()[:20]
        for metric, value in sorted(product_totals.items()):
            database.execute(
                """
                INSERT INTO pnl_cell (
                    pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                    definition_id, value, evidence_json, trust_tier
                )
                VALUES (?, ?, ?, ?, ?, ?,
                        'pnl-certified-product-direct-v1', ?, ?, ?)
                """,
                [
                    f"{run_id}:pnl:sku:{sku_digest}:{metric}",
                    run_id,
                    period_id,
                    store_id,
                    sku,
                    metric,
                    value,
                    json.dumps(
                        product_evidence[sku].get(metric, []),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    trust_tier,
                ],
            )
    product_assigned_totals = {
        metric: sum(
            (
                product_metrics.get(metric, Decimal("0.0000"))
                for product_metrics in product_summaries.values()
            ),
            Decimal("0.0000"),
        )
        for metric in profit_components
    }
    unassigned_totals = {
        metric: format(
            totals.get(metric, Decimal("0.0000"))
            - product_assigned_totals.get(metric, Decimal("0.0000")),
            ".4f",
        )
        for metric in profit_components
        if totals.get(metric, Decimal("0.0000"))
        != product_assigned_totals.get(metric, Decimal("0.0000"))
    }
    return {
        "complete": not missing_profit_components,
        "present_components": sorted(values),
        "missing_components": missing_profit_components,
        "profit_definition": "pnl-store-total-v1",
        "product_definition": "pnl-certified-product-direct-v1",
        "product_count": len(product_summaries),
        "complete_product_count": complete_product_count,
        "unassigned_product_totals": unassigned_totals,
        "performance_ready": bool(product_summaries)
        and complete_product_count == len(product_summaries)
        and not unassigned_totals,
    }


def _load_active_invariants(
    database: DuckDBMemory,
) -> list[_InvariantDefinition]:
    rows = database.execute(
        """
        SELECT d.definition_json
        FROM invariant_definition d
        JOIN invariant_version v ON v.invariant_id = d.invariant_id
        WHERE v.status = 'active'
        """
    ).fetchall()
    if rows:
        from .spec.invariant import parse_invariant
        return [parse_invariant(json.loads(str(r[0]))) for r in rows]
    builtin_path = (
        Path(__file__).resolve().parents[1]
        / "packs"
        / "builtin"
        / "ecommerce_settlement"
        / "invariants.json"
    )
    if builtin_path.is_file():
        return _load_invariants_json(builtin_path)
    return []


_DEFAULT_MATERIALITY_FLOOR = Decimal("500.0000")


def _materiality_floor(
    invariant_defs: Sequence[_InvariantDefinition],
) -> Decimal:
    """Smallest single-item materiality across active invariants.

    Taking the minimum keeps the strictest declared threshold in force rather
    than letting a lenient contract raise the bar for everyone.
    """
    thresholds = [
        inv.materiality.single_item
        for inv in invariant_defs
        if inv.materiality.single_item > 0
    ]
    if not thresholds:
        return _DEFAULT_MATERIALITY_FLOOR
    return min(thresholds)


def _load_approved_route_rules(
    database: DuckDBMemory,
) -> list[_RuleDefinition]:
    rows = database.execute(
        """
        SELECT v.definition_json
        FROM rule_version v
        WHERE v.status = 'approved'
        """
    ).fetchall()
    rules: list[_RuleDefinition] = []
    for row in rows:
        raw = json.loads(str(row[0]))
        if raw.get("action") == "route":
            rules.append(_parse_rule(raw))
    return rules


@dataclass(frozen=True, slots=True)
class ReconciliationInputs:
    """Kernel inputs derived from canonical rows under a given rule set."""

    items: list[Any]
    routed_items: list[_RoutedItem]
    bridges: dict[tuple[str, str], SettlementCashBridge]


def build_reconciliation_inputs(
    rows: Sequence[dict[str, Any]],
    contract: Any,
    route_rules: Sequence[_RuleDefinition],
) -> ReconciliationInputs:
    """Turn canonical rows into kernel items, honouring route decisions.

    Shared by the production run and by counterfactual experiments so that a
    shadow run exercises the same engine rather than an estimate of it.
    """
    items: list[Any] = []
    routed_items: list[_RoutedItem] = []
    bridges: dict[tuple[str, str], SettlementCashBridge] = {}
    contract_source_types = {source.source_type for source in contract.sources}

    for row in rows:
        source_type = str(row.get("source_type") or "")
        if source_type not in contract_source_types:
            continue
        occurred_at = datetime.fromisoformat(str(row["occurred_at"]))
        file_id = str(row.get("evidence_file_id") or "")
        raw_row_no = row.get("evidence_row_no")
        if raw_row_no is None:
            raise RuntimeError("标准化记录缺少可定位的原始行，已停止本范围核对")
        try:
            row_no = int(raw_row_no)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "标准化记录缺少可定位的原始行，已停止本范围核对"
            ) from exc
        if not file_id or row_no <= 0:
            raise RuntimeError(
                "标准化记录缺少可定位的原始文件或行，已停止本范围核对"
            )
        evidence = (
            EvidenceRef(
                file_id=str(row.get("_artifact_source_snapshot_id") or file_id),
                row_no=row_no,
                field=str(row.get("metric") or "amount"),
                rule_version=str(
                    row.get("_artifact_rule_version") or NORMALIZATION_RULE_VERSION
                ),
                source_value=format(Decimal(str(row["amount"])), ".4f"),
                artifact_id=str(row.get("_artifact_id") or ""),
                source_member=str(row.get("source_member") or ""),
                source_sheet=str(row.get("source_sheet") or ""),
            ),
        )
        attributes = json.loads(str(row.get("attributes_json") or "{}"))
        attributes["source_record_key"] = f"{file_id}:{row_no}"
        route = _decide_route(row, route_rules)
        if not route.enters_reconciliation:
            routed_items.append(
                _routed_item_from_row(
                    row,
                    route=route,
                    source_type=source_type,
                    occurred_at=occurred_at,
                    evidence=evidence,
                    attributes=attributes,
                )
            )
            continue
        item = make_item(
            contract=contract,
            source_type=source_type,
            business_key=str(row["business_key"]),
            value=Decimal(str(row["amount"])),
            occurred_at=occurred_at,
            evidence=evidence,
            settlement_batch_key=(
                str(row["settlement_batch_key"])
                if row.get("settlement_batch_key")
                else None
            ),
            cash_bridge_key=(
                str(row["cash_bridge_key"]) if row.get("cash_bridge_key") else None
            ),
            attributes=attributes,
        )
        items.append(item)
        settlement_key = item.settlement_batch_key
        cash_key = item.cash_bridge_key
        if settlement_key and cash_key:
            bridge_key = (settlement_key, cash_key)
            bridges.setdefault(
                bridge_key,
                SettlementCashBridge(
                    bridge_id=(
                        "bridge_"
                        + hashlib.sha256(
                            f"{settlement_key}|{cash_key}".encode()
                        ).hexdigest()[:24]
                    ),
                    settlement_batch_key=settlement_key,
                    cash_bridge_key=cash_key,
                    rule_version="finite-cash-bridge-v1",
                    evidence=evidence,
                ),
            )
    return ReconciliationInputs(
        items=items, routed_items=routed_items, bridges=bridges,
    )


def reconcile_period(
    workbench: WorkbenchPaths,
    *,
    period_token: str,
    store_id: str | None = None,
    mode: Literal["platform_wallet", "bank_three_way"] = "platform_wallet",
) -> ReconcileRunResult:
    identity = resolve_code_identity()
    run_id = f"reconcile_{uuid.uuid4().hex}"
    with _writer_lock(workbench.locks / "duckdb-writer.lock"):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            period_id, contract_id, store_id, _start, _end = _period_row(
                database,
                period_token,
                store_id=store_id,
            )
            platform_code = str(
                database.fetchone_required(
                    """
                    SELECT platform_code
                    FROM reconciliation_contract
                    WHERE contract_id = ?
                    """,
                    [contract_id],
                )[0]
            )
            if mode == "platform_wallet":
                contract = platform_wallet_contract(platform_code)
            elif platform_code == "taobao":
                contract = taobao_three_way_contract()
            else:
                raise RuntimeError(
                    f"{platform_code} 尚未启用银行三方桥接合同；"
                    "请使用平台钱包核对"
                )
            rows, artifact_manifest = _current_canonical_rows(
                database,
                period_id=period_id,
            )
            if not artifact_manifest:
                raise RuntimeError("该账期没有已确认的标准化输入版本")

            invariant_defs = _load_active_invariants(database)
            evaluations = _evaluate_invariants(rows, invariant_defs) if invariant_defs else ()
            inv_blocks: dict[str, bool] = {}
            for inv in invariant_defs:
                inv_blocks[inv.invariant_id] = inv.blocks_certification

            inv_version_map: dict[str, str] = {}
            if invariant_defs:
                iv_rows = database.execute(
                    """
                    SELECT invariant_id, invariant_version_id
                    FROM invariant_version
                    WHERE status = 'active'
                    """
                ).fetchall()
                for iv_row in iv_rows:
                    inv_version_map[str(iv_row[0])] = str(iv_row[1])

            route_rules = _route_rules_only(_load_approved_route_rules(database))

            input_sha = deterministic_checksum(artifact_manifest)
            rule_sha = _rule_set_sha256(database)
            database.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, period_id, run_kind, status, code_sha,
                    input_manifest_sha256, rule_set_sha256
                )
                VALUES (?, ?, ?, 'reconcile', 'running', ?, ?, ?)
                """,
                [
                    run_id,
                    contract_id,
                    period_id,
                    identity.value,
                    input_sha,
                    rule_sha,
                ],
            )
            try:
                inputs = build_reconciliation_inputs(rows, contract, route_rules)
                items = inputs.items
                routed_items = inputs.routed_items
                bridges = inputs.bridges
                result = reconcile_items(
                    items,
                    contract,
                    link_rule_version="taobao-order-platform-key-v1",
                    cash_bridges=bridges.values(),
                )
                _persist_reconciliation_result(
                    database,
                    run_id=run_id,
                    contract_id=contract_id,
                    period_id=period_id,
                    result=result,
                    routed_items=routed_items,
                )

                pending_decisions = int(
                    database.fetchone_required(
                        """
                        SELECT count(*) FROM business_decision
                        WHERE contract_id = ? AND status = 'pending'
                        """,
                        [contract_id],
                    )[0]
                )
                checklist_requirements = int(
                    database.fetchone_required(
                        """
                        SELECT count(*)
                        FROM checklist_requirement requirement
                        JOIN accounting_period period
                          ON period.period_id = ?
                        WHERE requirement.contract_id = ?
                          AND requirement.required = true
                          AND requirement.effective_from <= period.period_end
                          AND (
                              requirement.effective_to IS NULL
                              OR requirement.effective_to >= period.period_start
                          )
                        """,
                        [period_id, contract_id],
                    )[0]
                )
                incomplete_checklist = int(
                    database.fetchone_required(
                        """
                        WITH latest_results AS (
                            SELECT requirement_id, status,
                                   row_number() OVER (
                                       PARTITION BY requirement_id
                                       ORDER BY checked_at DESC
                                   ) AS position
                            FROM checklist_result
                            WHERE period_id = ?
                        )
                        SELECT count(*)
                        FROM checklist_requirement requirement
                        JOIN accounting_period period
                          ON period.period_id = ?
                        LEFT JOIN latest_results result
                          ON result.requirement_id = requirement.requirement_id
                         AND result.position = 1
                        WHERE requirement.contract_id = ?
                          AND requirement.required = true
                          AND requirement.effective_from <= period.period_end
                          AND (
                              requirement.effective_to IS NULL
                              OR requirement.effective_to >= period.period_start
                          )
                          AND coalesce(result.status, 'missing')
                              NOT IN ('present', 'not_applicable')
                        """,
                        [period_id, period_id, contract_id],
                    )[0]
                )
                if checklist_requirements == 0:
                    incomplete_checklist = 1
                candidate_revisions = int(
                    database.fetchone_required(
                        """
                        SELECT count(*)
                        FROM input_revision revision
                        LEFT JOIN input_revision_state state
                          ON state.revision_id = revision.revision_id
                        WHERE revision.period_id = ?
                          AND coalesce(state.status, revision.status) = 'candidate'
                          AND EXISTS (
                              SELECT 1
                              FROM normalized_artifact artifact
                              WHERE artifact.input_revision_id = revision.revision_id
                                AND artifact.rule_version = ?
                          )
                        """,
                        [period_id, NORMALIZATION_RULE_VERSION],
                    )[0]
                )
                approved_rules = {
                    str(row[0])
                    for row in database.execute(
                        """
                        SELECT d.logical_key
                        FROM rule_definition d
                        JOIN rule_version v ON v.rule_id = d.rule_id
                        WHERE v.status = 'approved'
                        """
                    ).fetchall()
                }
                required_rules = {
                    "alipay_classification",
                    "order_id_extraction",
                }
                if contract.requires_cash_bridge:
                    required_rules.add("cash_bridge")
                missing_rules = sorted(required_rules - approved_rules)
                present_sides = {item.side for item in result.items}
                missing_sides = sorted(
                    side.value
                    for side in contract.required_sides
                    if side not in present_sides
                )
                control_differences = _control_differences(rows)
                failed_controls = {
                    name: value
                    for name, value in control_differences.items()
                    if abs(Decimal(value)) > Decimal("0.0100")
                }

                for ev in evaluations:
                    version_id = inv_version_map.get(ev.invariant_id)
                    if not version_id:
                        continue
                    database.execute(
                        """
                        INSERT INTO invariant_evaluation (
                            evaluation_id, run_id, invariant_version_id,
                            period_id, store_id, status,
                            left_total, right_total, gap_amount,
                            participating_rows, is_material, evidence_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"{run_id}:{ev.evaluation_id}",
                            run_id,
                            version_id,
                            period_id,
                            store_id,
                            ev.status,
                            ev.left_total,
                            ev.right_total,
                            ev.gap_amount,
                            ev.participating_rows,
                            ev.is_material,
                            ev.evidence_json,
                        ],
                    )

                blocking_violations = sum(
                    1
                    for e in evaluations
                    if e.status == "violated"
                    and inv_blocks.get(e.invariant_id, False)
                )
                materiality_floor = _materiality_floor(invariant_defs)
                material_unresolved = sum(
                    1
                    for u in result.unresolved
                    if u.absolute_exposure >= materiality_floor
                )
                amount_basis = _one_sided_amount_basis(rows)
                unexplained_amount = sum(
                    (u.absolute_exposure for u in result.unresolved),
                    Decimal("0.0000"),
                )
                unexplained_ratio = (
                    unexplained_amount / amount_basis
                    if amount_basis > 0
                    else Decimal("0")
                )
                missing_components = _missing_profit_components(rows)

                # Structural defects stay all-or-nothing: without approved
                # rules, complete files, a settled revision and balanced
                # control totals there is nothing to grade.
                structural_blocks = any(
                    (
                        pending_decisions,
                        incomplete_checklist,
                        candidate_revisions,
                        missing_rules,
                        missing_sides,
                        failed_controls,
                    )
                )
                if structural_blocks:
                    tier = TrustTier.BLOCKED
                else:
                    tier = decide_trust_tier(
                        blocking_violations=blocking_violations,
                        material_unresolved=material_unresolved,
                        unexplained_ratio=unexplained_ratio,
                        incomplete_components=len(missing_components),
                    )
                certifiable = tier == TrustTier.CERTIFIED
                profit_completeness: dict[str, object] = {
                    "complete": False,
                    "present_components": [],
                    "missing_components": list(PROFIT_COMPONENTS),
                    "profit_definition": "pnl-store-total-v1",
                }
                if tier != TrustTier.BLOCKED:
                    profit_completeness = _insert_certified_pnl(
                        database,
                        run_id=run_id,
                        period_id=period_id,
                        store_id=store_id,
                        rows=rows,
                        trust_tier=tier.value,
                    )
                metrics = {
                    "item_count": len(result.items),
                    "link_count": len(result.links),
                    "balance_count": len(result.balances),
                    "unresolved_count": len(result.unresolved),
                    "cash_bridge_outcome_count": len(
                        result.cash_bridge_outcomes
                    ),
                    "reconciliation_mode": contract.mode.value,
                    "bank_cash_status": (
                        "required"
                        if contract.requires_cash_bridge
                        else "not_applicable"
                    ),
                    "checksum_sha256": result.checksum(),
                    "certifiable": certifiable,
                    "trust_tier": tier.value,
                    "pending_business_decisions": pending_decisions,
                    "incomplete_checklist": incomplete_checklist,
                    "configured_checklist_requirements": checklist_requirements,
                    "candidate_revisions": candidate_revisions,
                    "missing_required_rules": missing_rules,
                    "missing_sides": missing_sides,
                    "control_differences": control_differences,
                    "failed_controls": failed_controls,
                    "profit_completeness": profit_completeness,
                    "blocking_violations": blocking_violations,
                    "evaluation_count": len(evaluations),
                    "material_unresolved_count": material_unresolved,
                    "materiality_floor": format(materiality_floor, ".4f"),
                    "unexplained_amount": format(unexplained_amount, ".4f"),
                    "unexplained_ratio": format(unexplained_ratio, ".6f"),
                    "amount_basis": format(amount_basis, ".4f"),
                    "missing_profit_components": missing_components,
                    "routed_row_count": len(routed_items),
                    "routed_amount_abs": format(
                        sum(
                            (abs(r.amount) for r in routed_items),
                            Decimal("0.0000"),
                        ),
                        ".4f",
                    ),
                    "routed_by_target": {
                        target: count
                        for target, count in sorted(
                            Counter(
                                str(r.posting_target or "unspecified")
                                for r in routed_items
                            ).items()
                        )
                    },
                }
                supports_skipped = database.run_log_supports_skipped()
                if len(result.items) == 0:
                    final_status = "skipped" if supports_skipped else "cancelled"
                    empty_error_code = None if supports_skipped else "skipped_empty"
                else:
                    final_status = "succeeded"
                    empty_error_code = None
                database.execute(
                    """
                    UPDATE run_log
                    SET status = ?, finished_at = current_timestamp,
                        metrics_json = ?, error_code = ?
                    WHERE run_id = ?
                    """,
                    [
                        final_status,
                        json.dumps(
                            metrics,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        empty_error_code,
                        run_id,
                    ],
                )
            except Exception as exc:
                database.execute(
                    """
                    UPDATE run_log
                    SET status = 'failed', finished_at = current_timestamp,
                        error_code = 'reconcile_failed', error_detail = ?
                    WHERE run_id = ?
                    """,
                    [str(exc), run_id],
                )
                raise
    return ReconcileRunResult(
        run_id=run_id,
        period_id=period_id,
        item_count=len(result.items),
        link_count=len(result.links),
        balance_count=len(result.balances),
        unresolved_count=len(result.unresolved),
        checksum_sha256=result.checksum(),
        certifiable=certifiable,
        trust_tier=tier.value,
    )


def _baseline_output(
    database: DuckDBMemory,
    *,
    run_id: str,
) -> dict[str, list[list[Any]]]:
    balances = database.execute(
        """
        SELECT balance_id, balance_key, expected_amount, actual_amount,
               matched_amount, difference_amount, status,
               order_amount, platform_amount, cash_amount,
               order_to_platform_difference, platform_to_cash_difference
        FROM reconciliation_balance
        WHERE run_id = ?
        ORDER BY balance_id
        """,
        [run_id],
    ).fetchall()
    unresolved = database.execute(
        """
        SELECT u.unresolved_id, u.balance_id, u.reason_code, u.amount, u.status
        FROM unresolved_balance u
        JOIN reconciliation_balance b ON b.balance_id = u.balance_id
        WHERE b.run_id = ?
        ORDER BY u.unresolved_id
        """,
        [run_id],
    ).fetchall()
    pnl = database.execute(
        """
        SELECT pnl_cell_id, store_id, sku_key, metric, definition_id, value,
               coalesce(trust_tier, 'certified') AS trust_tier
        FROM pnl_cell
        WHERE run_id = ?
        ORDER BY pnl_cell_id
        """,
        [run_id],
    ).fetchall()
    return {
        "balances": _json_rows(balances),
        "unresolved": _json_rows(unresolved),
        "pnl": _json_rows(pnl),
    }


def _latest_reconcile_run(
    database: DuckDBMemory,
    *,
    period_id: str,
) -> tuple[str, str, str, str]:
    row = database.execute(
        """
        SELECT run_id, input_manifest_sha256, rule_set_sha256, metrics_json
        FROM run_log
        WHERE period_id = ? AND run_kind = 'reconcile' AND status = 'succeeded'
        ORDER BY finished_at DESC, started_at DESC
        LIMIT 1
        """,
        [period_id],
    ).fetchone()
    if row is None:
        raise RuntimeError("该账期还没有成功的确定性对账运行")
    return (
        str(row[0]),
        str(row[1] or ""),
        str(row[2] or ""),
        str(row[3] or "{}"),
    )


def _evidence_refs(payload: Any, *, fallback_file_id: str) -> tuple[EvidenceRef, ...]:
    rows = payload if isinstance(payload, list) else []
    result: list[EvidenceRef] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            file_id = str(row.get("file_id") or "").strip()
            raw_row_no = row.get("row_no")
            if raw_row_no is None:
                continue
            row_no = int(raw_row_no)
            if not file_id or row_no <= 0:
                continue
            result.append(
                EvidenceRef(
                    file_id=file_id,
                    row_no=row_no,
                    field=str(row.get("field") or ""),
                    rule_version=str(row.get("rule_version") or ""),
                    source_value=str(row.get("source_value") or ""),
                    artifact_id=str(row.get("artifact_id") or ""),
                    source_member=str(row.get("source_member") or ""),
                    source_sheet=str(row.get("source_sheet") or ""),
                    rule_version_id=str(row.get("rule_version_id") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    if result:
        return tuple(sorted(set(result)))
    return ()


def _run_trust_tier(database: DuckDBMemory, *, run_id: str) -> str:
    """Trust tier recorded on a run, preferring the persisted P&L annotation."""
    row = database.execute(
        """
        SELECT DISTINCT coalesce(trust_tier, 'certified')
        FROM pnl_cell
        WHERE run_id = ?
        ORDER BY 1
        """,
        [run_id],
    ).fetchall()
    if not row:
        return "unknown"
    # Distinct tiers on one run means the annotation is untrustworthy; report
    # the weakest rather than picking a favourable one.
    tiers = [str(item[0]) for item in row]
    for tier in ("blocked", "partial", "certified"):
        if tier in tiers:
            return tier
    return tiers[0]


def compare_period(
    workbench: WorkbenchPaths,
    *,
    period_token: str,
    store_id: str | None = None,
) -> DiffRunResult:
    identity = resolve_code_identity()
    run_id = f"diff_{uuid.uuid4().hex}"
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        period_id, contract_id, _store_id, _start, _end = _period_row(
            database,
            period_token,
            store_id=store_id,
        )
        reconcile_run_id, input_sha, rule_sha, _metrics = _latest_reconcile_run(
            database,
            period_id=period_id,
        )
        historical_rows = database.execute(
            """
            SELECT historical_output_id, snapshot_id, source_label, totals_json
            FROM historical_output
            WHERE period_id = ? AND status IN ('candidate', 'competing', 'adjudicated')
            ORDER BY historical_output_id
            """,
            [period_id],
        ).fetchall()
        if not historical_rows:
            raise RuntimeError("该账期尚未登记任何历史输出，不能执行对表")

        current_rows = database.execute(
            """
            SELECT metric, sum(value), list(evidence_json)
            FROM pnl_cell
            WHERE run_id = ?
            GROUP BY metric
            ORDER BY metric
            """,
            [reconcile_run_id],
        ).fetchall()
        # Comparing against history is allowed at any tier, but the tier of the
        # numbers being compared has to travel with the result.
        compared_trust_tier = _run_trust_tier(database, run_id=reconcile_run_id)
        current_by_metric = {
            str(metric): (value, evidence_json)
            for metric, value, evidence_json in current_rows
        }
        all_findings: list[DiffFinding] = []
        history_count = 0
        for _historical_id, snapshot_id, source_label, totals_json in historical_rows:
            history_count += 1
            totals = json.loads(str(totals_json))
            current_cells: list[ComparableCell] = []
            historical_cells: list[ComparableCell] = []
            for metric, raw in sorted(totals.items()):
                if isinstance(raw, dict):
                    historical_value = raw.get("value")
                    historical_evidence = raw.get("evidence")
                    historical_rule = str(
                        raw.get("rule_version") or "historical-output-v1"
                    )
                else:
                    historical_value = raw
                    historical_evidence = None
                    historical_rule = "historical-output-v1"
                if historical_value is None:
                    continue
                entity_key = str(source_label)
                historical_refs = _evidence_refs(
                    historical_evidence,
                    fallback_file_id=str(snapshot_id),
                )
                if not historical_refs:
                    continue
                historical_cells.append(
                    ComparableCell(
                        metric=str(metric),
                        entity_key=entity_key,
                        amount=historical_value,
                        rule_version=historical_rule,
                        evidence=historical_refs,
                    )
                )
                if str(metric) not in current_by_metric:
                    continue
                current_value, evidence_payloads = current_by_metric[str(metric)]
                flattened: list[dict[str, Any]] = []
                for payload in evidence_payloads or []:
                    decoded = (
                        json.loads(payload) if isinstance(payload, str) else payload
                    )
                    if isinstance(decoded, list):
                        flattened.extend(
                            item for item in decoded if isinstance(item, dict)
                        )
                current_refs = _evidence_refs(
                    flattened,
                    fallback_file_id=reconcile_run_id,
                )
                if not current_refs:
                    historical_cells.pop()
                    continue
                current_cells.append(
                    ComparableCell(
                        metric=str(metric),
                        entity_key=entity_key,
                        amount=current_value,
                        rule_version=rule_sha or "current-rule-set",
                        evidence=current_refs,
                    )
                )
            all_findings.extend(compare_cells(current_cells, historical_cells))

        true_difference_count = sum(
            finding.kind == DiffKind.TRUE_DIFFERENCE for finding in all_findings
        )
        metrics = {
            "reconcile_run_id": reconcile_run_id,
            "finding_count": len(all_findings),
            "true_difference_count": true_difference_count,
            "historical_output_count": history_count,
            "compared_trust_tier": compared_trust_tier,
        }
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, period_id, run_kind, status, code_sha,
                    input_manifest_sha256, rule_set_sha256, metrics_json,
                    finished_at
                )
                VALUES (?, ?, ?, 'adjudicate', 'succeeded', ?, ?, ?, ?,
                        current_timestamp)
                """,
                [
                    run_id,
                    contract_id,
                    period_id,
                    identity.value,
                    input_sha,
                    rule_sha,
                    json.dumps(metrics, sort_keys=True),
                ],
            )
            for finding in all_findings:
                diff_id = (
                    f"{run_id}:"
                    f"{hashlib.sha256(f'{finding.metric}|{finding.entity_key}'.encode()).hexdigest()[:24]}"
                )
                connection.execute(
                    """
                    INSERT INTO diff_finding (
                        diff_id, run_id, period_id, metric, source_row_key,
                        engine_value, historical_value, difference_value,
                        difference_kind, status, evidence_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        diff_id,
                        run_id,
                        period_id,
                        finding.metric,
                        finding.entity_key,
                        finding.current_amount,
                        finding.historical_amount,
                        finding.difference,
                        finding.kind.value,
                        (
                            "accepted"
                            if finding.kind == DiffKind.EQUAL
                            else "open"
                        ),
                        json.dumps(
                            {
                                "metric": finding.attribution.metric,
                                "rule_versions": list(
                                    finding.attribution.rule_versions
                                ),
                                "source_rows": list(
                                    finding.attribution.source_rows
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ],
                )
    return DiffRunResult(
        run_id=run_id,
        period_id=period_id,
        finding_count=len(all_findings),
        true_difference_count=true_difference_count,
        historical_output_count=history_count,
    )


def _freeze_blockers(
    database: DuckDBMemory,
    *,
    contract_id: str,
    period_id: str,
    reconcile_run_id: str,
) -> dict[str, int]:
    pending_decisions = int(
        database.fetchone_required(
            """
            SELECT count(*) FROM business_decision
            WHERE contract_id = ? AND status = 'pending'
            """,
            [contract_id],
        )[0]
    )
    incomplete_checklist = int(
        database.fetchone_required(
            """
            WITH latest_results AS (
                SELECT requirement_id, status,
                       row_number() OVER (
                           PARTITION BY requirement_id
                           ORDER BY checked_at DESC
                       ) AS position
                FROM checklist_result
                WHERE period_id = ?
            )
            SELECT count(*)
            FROM checklist_requirement requirement
            JOIN accounting_period period
              ON period.period_id = ?
            LEFT JOIN latest_results result
              ON result.requirement_id = requirement.requirement_id
             AND result.position = 1
            WHERE requirement.contract_id = ?
              AND requirement.required = true
              AND requirement.effective_from <= period.period_end
              AND (
                  requirement.effective_to IS NULL
                  OR requirement.effective_to >= period.period_start
              )
              AND coalesce(result.status, 'missing')
                  NOT IN ('present', 'not_applicable')
            """,
            [period_id, period_id, contract_id],
        )[0]
    )
    unresolved = int(
        database.fetchone_required(
            """
            SELECT count(*)
            FROM unresolved_balance u
            JOIN reconciliation_balance b ON b.balance_id = u.balance_id
            WHERE b.run_id = ? AND u.status = 'open'
            """,
            [reconcile_run_id],
        )[0]
    )
    open_diffs = int(
        database.fetchone_required(
            """
            SELECT count(*) FROM diff_finding
            WHERE period_id = ? AND status = 'open'
            """,
            [period_id],
        )[0]
    )
    return {
        "pending_business_decisions": pending_decisions,
        "incomplete_checklist": incomplete_checklist,
        "open_unresolved_balances": unresolved,
        "open_diff_findings": open_diffs,
    }


def create_baseline(
    workbench: WorkbenchPaths,
    *,
    period_token: str,
    store_id: str | None = None,
    freeze: bool = False,
    actor: str | None = None,
) -> BaselineRunResult:
    identity = require_committed_code() if freeze else resolve_code_identity()
    if freeze and not (actor or "").strip():
        raise ValueError("冻结黄金基线必须记录操作人")

    run_id = f"baseline_{uuid.uuid4().hex}"
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        period_id, contract_id, _store_id, _start, _end = _period_row(
            database,
            period_token,
            store_id=store_id,
        )
        reconcile_run_id, input_sha, run_rule_sha, metrics_json = (
            _latest_reconcile_run(database, period_id=period_id)
        )
        rule_sha = run_rule_sha or _rule_set_sha256(database)
        output = _baseline_output(database, run_id=reconcile_run_id)
        output_sha = deterministic_checksum(output)
        blockers = _freeze_blockers(
            database,
            contract_id=contract_id,
            period_id=period_id,
            reconcile_run_id=reconcile_run_id,
        )
        if freeze and any(blockers.values()):
            detail = "，".join(
                f"{name}={count}" for name, count in blockers.items() if count
            )
            raise RuntimeError(f"黄金基线冻结门禁未通过：{detail}")

        previous = database.fetchone_required(
            """
            SELECT coalesce(max(baseline_version), 0)
            FROM baseline
            WHERE contract_id = ? AND period_id = ?
            """,
            [contract_id, period_id],
        )
        version = int(previous[0]) + 1
        baseline_id = f"baseline_{period_id}_v{version}"
        status = "frozen" if freeze else "candidate"
        invariant_payload = {
            "reconcile_run_id": reconcile_run_id,
            "reconcile_metrics": json.loads(metrics_json),
            "freeze_blockers": blockers,
            "output_checksum_verified": True,
        }
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, period_id, run_kind, status, code_sha,
                    input_manifest_sha256, rule_set_sha256, metrics_json,
                    finished_at
                )
                VALUES (?, ?, ?, 'baseline', 'succeeded', ?, ?, ?, ?,
                        current_timestamp)
                """,
                [
                    run_id,
                    contract_id,
                    period_id,
                    identity.value,
                    input_sha,
                    rule_sha,
                    json.dumps(
                        {
                            "baseline_id": baseline_id,
                            "status": status,
                            "output_sha256": output_sha,
                        },
                        sort_keys=True,
                    ),
                ],
            )
            if freeze:
                connection.execute(
                    """
                    UPDATE baseline
                    SET status = 'superseded'
                    WHERE contract_id = ? AND period_id = ? AND status = 'frozen'
                    """,
                    [contract_id, period_id],
                )
            connection.execute(
                """
                INSERT INTO baseline (
                    baseline_id, contract_id, period_id, baseline_version,
                    input_manifest_sha256, rule_set_sha256, code_sha,
                    output_sha256, invariant_report_json, status,
                    frozen_by, frozen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CASE WHEN ? = 'frozen' THEN current_timestamp ELSE NULL END)
                """,
                [
                    baseline_id,
                    contract_id,
                    period_id,
                    version,
                    input_sha,
                    rule_sha,
                    identity.value,
                    output_sha,
                    json.dumps(
                        invariant_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    status,
                    actor.strip() if actor else None,
                    status,
                ],
            )
    return BaselineRunResult(
        run_id=run_id,
        baseline_id=baseline_id,
        period_id=period_id,
        baseline_version=version,
        status=status,
        output_sha256=output_sha,
        code_sha=identity.value,
    )
