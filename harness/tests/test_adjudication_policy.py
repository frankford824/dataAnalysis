from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from commerce_harness.adjudication_policy import (
    BUSINESS_POLICY_VERSION,
    DEFAULT_NORMALIZATION_RULE_VERSION,
    apply_evidence_driven_adjudication,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.rules.wallet import WalletRuleSet

_SCHEMA = pa.schema(
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


def _seed_contract(database: DuckDBMemory, *period_keys: str) -> None:
    database.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        )
        VALUES (
            'contract-demo', 'demo', 'enterprise-demo', 'store-demo', 'taobao',
            1, DATE '2026-01-01', 'active', '{}'
        )
        """
    )
    for period_key in period_keys:
        month = int(period_key[-2:])
        next_month = month + 1
        database.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES (?, 'contract-demo', 'store-demo', ?, ?, 'open')
            """,
            [
                f"period-{period_key}",
                f"2026-{month:02d}-01",
                f"2026-{next_month:02d}-01",
            ],
        )


def _canonical_row(
    *,
    period_key: str,
    source_type: str,
    business_key: str,
    amount: str,
    evidence_file_id: str,
    evidence_row_no: int,
    formula_polluted: bool = False,
    detail_count: int | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"business_key_kind": "test"}
    if formula_polluted:
        attributes["wallet_business_description_formula_ignored"] = "true"
    if detail_count is not None:
        attributes["detail_count"] = str(detail_count)
    return {
        "dataset_kind": (
            "control_total" if source_type.endswith("_control_total") else "platform_ledger"
        ),
        "source_type": source_type,
        "side": "platform",
        "business_key": business_key,
        "settlement_batch_key": None,
        "cash_bridge_key": None,
        "occurred_at": f"2026-{period_key[-2:]}-01T00:00:00",
        "period_key": period_key,
        "amount": Decimal(amount),
        "metric": (
            "platform_control_net"
            if source_type.endswith("_control_total")
            else "wallet_transaction"
        ),
        "sku": None,
        "evidence_file_id": evidence_file_id,
        "evidence_row_no": evidence_row_no,
        "source_member": None,
        "source_sheet": "账单",
        "attributes_json": json.dumps(attributes, ensure_ascii=False, sort_keys=True),
    }


def _add_revision(
    database: DuckDBMemory,
    tmp_path: Path,
    *,
    period_key: str,
    revision_id: str,
    source_kind: str,
    logical_input_key: str,
    source_uri: str,
    rows: list[dict[str, Any]],
    status: str = "candidate",
    revision_no: int = 1,
) -> None:
    snapshot_id = f"snapshot-{revision_id}"
    artifact_id = f"artifact-{revision_id}"
    parquet_path = tmp_path / f"{artifact_id}.parquet"
    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    pq.write_table(table, parquet_path)
    content_sha256 = __import__("hashlib").sha256(parquet_path.read_bytes()).hexdigest()
    database.execute(
        """
        INSERT INTO source_snapshot (
            snapshot_id, content_sha256, byte_size, object_uri, source_uri,
            original_name, media_type, captured_at, manifest_json
        )
        VALUES (?, ?, ?, ?, ?, ?, 'application/octet-stream', ?, '{}')
        """,
        [
            snapshot_id,
            content_sha256,
            parquet_path.stat().st_size,
            str(parquet_path),
            source_uri,
            Path(source_uri.replace("\\", "/")).name,
            datetime.now(UTC),
        ],
    )
    database.execute(
        """
        INSERT INTO input_revision (
            revision_id, contract_id, period_id, source_kind, logical_input_key,
            revision_no, snapshot_id, status, reason
        )
        VALUES (?, 'contract-demo', ?, ?, ?, ?, ?, ?, 'test candidate')
        """,
        [
            revision_id,
            f"period-{period_key}",
            source_kind,
            logical_input_key,
            revision_no,
            snapshot_id,
            status,
        ],
    )
    database.execute(
        """
        INSERT INTO input_revision_state (revision_id, status, reason)
        VALUES (?, ?, 'test candidate')
        """,
        [revision_id, status],
    )
    database.execute(
        """
        INSERT INTO normalized_artifact (
            artifact_id, normalization_run_id, input_revision_id, content_sha256,
            source_snapshot_id, dataset_kind, schema_version, rule_version,
            row_count, byte_size, parquet_uri, partition_json, arrow_schema, created_at
        )
        VALUES (
            ?, 'normalize-test', ?, ?, ?, ?, 'canonical-v1', ?,
            ?, ?, ?, ?, ?, ?
        )
        """,
        [
            artifact_id,
            revision_id,
            content_sha256,
            snapshot_id,
            rows[0]["dataset_kind"],
            DEFAULT_NORMALIZATION_RULE_VERSION,
            len(rows),
            parquet_path.stat().st_size,
            str(parquet_path),
            json.dumps({"period": period_key}),
            str(_SCHEMA),
            datetime.now(UTC),
        ],
    )


