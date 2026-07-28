from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from commerce_harness.code_identity import CodeIdentity, resolve_code_identity
from commerce_harness.kernel.contract import taobao_three_way_contract
from commerce_harness.kernel.recon import (
    EvidenceRef,
    SettlementCashBridge,
    make_item,
    reconcile_items,
)
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.normalization import CanonicalRow, CanonicalSide
from commerce_harness.phase_a import (
    NORMALIZATION_RULE_VERSION,
    _canonical_business_sha256,
    _current_canonical_rows,
    _deduplicate_incremental_rows,
    _enrich_route_period,
    _evidence_refs,
    _historical_totals,
    _insert_certified_pnl,
    _logical_input_key,
    _NormalizationCandidate,
    _period_row,
    _period_tokens,
    _persist_reconciliation_result,
    _select_current_candidates,
    compare_period,
    create_baseline,
    normalize_workspace,
    reconcile_period,
)
from commerce_harness.workbench import WorkbenchPaths


def _canonical_row(
    *,
    dataset_kind: str = "order",
    period_key: str = "2602",
    metric: str | None = None,
) -> CanonicalRow:
    return CanonicalRow(
        dataset_kind=dataset_kind,
        source_type="baobei_order",
        side=CanonicalSide.ORDER,
        business_key="ORDER-1",
        cash_bridge_key=None,
        occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
        amount=Decimal("10.0000"),
        period_key=period_key,
        evidence_row=2,
        source_name="orders.csv",
        source_snapshot_id="snapshot-1",
        metric=metric,
    )


def _candidate(
    snapshot_id: str,
    digest: str,
    purpose: str,
    *,
    source_type: str = "baobei_order",
    business_digest: str | None = None,
) -> _NormalizationCandidate:
    row = _canonical_row()
    if source_type != row.source_type:
        row = CanonicalRow(
            dataset_kind=row.dataset_kind,
            source_type=source_type,
            side=row.side,
            business_key=row.business_key,
            cash_bridge_key=row.cash_bridge_key,
            occurred_at=row.occurred_at,
            amount=row.amount,
            period_key=row.period_key,
            evidence_row=row.evidence_row,
            source_name=row.source_name,
            source_snapshot_id=row.source_snapshot_id,
            metric=row.metric,
        )
    return _NormalizationCandidate(
        logical_key="order:2602",
        dataset_kind="order",
        period_key="2602",
        snapshot_id=snapshot_id,
        snapshot_sha256=digest,
        business_sha256=business_digest or digest,
        source_uri=f"finance-win-ro://D:\\{snapshot_id}.csv",
        source_modified_ns=1,
        purpose=purpose,
        original_name="2602-orders.csv",
        rows=(row,),
    )