def _add_control(
    database: DuckDBMemory,
    tmp_path: Path,
    *,
    period_key: str,
    amount: str,
    detail_count: int,
) -> None:
    _add_revision(
        database,
        tmp_path,
        period_key=period_key,
        revision_id=f"control-{period_key}",
        source_kind="wechat_control_total",
        logical_input_key=f"control_total:{period_key}",
        source_uri=rf"finance-win-ro://D:\archive\control-{period_key}.xlsx",
        rows=[
            _canonical_row(
                period_key=period_key,
                source_type="wechat_control_total",
                business_key=period_key,
                amount=amount,
                evidence_file_id=f"control-{period_key}",
                evidence_row_no=2,
                detail_count=detail_count,
            )
        ],
        status="current",
    )


def _status_by_revision(database: DuckDBMemory) -> dict[str, tuple[str, str | None]]:
    return {
        str(row[0]): (str(row[1]), str(row[2]) if row[2] is not None else None)
        for row in database.execute(
            """
            SELECT revision_id, status, approved_by
            FROM input_revision_state
            ORDER BY revision_id
            """
        ).fetchall()
    }


def _seed_2603(database: DuckDBMemory, tmp_path: Path) -> None:
    raw_rows = [
        _canonical_row(
            period_key="2603",
            source_type="wechat_ledger",
            business_key="WX-2603-1",
            amount="-10000.0000",
            evidence_file_id="raw-2603",
            evidence_row_no=2,
        ),
        _canonical_row(
            period_key="2603",
            source_type="wechat_ledger",
            business_key="WX-2603-2",
            amount="-4191.2500",
            evidence_file_id="raw-2603",
            evidence_row_no=3,
        ),
    ]
    processed_rows = [
        _canonical_row(
            period_key="2603",
            source_type="wechat_ledger",
            business_key="WX-2603-1",
            amount="20000.0000",
            evidence_file_id="processed-2603",
            evidence_row_no=2,
            formula_polluted=True,
        ),
        _canonical_row(
            period_key="2603",
            source_type="wechat_ledger",
            business_key="WX-2603-2",
            amount="3237.7500",
            evidence_file_id="processed-2603",
            evidence_row_no=3,
        ),
    ]
    _add_revision(
        database,
        tmp_path,
        period_key="2603",
        revision_id="raw-2603",
        source_kind="wechat_ledger",
        logical_input_key="platform_ledger:2603",
        source_uri=r"finance-win-ro://C:\店铺\原始数据\微信明细\2603.xlsx",
        rows=raw_rows,
        revision_no=1,
    )
    _add_revision(
        database,
        tmp_path,
        period_key="2603",
        revision_id="processed-2603",
        source_kind="wechat_ledger",
        logical_input_key="platform_ledger:2603",
        source_uri=r"finance-win-ro://C:\店铺\修改后数据\微信明细\2603.xlsx",
        rows=processed_rows,
        revision_no=2,
    )
    _add_control(
        database,
        tmp_path,
        period_key="2603",
        amount="-14191.2500",
        detail_count=2,
    )