def test_phase_a_period_and_revision_helpers_are_explicit() -> None:
    assert _period_tokens("orders_202602.csv") == ("2602",)
    assert _enrich_route_period(
        {"kind": "file"},
        original_name="orders_2602.csv",
        allowed_periods={"2602"},
    )["period_key"] == "2602"
    assert "period_key" not in _enrich_route_period(
        {"kind": "file"},
        original_name="range_2602_2603.csv",
        allowed_periods={"2602", "2603"},
    )
    historical = _canonical_row(
        dataset_kind="historical_pnl",
        metric="sales",
    )
    assert ":group-" in _logical_input_key(
        row=historical,
        source_uri=r"finance-win-ro://D:\Finance\History\2602.csv",
        original_name="2602.csv",
    )
    platform_fee = _canonical_row(dataset_kind="platform_fee")
    fee_2602 = _logical_input_key(
        row=platform_fee,
        source_uri=r"finance-win-ro://D:\Finance\Fees\2602.csv",
        original_name="service-fee_202602.csv",
    )
    fee_2603_late_adjustment = _logical_input_key(
        row=platform_fee,
        source_uri=r"finance-win-ro://D:\Finance\Fees\2603.csv",
        original_name="service-fee_202603.csv",
    )
    assert ":family-" in fee_2602
    assert ":export-2602" in fee_2602
    assert ":export-2603" in fee_2603_late_adjustment
    assert fee_2602 != fee_2603_late_adjustment
    assert fee_2602 == _logical_input_key(
        row=platform_fee,
        source_uri=r"finance-win-ro://D:\CopiedElsewhere\2602.csv",
        original_name="service-fee_202602.csv",
    )
    assert _historical_totals((historical,))["sales"]["value"] == "10.0000"
    second = CanonicalRow(
        dataset_kind=historical.dataset_kind,
        source_type=historical.source_type,
        side=historical.side,
        business_key="ORDER-2",
        cash_bridge_key=None,
        occurred_at=historical.occurred_at,
        amount=Decimal("20.0000"),
        period_key=historical.period_key,
        evidence_row=99,
        source_name="different-container.xlsx",
        source_snapshot_id="snapshot-2",
        metric=historical.metric,
    )
    assert _canonical_business_sha256((historical, second)) == (
        _canonical_business_sha256((second, historical))
    )

    current, reasons = _select_current_candidates(
        [_candidate("only", "a" * 64, "historical_workspace")]
    )
    assert current == {"only"}
    assert "唯一内容版本" in reasons["only"]

    preferred, _ = _select_current_candidates(
        [
            _candidate("raw", "b" * 64, "historical_workspace"),
            _candidate("canonical", "c" * 64, "orders"),
        ]
    )
    assert preferred == {"canonical"}

    ambiguous, reasons = _select_current_candidates(
        [
            _candidate("candidate-1", "d" * 64, "historical_workspace"),
            _candidate("candidate-2", "e" * 64, "historical_workspace"),
        ]
    )
    assert ambiguous == set()
    assert all("人工选择" in value for value in reasons.values())

    equivalent, _ = _select_current_candidates(
        [
            _candidate(
                "archive",
                "1" * 64,
                "historical_workspace",
                business_digest="9" * 64,
            ),
            _candidate(
                "extracted",
                "2" * 64,
                "orders",
                business_digest="9" * 64,
            ),
        ]
    )
    assert equivalent == {"extracted"}

    exact_duplicate, _ = _select_current_candidates(
        [
            _candidate(
                "first-copy",
                "7" * 64,
                "orders",
                business_digest="8" * 64,
            ),
            _candidate(
                "second-copy",
                "7" * 64,
                "orders",
                business_digest="8" * 64,
            ),
        ]
    )
    assert len(exact_duplicate) == 1

    parallel, _ = _select_current_candidates(
        [
            _candidate(
                "alipay",
                "f" * 64,
                "settlement",
                source_type="alipay_ledger",
            ),
            _candidate(
                "wechat",
                "0" * 64,
                "settlement",
                source_type="wechat_ledger",
            ),
        ]
    )
    assert parallel == {"alipay", "wechat"}

    with DuckDBMemory() as memory:
        memory.initialize()
        for invalid in ("202602", "2613"):
            try:
                _period_row(memory, invalid)
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("invalid period must be rejected")