def _seed_2604(database: DuckDBMemory, tmp_path: Path) -> None:
    def clean_rows(evidence_file_id: str) -> list[dict[str, Any]]:
        return [
            _canonical_row(
                period_key="2604",
                source_type="wechat_ledger",
                business_key="WX-2604-1",
                amount="9000.0000",
                evidence_file_id=evidence_file_id,
                evidence_row_no=2,
            ),
            _canonical_row(
                period_key="2604",
                source_type="wechat_ledger",
                business_key="WX-2604-2",
                amount="734.3600",
                evidence_file_id=evidence_file_id,
                evidence_row_no=3,
            ),
        ]

    _add_revision(
        database,
        tmp_path,
        period_key="2604",
        revision_id="raw-2604",
        source_kind="wechat_ledger",
        logical_input_key="platform_ledger:2604",
        source_uri=r"finance-win-ro://C:\店铺\原始数据\微信明细\2604.xlsx",
        rows=clean_rows("raw-2604"),
        revision_no=1,
    )
    _add_revision(
        database,
        tmp_path,
        period_key="2604",
        revision_id="archive-2604",
        source_kind="wechat_ledger",
        logical_input_key="platform_ledger:2604",
        source_uri=r"finance-win-ro://D:\KAOSHI\OneDrive\微信收款\2604.xlsx",
        rows=clean_rows("archive-2604"),
        revision_no=2,
    )
    _add_revision(
        database,
        tmp_path,
        period_key="2604",
        revision_id="processed-2604",
        source_kind="wechat_ledger",
        logical_input_key="platform_ledger:2604",
        source_uri=r"finance-win-ro://C:\店铺\修改后数据\微信明细\2604.xlsx",
        rows=[
            _canonical_row(
                period_key="2604",
                source_type="wechat_ledger",
                business_key="WX-2604-1",
                amount="36000.0000",
                evidence_file_id="processed-2604",
                evidence_row_no=2,
                formula_polluted=True,
            ),
            _canonical_row(
                period_key="2604",
                source_type="wechat_ledger",
                business_key="WX-2604-2",
                amount="642.5000",
                evidence_file_id="processed-2604",
                evidence_row_no=3,
            ),
        ],
        revision_no=3,
    )
    _add_control(
        database,
        tmp_path,
        period_key="2604",
        amount="9734.3600",
        detail_count=2,
    )


def test_auto_selects_2603_control_match_and_2604_original_equivalent(
    tmp_path: Path,
) -> None:
    with DuckDBMemory() as database:
        database.initialize()
        _seed_contract(database, "2603", "2604")
        _seed_2603(database, tmp_path)
        _seed_2604(database, tmp_path)

        summary = apply_evidence_driven_adjudication(database)

        assert summary.groups_evaluated == 2
        assert summary.groups_selected == 2
        assert summary.groups_deferred == 0
        assert summary.selected_revision_ids == ("raw-2603", "raw-2604")
        statuses = _status_by_revision(database)
        assert statuses["raw-2603"] == ("current", "policy:evidence-auto-v1")
        assert statuses["processed-2603"][0] == "superseded"
        assert statuses["raw-2604"][0] == "current"
        assert statuses["archive-2604"][0] == "superseded"
        assert statuses["processed-2604"][0] == "superseded"

        decisions = database.execute(
            """
            SELECT decision, finding_json, evidence_json, rationale
            FROM adjudication
            WHERE subject_kind = 'input_revision_selection'
            ORDER BY subject_key
            """
        ).fetchall()
        assert len(decisions) == 2
        assert all(str(row[0]) == "accept_engine" for row in decisions)
        evidence_2604 = json.loads(str(decisions[1][2]))
        by_revision = {
            item["revision_id"]: item for item in evidence_2604["candidates"]
        }
        assert (
            by_revision["raw-2604"]["business_content_sha256"]
            == by_revision["archive-2604"]["business_content_sha256"]
        )
        assert by_revision["raw-2604"]["control_exact"] is True
        assert by_revision["processed-2604"]["formula_pollution_count"] == 1
        assert "控制总额" in str(decisions[1][3])


def test_insufficient_evidence_remains_candidate_and_records_defer(
    tmp_path: Path,
) -> None:
    with DuckDBMemory() as database:
        database.initialize()
        _seed_contract(database, "2603")
        for revision_no, revision_id, business_key in (
            (1, "raw-a", "WX-A"),
            (2, "archive-b", "WX-B"),
        ):
            _add_revision(
                database,
                tmp_path,
                period_key="2603",
                revision_id=revision_id,
                source_kind="wechat_ledger",
                logical_input_key="platform_ledger:2603",
                source_uri=(
                    rf"finance-win-ro://C:\店铺\原始数据\{revision_id}.xlsx"
                    if revision_id == "raw-a"
                    else rf"finance-win-ro://D:\archive\{revision_id}.xlsx"
                ),
                rows=[
                    _canonical_row(
                        period_key="2603",
                        source_type="wechat_ledger",
                        business_key=business_key,
                        amount="100.0000",
                        evidence_file_id=revision_id,
                        evidence_row_no=2,
                    )
                ],
                revision_no=revision_no,
            )
        _add_control(
            database,
            tmp_path,
            period_key="2603",
            amount="100.0000",
            detail_count=1,
        )

        summary = apply_evidence_driven_adjudication(database)

        assert summary.groups_selected == 0
        assert summary.groups_deferred == 1
        statuses = _status_by_revision(database)
        assert statuses["raw-a"][0] == "candidate"
        assert statuses["archive-b"][0] == "candidate"
        decision, rationale = database.fetchone_required(
            """
            SELECT decision, rationale
            FROM adjudication
            WHERE subject_kind = 'input_revision_selection'
            """
        )
        assert decision == "defer"
        assert "多个不同业务内容" in str(rationale)


def test_repeated_run_is_idempotent(tmp_path: Path) -> None:
    with DuckDBMemory() as database:
        database.initialize()
        _seed_contract(database, "2603", "2604")
        _seed_2603(database, tmp_path)
        _seed_2604(database, tmp_path)

        first = apply_evidence_driven_adjudication(database)
        first_counts = database.fetchone_required(
            """
            SELECT
                (SELECT count(*) FROM adjudication),
                (SELECT count(*) FROM business_decision_event),
                (SELECT count(*) FROM rule_version)
            """
        )
        second = apply_evidence_driven_adjudication(database)
        second_counts = database.fetchone_required(
            """
            SELECT
                (SELECT count(*) FROM adjudication),
                (SELECT count(*) FROM business_decision_event),
                (SELECT count(*) FROM rule_version)
            """
        )

        assert first.groups_selected == 2
        assert second.groups_evaluated == 0
        assert second.adjudications_recorded == 0
        assert second.business_policies_decided == 0
        assert second.wallet_rules_registered == 0
        assert first_counts == second_counts == (2, 3, 2)


@pytest.mark.parametrize(
    ("mode", "expected_answer", "expected_mapping"),
    [
        ("platform_wallet", "not_applicable", "not_applicable"),
        (
            "bank_three_way",
            "explicit_effective_dated_mapping_required",
            "required",
        ),
    ],
)
def test_versioned_policies_and_wallet_metadata(
    tmp_path: Path,
    mode: str,
    expected_answer: str,
    expected_mapping: str,
) -> None:
    del tmp_path
    with DuckDBMemory() as database:
        database.initialize()
        _seed_contract(database, "2603")

        summary = apply_evidence_driven_adjudication(
            database,
            reconciliation_mode=mode,  # type: ignore[arg-type]
        )

        assert summary.business_policies_decided == 3
        policy_rows = database.execute(
            """
            SELECT subject_kind, decision_json
            FROM business_decision
            WHERE contract_id = 'contract-demo'
            ORDER BY subject_kind
            """
        ).fetchall()
        policies = {str(row[0]): json.loads(str(row[1])) for row in policy_rows}
        assert set(policies) == {
            "freight_period_attribution",
            "fund_account_effectivity",
            "shared_cost_attribution",
        }
        assert policies["freight_period_attribution"]["open_period_behavior"] == (
            "post_to_original_period"
        )
        assert policies["freight_period_attribution"]["closed_period_behavior"] == (
            "create_adjustment_linked_to_original_period"
        )
        assert policies["shared_cost_attribution"]["answer"] == (
            "direct_then_positive_net_sales_share"
        )
        assert policies["shared_cost_attribution"]["zero_denominator_behavior"] == (
            "block_and_keep_unresolved"
        )
        fund_policy = policies["fund_account_effectivity"]
        assert fund_policy["answer"] == expected_answer
        assert fund_policy["bank_account_mapping"] == expected_mapping
        assert all(
            policy["policy_version"] == BUSINESS_POLICY_VERSION
            for policy in policies.values()
        )

        event_count = database.fetchone_required(
            "SELECT count(*) FROM business_decision_event"
        )[0]
        assert event_count == 3

        wallet = WalletRuleSet()
        rule_rows = database.execute(
            """
            SELECT definition.logical_key, version.status, version.version,
                   version.checksum_sha256, version.definition_json
            FROM rule_definition definition
            JOIN rule_version version ON version.rule_id = definition.rule_id
            ORDER BY definition.logical_key
            """
        ).fetchall()
        assert [str(row[0]) for row in rule_rows] == [
            "alipay_classification",
            "order_id_extraction",
        ]
        assert all(str(row[1]) == "approved" for row in rule_rows)
        assert all(int(row[2]) == 1 for row in rule_rows)
        assert all(str(row[3]) == wallet.checksum for row in rule_rows)
        assert all(
            json.loads(str(row[4]))["ruleset_version"] == wallet.version
            for row in rule_rows
        )