def test_incremental_platform_fee_deduplicates_exact_facts_only() -> None:
    base = {
        "dataset_kind": "platform_fee",
        "source_type": "taobao_platform_fee",
        "side": "platform",
        "business_key": "ORDER-1",
        "settlement_batch_key": None,
        "cash_bridge_key": None,
        "occurred_at": "2026-02-12T00:00:00+00:00",
        "period_key": "2602",
        "amount": Decimal("-5.0000"),
        "metric": "platform_fee",
        "sku": None,
        "attributes_json": json.dumps(
            {"category": "佣金", "business_key_kind": "main_order_id"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "evidence_file_id": "snapshot-first",
        "evidence_row_no": 10,
    }
    repeated_in_later_export = {
        **base,
        "attributes_json": json.dumps(
            {
                "billing_period": "20260313",
                "business_key_kind": "main_order_id",
                "category": "佣金",
                "fee_amount": "5.00",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "evidence_file_id": "snapshot-later",
        "evidence_row_no": 99,
    }
    repeated_in_same_export = {
        **base,
        "evidence_row_no": 11,
    }
    distinct_fee_on_same_order = {
        **base,
        "amount": Decimal("-2.0000"),
        "attributes_json": json.dumps(
            {"category": "软件服务费", "business_key_kind": "main_order_id"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "evidence_file_id": "snapshot-later",
        "evidence_row_no": 100,
    }
    snapshot_rows = [
        {
            **base,
            "dataset_kind": "order",
            "source_type": "baobei_order",
            "evidence_file_id": "order-a",
        },
        {
            **base,
            "dataset_kind": "order",
            "source_type": "baobei_order",
            "evidence_file_id": "order-b",
        },
    ]

    result = _deduplicate_incremental_rows(
        [
            base,
            repeated_in_same_export,
            repeated_in_later_export,
            distinct_fee_on_same_order,
            *snapshot_rows,
        ]
    )

    assert len(result) == 5
    assert result[0]["evidence_file_id"] == "snapshot-first"
    assert result[1]["evidence_file_id"] == "snapshot-first"
    assert result[2]["amount"] == Decimal("-2.0000")
    assert [row["evidence_file_id"] for row in result[3:]] == [
        "order-a",
        "order-b",
    ]


def _prepare(memory: DuckDBMemory) -> None:
    memory.initialize()
    for snapshot_id in ("orders", "platform", "bank", "bridge"):
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                captured_at, manifest_json
            )
            VALUES (?, ?, 1, ?, ?, current_timestamp, '{}')
            """,
            [
                snapshot_id,
                f"sha-{snapshot_id}",
                f"/objects/{snapshot_id}",
                snapshot_id,
            ],
        )
    memory.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        )
        VALUES ('contract-1', 'contract', 'enterprise-1', 'store-1', 'taobao',
                1, DATE '2026-02-01', 'active', '{}')
        """
    )
    memory.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        )
        VALUES ('period-1', 'contract-1', 'store-1', DATE '2026-02-01',
                DATE '2026-02-28', 'open')
        """
    )
    memory.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status
        )
        VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'running')
        """
    )


def test_persists_real_key_cash_bridge_result_without_key_collapse() -> None:
    contract = taobao_three_way_contract()
    occurred_at = datetime(2026, 2, 28, tzinfo=UTC)

    def evidence(file_id: str, row_no: int) -> tuple[EvidenceRef, ...]:
        return (
            EvidenceRef(
                file_id=file_id,
                row_no=row_no,
                field="amount",
                rule_version="normalize-v1",
                source_value="100.0000",
            ),
        )

    result = reconcile_items(
        [
            make_item(
                contract=contract,
                source_type="baobei_order",
                business_key="ORDER-1",
                value="100",
                occurred_at=occurred_at,
                evidence=evidence("orders", 2),
            ),
            make_item(
                contract=contract,
                source_type="alipay_ledger",
                business_key="ORDER-1",
                settlement_batch_key="SETTLEMENT-9",
                value="100",
                occurred_at=occurred_at,
                evidence=evidence("platform", 3),
            ),
            make_item(
                contract=contract,
                source_type="bank_statement",
                business_key="BANK-TX-7",
                cash_bridge_key="BANK-MEMO-9",
                value="100",
                occurred_at=occurred_at,
                evidence=evidence("bank", 4),
            ),
        ],
        contract,
        link_rule_version="order-platform-v1",
        cash_bridges=[
            SettlementCashBridge(
                bridge_id="bridge-1",
                settlement_batch_key="SETTLEMENT-9",
                cash_bridge_key="BANK-MEMO-9",
                rule_version="cash-bridge-v1",
                evidence=evidence("bridge", 5),
            )
        ],
    )

    with DuckDBMemory() as memory:
        _prepare(memory)
        _persist_reconciliation_result(
            memory,
            run_id="run-1",
            contract_id="contract-1",
            period_id="period-1",
            result=result,
        )
        assert memory.execute(
            "SELECT count(*) FROM reconciliation_item"
        ).fetchone() == (3,)
        assert memory.execute(
            """
            SELECT count(*), count(DISTINCT snapshot_id)
            FROM evidence_binding
            """
        ).fetchone() == (8, 4)
        assert memory.execute(
            """
            SELECT business_key, settlement_batch_key, cash_bridge_key
            FROM reconciliation_item
            WHERE side = 'fund'
            """
        ).fetchone() == ("BANK-TX-7", None, "BANK-MEMO-9")
        assert memory.execute(
            """
            SELECT count(*) FROM reconciliation_balance
            WHERE status = 'balanced'
            """
        ).fetchone() == (2,)
        assert memory.execute(
            "SELECT count(*) FROM unresolved_balance"
        ).fetchone() == (0,)


def test_evidence_refs_never_invent_first_row() -> None:
    assert _evidence_refs(None, fallback_file_id="snapshot-fallback") == ()
    assert _evidence_refs(
        [{"file_id": "snapshot-1", "row_no": None}],
        fallback_file_id="snapshot-fallback",
    ) == ()
    assert _evidence_refs(
        [{"file_id": "snapshot-1", "row_no": 0}],
        fallback_file_id="snapshot-fallback",
    ) == ()


def test_compare_period_persists_row_attributed_difference(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.duckdb"
    with DuckDBMemory(database_path) as memory:
        _prepare(memory)
        memory.execute(
            """
            UPDATE run_log
            SET status = 'succeeded', finished_at = current_timestamp,
                input_manifest_sha256 = 'input-sha',
                rule_set_sha256 = 'rule-sha', metrics_json = '{}'
            WHERE run_id = 'run-1'
            """
        )
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES ('snapshot-history', ?, 1, '/snapshot', 'fixture://history',
                    'history.csv', current_timestamp, '{}')
            """,
            ["a" * 64],
        )
        memory.execute(
            """
            INSERT INTO historical_output (
                historical_output_id, contract_id, period_id, snapshot_id,
                output_kind, source_label, totals_json, status
            )
            VALUES ('history-1', 'contract-1', 'period-1', 'snapshot-history',
                    'pnl_16', 'history-a', ?, 'competing')
            """,
            [
                json.dumps(
                    {
                        "transaction_receipt": {
                            "value": "101.0000",
                            "evidence": [
                                {
                                    "file_id": "snapshot-history",
                                    "row_no": 2,
                                    "field": "交易收款",
                                }
                            ],
                        }
                    }
                )
            ],
        )
        memory.execute(
            """
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json
            )
            VALUES ('pnl-1', 'run-1', 'period-1', 'store-1', 'sku-1',
                    'transaction_receipt', 'pnl-v1', 100.0000, ?)
            """,
            [
                json.dumps(
                    [
                        {
                            "file_id": "orders",
                            "row_no": 3,
                            "field": "amount",
                        }
                    ]
                )
            ],
        )
    workbench = WorkbenchPaths(
        root=tmp_path,
        database=database_path,
        snapshots=tmp_path / "snapshots",
        normalized=tmp_path / "normalized",
        reports=tmp_path / "reports",
        llm_logs=tmp_path / "llm_logs",
        locks=tmp_path / "locks",
    )

    result = compare_period(workbench, period_token="2602")

    assert result.finding_count == 1
    assert result.true_difference_count == 1
    with DuckDBMemory(database_path) as memory:
        row = memory.execute(
            """
            SELECT engine_value, historical_value, difference_value,
                   difference_kind, status
            FROM diff_finding
            """
        ).fetchone()
        assert row is not None
        assert tuple(str(value) for value in row[:3]) == (
            "100.0000",
            "101.0000",
            "-1.0000",
        )
        assert row[3:] == ("true_difference", "open")


def test_candidate_baseline_records_code_identity_and_output_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ledger.duckdb"
    with DuckDBMemory(database_path) as memory:
        _prepare(memory)
        memory.execute(
            """
            UPDATE run_log
            SET status = 'succeeded', finished_at = current_timestamp,
                input_manifest_sha256 = 'input-sha',
                rule_set_sha256 = 'rule-sha', metrics_json = '{}'
            WHERE run_id = 'run-1'
            """
        )
    workbench = WorkbenchPaths(
        root=tmp_path,
        database=database_path,
        snapshots=tmp_path / "snapshots",
        normalized=tmp_path / "normalized",
        reports=tmp_path / "reports",
        llm_logs=tmp_path / "llm_logs",
        locks=tmp_path / "locks",
    )
    expected = resolve_code_identity()
    result = create_baseline(workbench, period_token="2602")

    assert result.status == "candidate"
    assert len(result.output_sha256) == 64
    assert result.code_sha == expected.value
    with DuckDBMemory(database_path) as memory:
        assert memory.execute(
            "SELECT status, output_sha256 FROM baseline"
        ).fetchone() == ("candidate", result.output_sha256)


def test_candidate_baseline_records_dirty_code_identity_when_worktree_dirty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ledger.duckdb"
    with DuckDBMemory(database_path) as memory:
        _prepare(memory)
        memory.execute(
            """
            UPDATE run_log
            SET status = 'succeeded', finished_at = current_timestamp,
                input_manifest_sha256 = 'input-sha',
                rule_set_sha256 = 'rule-sha', metrics_json = '{}'
            WHERE run_id = 'run-1'
            """
        )
    workbench = WorkbenchPaths(
        root=tmp_path,
        database=database_path,
        snapshots=tmp_path / "snapshots",
        normalized=tmp_path / "normalized",
        reports=tmp_path / "reports",
        llm_logs=tmp_path / "llm_logs",
        locks=tmp_path / "locks",
    )
    dirty = CodeIdentity(
        commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        worktree_sha256="b" * 64,
    )
    monkeypatch.setattr(
        "commerce_harness.phase_a.resolve_code_identity",
        lambda start=None: dirty,
    )
    result = create_baseline(workbench, period_token="2602")
    assert "+dirty." in result.code_sha
    assert result.code_sha == dirty.value


def test_normalize_workspace_writes_current_decimal_parquet(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.duckdb"
    content = (
        b"Order ID,Date,Paid,Refund\n"
        b"ORDER-1,2026-02-03,100.10,5.10\n"
    )
    digest = hashlib.sha256(content).hexdigest()
    snapshot_path = (
        tmp_path
        / "snapshots"
        / "objects"
        / "sha256"
        / digest[:2]
        / digest
    )
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_bytes(content)
    stale_object_path = tmp_path / "former-mount" / "snapshots" / digest
    reports = tmp_path / "reports"
    reports.mkdir()
    source_path = r"D:\FinanceData\2602-orders.csv"
    (reports / "source-inventory.json").write_text(
        json.dumps(
            {
                "ssh_alias": "finance-win-ro",
                "records": [
                    {
                        "path": source_path,
                        "purpose": "orders",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with DuckDBMemory(database_path) as memory:
        _prepare(memory)
        memory.execute("DELETE FROM run_log")
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                source_modified_ns, original_name, captured_at, manifest_json
            )
            VALUES ('snapshot-order', ?, ?, ?, ?, 1, '2602-orders.csv',
                    current_timestamp, '{}')
            """,
            [
                digest,
                len(content),
                str(stale_object_path),
                f"finance-win-ro://{source_path}",
            ],
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, run_kind, status,
                input_manifest_sha256, finished_at
            )
            VALUES ('parse-1', 'contract-1', 'parse', 'succeeded',
                    'input-sha', current_timestamp)
            """
        )
        route = {
            "kind": "file",
            "source_kind": "order",
            "template_id": "taobao_order_v1",
            "fields": {
                "order_id": "Order ID",
                "business_date": "Date",
                "paid_amount": "Paid",
                "refund_amount": "Refund",
            },
            "location": {
                "header_row": 1,
                "sheet": None,
                "member": "2602-orders.csv",
            },
        }
        memory.execute(
            """
            INSERT INTO source_profile (
                profile_id, run_id, snapshot_id, parser_version, status,
                source_kind, template_id, fingerprint_sha256, route_json
            )
            VALUES ('profile-1', 'parse-1', 'snapshot-order', 'test-v1',
                    'matched', 'order', 'taobao_order_v1', ?, ?)
            """,
            ["b" * 64, json.dumps(route)],
        )
    workbench = WorkbenchPaths(
        root=tmp_path,
        database=database_path,
        snapshots=tmp_path / "snapshots",
        normalized=tmp_path / "normalized",
        reports=reports,
        llm_logs=tmp_path / "llm_logs",
        locks=tmp_path / "locks",
    )

    result = normalize_workspace(workbench, periods=("2602",))

    assert result.artifacts_written == 1
    assert result.rows_written == 1
    assert result.current_revisions == 1
    with DuckDBMemory(database_path) as memory:
        memory.execute(
            """
            UPDATE input_revision
            SET source_kind = 'order'
            """
        )
        memory.execute(
            """
            UPDATE input_revision_state
            SET approved_by = 'manual-reviewer',
                reason = '已核对原始订单文件',
                status = 'current'
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, run_kind, status,
                input_manifest_sha256, finished_at
            )
            VALUES ('parse-2', 'contract-1', 'parse', 'succeeded',
                    'input-sha', current_timestamp + INTERVAL 1 SECOND)
            """
        )
    rerun = normalize_workspace(workbench, periods=("2602",))
    assert rerun.artifacts_written == 0
    assert rerun.current_revisions == 1
    with DuckDBMemory(database_path) as memory:
        assert memory.execute(
            "SELECT count(*) FROM normalized_artifact"
        ).fetchone() == (1,)
        assert memory.execute(
            "SELECT source_kind FROM input_revision"
        ).fetchone() == ("baobei_order",)
        assert memory.execute(
            """
            SELECT status, reason, approved_by
            FROM input_revision_state
            """
        ).fetchone() == (
            "current",
            "已核对原始订单文件",
            "manual-reviewer",
        )
        memory.execute(
            """
            UPDATE normalized_artifact
            SET rule_version = 'finite-normalization-v1'
            """
        )

    upgraded = normalize_workspace(workbench, periods=("2602",))

    assert upgraded.artifacts_written == 1
    with DuckDBMemory(database_path) as memory:
        assert memory.execute(
            """
            SELECT rule_version, count(*)
            FROM normalized_artifact
            GROUP BY rule_version
            ORDER BY rule_version
            """
        ).fetchall() == [
            ("finite-normalization-v1", 1),
            (NORMALIZATION_RULE_VERSION, 1),
        ]
        current_rows, manifest = _current_canonical_rows(
            memory,
            period_id="period-1",
        )
    assert len(current_rows) == 1
    assert len(manifest) == 1

    reconciliation = reconcile_period(workbench, period_token="2602")

    assert reconciliation.item_count == 1
    assert reconciliation.balance_count == 1
    assert reconciliation.unresolved_count == 1
    assert reconciliation.certifiable is False
    with DuckDBMemory(database_path) as memory:
        assert memory.execute(
            "SELECT count(*) FROM reconciliation_item"
        ).fetchone() == (1,)


def test_certified_profit_is_not_invented_when_components_are_missing() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
            VALUES (
                'contract-profit', 'logical-profit', 'enterprise-profit',
                'store-profit', 'taobao', 1, DATE '2026-02-01', 'active',
                '{}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status
            )
            VALUES (
                'period-profit', 'contract-profit', 'store-profit',
                DATE '2026-02-01', DATE '2026-02-28', 'open'
            )
            """
        )
        for run_id in ("run-incomplete", "run-complete"):
            memory.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, period_id, run_kind, status
                )
                VALUES (?, 'contract-profit', 'period-profit',
                        'reconcile', 'succeeded')
                """,
                [run_id],
            )

        order_row = {
            "metric": "net_order_amount",
            "amount": Decimal("90.0000"),
            "attributes_json": json.dumps(
                {
                    "gross_paid_amount": "100.0000",
                    "refund_amount": "10.0000",
                }
            ),
            "evidence_file_id": "orders.xlsx",
            "evidence_row_no": 2,
        }
        incomplete = _insert_certified_pnl(
            memory,
            run_id="run-incomplete",
            period_id="period-profit",
            store_id="store-profit",
            rows=[order_row],
            trust_tier="certified",
        )
        assert incomplete["complete"] is False
        assert "cost" in incomplete["missing_components"]
        assert memory.execute(
            """
            SELECT count(*) FROM pnl_cell
            WHERE run_id = 'run-incomplete' AND metric = 'profit'
            """
        ).fetchone() == (0,)

        direct_rows = [
            {
                "metric": metric,
                "amount": Decimal(value),
                "attributes_json": "{}",
                "evidence_file_id": f"{metric}.xlsx",
                "evidence_row_no": 2,
            }
            for metric, value in (
                ("platform_fee", "-5.0000"),
                ("freight", "-4.0000"),
                ("cost", "-30.0000"),
                ("advertising", "-6.0000"),
            )
        ]
        complete = _insert_certified_pnl(
            memory,
            run_id="run-complete",
            period_id="period-profit",
            store_id="store-profit",
            rows=[order_row, *direct_rows],
            trust_tier="certified",
        )
        assert complete["complete"] is True
        assert complete["missing_components"] == []
        assert memory.execute(
            """
            SELECT value FROM pnl_cell
            WHERE run_id = 'run-complete' AND metric = 'profit'
            """
        ).fetchone() == (Decimal("45.0000"),)


def test_certified_pnl_publishes_product_cells_only_from_direct_evidence() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from, status,
                definition_json
            )
            VALUES (
                'contract-product-profit', 'contract-product-profit',
                'enterprise-product-profit', 'store-product-profit',
                'taobao', 1, DATE '2026-02-01', 'active', '{}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end,
                status
            )
            VALUES (
                'period-product-profit', 'contract-product-profit',
                'store-product-profit', DATE '2026-02-01',
                DATE '2026-02-28', 'open'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status
            )
            VALUES (
                'run-product-profit', 'contract-product-profit',
                'period-product-profit', 'reconcile', 'succeeded'
            )
            """
        )
        order_row = {
            "metric": "net_order_amount",
            "business_key": "ORDER-1",
            "sku": "SKU-1",
            "amount": Decimal("90.0000"),
            "attributes_json": json.dumps(
                {
                    "gross_paid_amount": "100.0000",
                    "refund_amount": "10.0000",
                }
            ),
            "evidence_file_id": "snapshot-orders",
            "evidence_row_no": 2,
        }
        direct_rows = [
            {
                "metric": metric,
                "business_key": (
                    "ORDER-1"
                    if metric in {"platform_fee", "cost"}
                    else metric
                ),
                "sku": (
                    None
                    if metric == "platform_fee"
                    else "INTERNAL-COST-CODE"
                    if metric == "cost"
                    else "SKU-1"
                ),
                "amount": Decimal(value),
                "attributes_json": "{}",
                "evidence_file_id": f"snapshot-{metric}",
                "evidence_row_no": 2,
            }
            for metric, value in (
                ("platform_fee", "-5.0000"),
                ("freight", "-4.0000"),
                ("cost", "-30.0000"),
                ("advertising", "-6.0000"),
            )
        ]

        result = _insert_certified_pnl(
            memory,
            run_id="run-product-profit",
            period_id="period-product-profit",
            store_id="store-product-profit",
            rows=[order_row, *direct_rows],
            trust_tier="certified",
        )

        assert result["product_count"] == 1
        assert result["complete_product_count"] == 1
        assert result["unassigned_product_totals"] == {}
        assert result["performance_ready"] is True
        product_cells = memory.execute(
            """
            SELECT metric, value, definition_id
            FROM pnl_cell
            WHERE run_id = 'run-product-profit' AND sku_key = 'SKU-1'
            ORDER BY metric
            """
        ).fetchall()
        assert product_cells == [
            ("advertising", Decimal("-6.0000"), "pnl-certified-product-direct-v1"),
            ("cost", Decimal("-30.0000"), "pnl-certified-product-direct-v1"),
            ("freight", Decimal("-4.0000"), "pnl-certified-product-direct-v1"),
            ("platform_fee", Decimal("-5.0000"), "pnl-certified-product-direct-v1"),
            ("profit", Decimal("45.0000"), "pnl-certified-product-direct-v1"),
            ("refund", Decimal("-10.0000"), "pnl-certified-product-direct-v1"),
            ("sales", Decimal("100.0000"), "pnl-certified-product-direct-v1"),
        ]


def test_certified_pnl_never_invents_first_source_row() -> None:
    with DuckDBMemory() as memory:
        memory.initialize()
        with pytest.raises(RuntimeError, match="缺少可定位"):
            _insert_certified_pnl(
                memory,
                run_id="run-missing-evidence",
                period_id="period-missing-evidence",
                store_id="store-missing-evidence",
                rows=[
                    {
                        "metric": "platform_fee",
                        "amount": Decimal("-1.0000"),
                        "attributes_json": "{}",
                        "evidence_file_id": "snapshot-fee",
                        "evidence_row_no": None,
                    }
                ],
                trust_tier="certified",
            )
