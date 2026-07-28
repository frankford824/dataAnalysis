import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from commerce_harness.api import (
    _compute_error_business_copy,
    _review_business_copy,
    create_app,
)
from commerce_harness.bootstrap import stable_identity
from commerce_harness.config import load_config
from commerce_harness.judgment.models import GatewayResult
from commerce_harness.llm_runtime import ModelDiscoveryResult, RuntimeLlmStore
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.snapshot import BytesReader, SnapshotStore
from commerce_harness.workbench import initialize


def test_empty_workspace_is_truthful(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()

    client = TestClient(create_app(config))
    assert client.get("/healthz").text == "ok\n"
    assert client.get("/readyz").text == "ready\n"
    status = client.get("/api/v1/status").json()
    progress = client.get("/api/v1/progress").json()

    assert status["mode"] == "empty"
    assert status["llmConfigured"] is False
    assert status["reconciliationMode"] == "platform_wallet"
    assert status["bankCashStatus"] == "not_applicable"
    assert progress["sourceCount"] == 0
    assert progress["unresolvedCount"] == 0
    assert client.get("/api/v1/balances").json() == []
    assert client.get("/api/v1/reviews").json() == []

    paths = (
        "/api/v1/status",
        "/api/v1/progress",
        "/api/v1/balances",
        "/api/v1/reviews",
        "/api/v1/business-decisions",
    )
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        responses = list(pool.map(client.get, paths))
    assert [response.status_code for response in responses] == [200] * len(paths)


def test_compute_error_copy_does_not_leak_internal_details() -> None:
    visible = _compute_error_business_copy(
        "Binder Error: SELECT secret FROM /workbench/private/ledger.duckdb"
    )

    assert visible is not None
    assert "故障编号" in visible
    assert "/workbench" not in visible
    assert "SELECT secret" not in visible


def test_review_evidence_preview_is_bound_to_exact_snapshot_and_row(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    manifest = SnapshotStore(workbench.snapshots).capture(
        BytesReader("订单号,店铺,金额\nA-1,一店,10.25\nA-2,一店,20.00\n".encode()),
        original_name="三月订单.csv",
        media_type="text/csv",
    )
    enterprise_id = stable_identity("enterprise", "local-enterprise")
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            ) VALUES (
                'contract-evidence', 'store-evidence', ?, '一店', 'taobao',
                1, DATE '2026-03-01', 'active', '{}'
            )
            """,
            [enterprise_id],
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            ) VALUES (
                'period-evidence', 'contract-evidence', '一店',
                DATE '2026-03-01', DATE '2026-03-31', 'open'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status, finished_at
            ) VALUES (
                'run-evidence', 'contract-evidence', 'period-evidence',
                'reconcile', 'succeeded', current_timestamp
            )
            """
        )
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, media_type, captured_at, manifest_json
            ) VALUES (?, ?, ?, ?, 'finance-win-ro://orders.csv',
                      '三月订单.csv', 'text/csv', current_timestamp, '{}')
            """,
            [
                manifest.snapshot_id,
                manifest.content_sha256,
                manifest.byte_size,
                manifest.object_path,
            ],
        )
        memory.execute(
            """
            INSERT INTO evidence_record (
                evidence_id, run_id, snapshot_id, evidence_kind,
                payload_json, payload_sha256
            ) VALUES (
                'evidence-preview', 'run-evidence', ?, 'source_row',
                '[]', repeat('a', 64)
            )
            """,
            [manifest.snapshot_id],
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount, difference_amount,
                status, evidence_json
            ) VALUES (
                'balance-preview', 'run-evidence', 'contract-evidence',
                'period-evidence', 'A-2', 20, 0, 0, 20, 'unresolved', '[]'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status, evidence_id
            ) VALUES (
                'review-preview', 'balance-preview', 'missing_side',
                20, 'open', 'evidence-preview'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, row_no,
                field, source_value, normalization_version
                ) VALUES (
                    'binding-preview', 'evidence-preview', 0, ?, 3,
                    '金额', '20.00', 'finite-normalization-v5'
                )
            """,
            [manifest.snapshot_id],
        )

    client = TestClient(create_app(config))
    response = client.get(
        f"/api/v1/reviews/review-preview/evidence/{manifest.snapshot_id}/preview",
        params={"radius": 1, "maxColumns": 10},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["originalName"] == "三月订单.csv"
    assert body["sheet"]["window"]["targetRowNumber"] == 3
    assert body["sheet"]["window"]["targetColumnIndex"] == 2
    assert body["sheet"]["window"]["rows"][-1]["cells"][2]["value"] == "20.00"
    assert body["context"]["storeName"] == "一店"
    assert body["comparison"]["differenceAmount"] == "20.0000"
    assert (
        client.get("/api/v1/reviews/review-preview/evidence/not-bound/preview").status_code == 404
    )


def test_analytics_catalog_uses_authoritative_target_stores(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    enterprise_id = stable_identity("enterprise", "local-enterprise")
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        for suffix, store_name in (("1", "淘宝一店"), ("2", "PDD二店")):
            platform = "taobao" if suffix == "1" else "pinduoduo"
            memory.execute(
                """
                INSERT INTO reconciliation_contract (
                    contract_id, logical_key, enterprise_id, store_id,
                    platform_code, contract_version, effective_from, status,
                    definition_json
                )
                VALUES (?, ?, ?, ?, ?, 1, DATE '2026-02-01', 'active', ?)
                """,
                [
                    f"contract-{suffix}",
                    f"logical-{suffix}",
                    enterprise_id,
                    f"store-{suffix}",
                    platform,
                    json.dumps({"store_name": store_name}),
                ],
            )
    (workbench.reports / "source-inventory.json").write_text(
        json.dumps({"records": [], "candidate_source_ids": []}),
        encoding="utf-8",
    )
    (workbench.reports / "target-plan.json").write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "platform": "taobao",
                        "logical_store": "淘宝一店",
                        "period": "2026-02",
                        "source_ids": ["orders-1"],
                    },
                    {
                        "platform": "pinduoduo",
                        "logical_store": "PDD二店",
                        "period": "2026-02",
                        "source_ids": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    response = TestClient(create_app(config)).get("/api/v1/analytics/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["discoveredStoreCount"] == 2
    assert body["processedStoreCount"] == 1
    assert {
        (store["name"], store["platformId"], tuple(store["periods"])) for store in body["stores"]
    } == {
        ("淘宝一店", "taobao", ("2026-02",)),
        ("PDD二店", "pinduoduo", ("2026-02",)),
    }


def test_readiness_rejects_missing_frontend_bundle(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()

    client = TestClient(create_app(config, tmp_path / "missing-web-dist"))

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["detail"] == "前端产物未就绪"


def test_input_revision_selection_is_scoped_persistent_and_path_safe(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-03-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES
                ('period-1', 'contract-1', 'store-1',
                 DATE '2026-03-01', DATE '2026-03-31', 'open'),
                ('period-closed', 'contract-1', 'store-1',
                 DATE '2026-04-01', DATE '2026-04-30', 'closed')
            """
        )
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES
                ('wx-raw', repeat('a', 64), 10, '/safe/wx-raw',
                 'finance-win-ro://D:\\Private\\原始数据\\2603微信原始.xlsx',
                 '2603微信原始.xlsx', current_timestamp, '{}'),
                ('wx-processed', repeat('b', 64), 20, '/safe/wx-processed',
                 'finance-win-ro://D:\\Private\\修改后数据\\2603微信加工.xlsx',
                 '2603微信加工.xlsx', current_timestamp, '{}'),
                ('ali-current', repeat('c', 64), 30, '/safe/alipay',
                 'finance-win-ro://D:\\Private\\Wallet\\2603支付宝.xlsx',
                 '2603支付宝.xlsx', current_timestamp, '{}'),
                ('wx-closed', repeat('f', 64), 40, '/safe/wx-closed',
                 'finance-win-ro://D:\\Private\\Wallet\\2604微信.xlsx',
                 '2604微信.xlsx', current_timestamp, '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO input_revision (
                revision_id, contract_id, period_id, source_kind,
                logical_input_key, revision_no, snapshot_id, status, reason
            )
            VALUES
                ('revision-wx-raw', 'contract-1', 'period-1', 'wechat_ledger',
                 'platform_ledger:2603', 1, 'wx-raw', 'candidate',
                 '同一输入流存在多个内容版本'),
                ('revision-wx-processed', 'contract-1', 'period-1',
                 'wechat_ledger', 'platform_ledger:2603', 2, 'wx-processed',
                 'candidate', '同一输入流存在多个内容版本'),
                ('revision-ali', 'contract-1', 'period-1', 'alipay_ledger',
                 'platform_ledger:2603', 3, 'ali-current', 'current',
                 '唯一内容版本'),
                ('revision-wx-closed', 'contract-1', 'period-closed',
                 'wechat_ledger', 'platform_ledger:2604', 1, 'wx-closed',
                 'candidate', '关闭账期候选')
            """
        )
        memory.execute(
            """
            INSERT INTO input_revision_state (
                revision_id, status, reason, approved_by
            )
            VALUES
                ('revision-wx-raw', 'candidate', '需要人工选择', NULL),
                ('revision-wx-processed', 'candidate', '需要人工选择', NULL),
                ('revision-ali', 'current', '唯一内容版本', NULL),
                ('revision-wx-closed', 'candidate', '关闭账期候选', NULL)
            """
        )
        memory.execute(
            """
            INSERT INTO normalized_artifact (
                artifact_id, input_revision_id, content_sha256,
                source_snapshot_id, dataset_kind, schema_version, rule_version,
                row_count, byte_size, parquet_uri, partition_json,
                arrow_schema, created_at
            )
            VALUES
                ('artifact-wx-raw', 'revision-wx-raw', repeat('d', 64),
                 'wx-raw', 'platform_ledger', 'canonical-v1',
                 'finite-normalization-v5', 1608, 100, '/safe/a.parquet',
                 '{"period":"2603"}', 'schema', current_timestamp),
                ('artifact-wx-processed', 'revision-wx-processed',
                 repeat('e', 64), 'wx-processed', 'platform_ledger',
                 'canonical-v1', 'finite-normalization-v5', 1607, 100,
                 '/safe/b.parquet', '{"period":"2603"}', 'schema',
                 current_timestamp)
                """
            )
    client = TestClient(create_app(config))
    response = client.get("/api/v1/input-revisions")

    assert response.status_code == 200
    assert "D:\\\\Private" not in response.text
    assert "finance-win-ro://" not in response.text
    groups = response.json()
    assert len(groups) == 1
    assert len(groups[0]["groupId"]) == 24
    assert groups[0]["period"] == "2026-03"
    assert groups[0]["sourceKind"] == "wechat_ledger"
    assert groups[0]["label"] == "微信流水"
    assert {
        (
            item["revisionId"],
            item["originalName"],
            item["sourceLabel"],
            item["rowCount"],
        )
        for item in groups[0]["candidates"]
    } == {
        (
            "revision-wx-raw",
            "2603微信原始.xlsx",
            "原始数据目录",
            1608,
        ),
        (
            "revision-wx-processed",
            "2603微信加工.xlsx",
            "历史加工目录",
            1607,
        ),
    }

    selected = client.post(
        "/api/v1/input-revisions/revision-wx-raw/select",
        json={"reason": "原始导出文件不含人工公式"},
    )
    assert selected.status_code == 204
    assert (
        client.post(
            "/api/v1/input-revisions/revision-wx-raw/select",
            json={"reason": "幂等重试不覆盖首次理由"},
        ).status_code
        == 204
    )
    assert client.get("/api/v1/input-revisions").json() == []
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT revision_id, status, approved_by
            FROM input_revision_state
            ORDER BY revision_id
            """
        ).fetchall() == [
            ("revision-ali", "current", None),
            ("revision-wx-closed", "candidate", None),
            ("revision-wx-processed", "superseded", "local_business_owner"),
            ("revision-wx-raw", "current", "local_business_owner"),
        ]
        assert memory.execute(
            """
            SELECT reason
            FROM input_revision_state
            WHERE revision_id = 'revision-wx-raw'
            """
        ).fetchone() == ("原始导出文件不含人工公式",)

    closed = client.post(
        "/api/v1/input-revisions/revision-wx-closed/select",
        json={"reason": "关闭账期后不允许更换"},
    )
    assert closed.status_code == 409
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute(
            """
            SELECT status
            FROM input_revision_state
            WHERE revision_id = 'revision-wx-raw'
            """
        ).fetchone() == ("current",)


def test_review_decision_is_append_only_and_does_not_change_amount(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('period-1', 'contract-1', 'store-1',
                    DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (run_id, contract_id, period_id, run_kind, status)
            VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded')
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount, difference_amount, status
            )
            VALUES ('balance-1', 'run-1', 'contract-1', 'period-1', 'order-1',
                    10.0000, 9.0000, 9.0000, -1.0000, 'unresolved')
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            )
            VALUES ('unresolved-1', 'balance-1', 'amount_mismatch', 1.0000, 'open')
            """
        )

    client = TestClient(create_app(config))
    response = client.post(
        "/api/v1/reviews/unresolved-1",
        json={"decision": "explain", "reason": "平台手续费跨期入账"},
    )

    assert response.status_code == 204
    with DuckDBMemory(workbench.database) as memory:
        unresolved = memory.execute(
            """
            SELECT amount, status, explanation
            FROM unresolved_balance WHERE unresolved_id = 'unresolved-1'
            """
        ).fetchone()
        decisions = memory.execute(
            """
            SELECT decision, reason, decided_by
            FROM review_decision WHERE unresolved_id = 'unresolved-1'
            """
        ).fetchall()
    assert unresolved is not None
    assert str(unresolved[0]) == "1.0000"
    assert unresolved[1:] == ("open", None)
    assert decisions == [("explain", "平台手续费跨期入账", "local_operator")]
    assert client.get("/api/v1/reviews").json() == []


def test_review_evidence_returns_frozen_file_sheet_and_row(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES (
                'snapshot-1', 'source-sha', 123, '/objects/source-sha',
                'D:\\finance\\wallet\\ledger.xlsx', 'ledger.xlsx',
                current_timestamp, '{}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('period-1', 'contract-1', 'store-1',
                    DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status,
                started_at, finished_at
            )
            VALUES (
                'run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded',
                current_timestamp, current_timestamp
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_record (
                evidence_id, run_id, snapshot_id, source_locator,
                source_row_key, evidence_kind, payload_json, payload_sha256
            )
            VALUES (
                'evidence-1', 'run-1', 'snapshot-1', 'snapshot-1',
                'snapshot-1:56667', 'reconciliation_balance',
                '[{"file_id":"snapshot-1","row_no":56667,"field":"platform_net_amount","rule_version":"finite-normalization-v5","source_value":"-0.6000"}]',
                'payload-sha'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
                row_no, field, source_value, normalization_version
            )
            VALUES (
                'binding-1', 'evidence-1', 0, 'snapshot-1', '账务明细',
                56667, 'platform_net_amount', '-0.6000',
                'finite-normalization-v5'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount, difference_amount,
                status
            )
            VALUES (
                'balance-1', 'run-1', 'contract-1', 'period-1', 'order-1',
                1.0000, 0.4000, 0.4000, -0.6000, 'unresolved'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status, evidence_id
            )
            VALUES (
                'unresolved-1', 'balance-1', 'amount_mismatch', 0.6000,
                'open', 'evidence-1'
            )
            """
        )

    client = TestClient(create_app(config))
    response = client.get("/api/v1/reviews/unresolved-1/evidence")

    assert response.status_code == 200
    assert response.json() == {
        "unresolvedId": "unresolved-1",
        "balanceId": "balance-1",
        "lineageStatus": "frozen",
        "sources": [
            {
                "snapshotId": "snapshot-1",
                "artifactId": None,
                "originalName": "ledger.xlsx",
                "sourceMember": None,
                "sourceSheet": "账务明细",
                "rowNumber": 56667,
                "field": "platform_net_amount",
                "normalizedValue": "-0.6000",
                "normalizationVersion": "finite-normalization-v5",
                "ruleVersionId": None,
                "sourceKind": None,
            }
        ],
    }
    reviews = client.get("/api/v1/reviews").json()
    assert reviews[0]["evidenceCount"] == 1
    assert "D:\\finance" not in response.text
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
                row_no, field, source_value, normalization_version
            )
            VALUES (
                'binding-old-mixed', 'evidence-1', 1, 'snapshot-1',
                '账务明细', 56668, 'platform_net_amount', '-0.1000',
                'finite-normalization-v4'
            )
            """
        )
    mixed_binding = client.get("/api/v1/reviews/unresolved-1/evidence")
    assert mixed_binding.json()["lineageStatus"] == "unavailable"
    assert mixed_binding.json()["sources"] == []
    mixed_preview = client.get(
        "/api/v1/reviews/unresolved-1/evidence/snapshot-1/preview"
    )
    assert mixed_preview.status_code == 409
    assert "暂不能作为完整证据打开" in mixed_preview.json()["detail"]
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            "DELETE FROM evidence_binding WHERE binding_id = 'binding-old-mixed'"
        )
        memory.execute(
            "UPDATE evidence_binding SET source_sheet = NULL WHERE binding_id = 'binding-1'"
        )
    invalid_binding = client.get("/api/v1/reviews/unresolved-1/evidence")
    assert invalid_binding.status_code == 200
    assert invalid_binding.json()["lineageStatus"] == "unavailable"
    assert invalid_binding.json()["sources"] == []
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            UPDATE evidence_binding
            SET source_sheet = '账务明细',
                normalization_version = 'finite-normalization-v4'
            WHERE binding_id = 'binding-1'
            """
        )
    obsolete_binding = client.get("/api/v1/reviews/unresolved-1/evidence")
    assert obsolete_binding.status_code == 200
    assert obsolete_binding.json()["lineageStatus"] == "unavailable"
    assert obsolete_binding.json()["sources"] == []
    obsolete_preview = client.get(
        "/api/v1/reviews/unresolved-1/evidence/snapshot-1/preview"
    )
    assert obsolete_preview.status_code == 409
    assert "暂不能作为完整证据打开" in obsolete_preview.json()["detail"]


def test_review_business_copy_hides_kernel_json() -> None:
    raw = (
        '{"bridge_ids":[],"cash_bridge_keys":[],"missing_sides":["order"],'
        '"rule_versions":["taobao-order-platform-key-v1"],'
        '"scope":"order_platform","settlement_batch_keys":[]}'
    )

    title, summary, action = _review_business_copy("missing_side", raw)

    assert title == "平台钱包有记录，订单明细未找到"
    assert "本月订单文件" in summary
    assert "退款" in action
    assert "bridge_ids" not in f"{title}{summary}{action}"
    assert "rule_versions" not in f"{title}{summary}{action}"


def test_llm_test_and_review_suggestion_are_advisory_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    RuntimeLlmStore(workbench.root).save(
        protocol="openai_compatible",
        base_url="https://models.example/v1",
        api_key="runtime-test-key",
        selected_model="fake-model",
        reviewer_model="reviewer-model",
    )
    purposes: list[str] = []

    class FakeGateway:
        def complete_json(self, *, purpose, model, messages):
            del model, messages
            purposes.append(purpose)
            if purpose == "review_explanation_suggestion":
                content = {
                    "suggestion": "建议先核对退款状态和交易日期，再确认是否跨月到账。",
                    "citations": [{"snapshotId": "snapshot-llm", "rowNumber": 42}],
                }
                result_model = "fake-model"
            elif purpose == "review_explanation_review":
                content = {
                    "agree": True,
                    "reason": "看起来一致",
                    "citations": [{"snapshotId": "snapshot-llm", "rowNumber": 42}],
                }
                result_model = "reviewer-model"
            else:
                content = {"status": "ok"}
                result_model = "fake-model"
            return GatewayResult(
                status="ok",
                model=result_model,
                content=content,
                request_id=f"request-{purpose}",
            )

    monkeypatch.setattr(
        "commerce_harness.api.OpenAICompatibleGateway",
        lambda **kwargs: FakeGateway(),
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('period-1', 'contract-1', 'store-1',
                    DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (run_id, contract_id, period_id, run_kind, status)
            VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded')
            """
        )
        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES (
                'snapshot-llm', repeat('a', 64), 123, '/objects/source-llm',
                'finance-win-ro://wallet.xlsx', '钱包流水.xlsx',
                current_timestamp, '{}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_record (
                evidence_id, run_id, snapshot_id, evidence_kind,
                payload_json, payload_sha256
            )
            VALUES (
                'evidence-llm', 'run-1', 'snapshot-llm', 'source_row',
                '[{"file_id":"snapshot-llm","row_no":42,"field":"交易金额","source_value":"10.0000"}]',
                repeat('b', 64)
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
                row_no, field, source_value, normalization_version
            )
            VALUES (
                'binding-llm', 'evidence-llm', 0, 'snapshot-llm', '钱包流水',
                42, '交易金额', '10.0000', 'finite-normalization-v5'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount, difference_amount, status
            )
            VALUES ('balance-1', 'run-1', 'contract-1', 'period-1',
                    'order_platform:test', 0.0000, 10.0000, 0.0000, 10.0000,
                    'unresolved')
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status,
                explanation, evidence_id
            )
            VALUES (
                'unresolved-1', 'balance-1', 'missing_side', 10.0000, 'open',
                '{"missing_sides":["order"],"scope":"order_platform"}',
                'evidence-llm'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_record (
                evidence_id, run_id, snapshot_id, evidence_kind,
                payload_json, payload_sha256
            )
            VALUES (
                'evidence-legacy', 'run-1', 'snapshot-llm', 'source_row',
                '[{"file_id":"snapshot-llm","row_no":42,"field":"交易金额"}]',
                repeat('c', 64)
            )
            """
        )
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
                row_no, field, source_value, normalization_version
            )
            VALUES (
                'binding-legacy-v4', 'evidence-legacy', 0, 'snapshot-llm',
                '钱包流水', 42, '交易金额', '10.0000',
                'finite-normalization-v4'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount,
                difference_amount, status
            )
            VALUES (
                'balance-legacy', 'run-1', 'contract-1', 'period-1',
                'order_platform:legacy', 0.0000, 10.0000, 0.0000,
                10.0000, 'unresolved'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status,
                explanation, evidence_id
            )
            VALUES (
                'unresolved-legacy', 'balance-legacy', 'missing_side',
                10.0000, 'open',
                '{"missing_sides":["order"],"scope":"order_platform"}',
                'evidence-legacy'
            )
            """
        )
        memory.execute(
            """
            UPDATE reconciliation_contract
            SET enterprise_id = ?
            WHERE contract_id = 'contract-1'
            """,
            [stable_identity("enterprise", "local-enterprise")],
        )

    client = TestClient(create_app(config))
    connection = client.post("/api/v1/llm/test")
    suggestion = client.post("/api/v1/reviews/unresolved-1/suggestion")
    legacy_suggestion = client.post(
        "/api/v1/reviews/unresolved-legacy/suggestion"
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            """
            INSERT INTO evidence_binding (
                binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
                row_no, field, source_value, normalization_version
            )
            VALUES (
                'binding-mixed-v4', 'evidence-llm', 1, 'snapshot-llm',
                '钱包流水', 43, '交易金额', '1.0000',
                'finite-normalization-v4'
            )
            """
        )
    mixed_suggestion = client.post(
        "/api/v1/reviews/unresolved-1/suggestion"
    )
    with DuckDBMemory(workbench.database) as memory:
        memory.execute(
            "DELETE FROM evidence_binding WHERE binding_id = 'binding-mixed-v4'"
        )
    visible = client.get("/api/v1/reviews").json()[0]

    assert connection.json()["status"] == "ok"
    assert suggestion.status_code == 200
    assert suggestion.json()["mayWriteLedger"] is False
    assert suggestion.json()["requiresHumanReview"] is True
    assert suggestion.json()["suggestionId"].startswith("suggestion_")
    assert suggestion.json()["evidenceGuard"] == "passed"
    assert suggestion.json()["reviewerModel"] == "reviewer-model"
    assert suggestion.json()["reviewerStatus"] == "passed"
    assert "退款状态" in suggestion.json()["suggestion"]
    assert legacy_suggestion.status_code == 409
    assert "版本过期" in legacy_suggestion.json()["detail"]
    assert mixed_suggestion.status_code == 409
    assert visible["businessTitle"] == "平台钱包有记录，订单明细未找到"
    assert "missing_sides" not in str(visible)
    decision = client.post(
        "/api/v1/reviews/unresolved-1",
        json={
            "decision": "explain",
            "reason": "人工核对后确认该记录属于跨月结算。",
        },
    )
    assert decision.status_code == 204
    remaining = client.get("/api/v1/reviews").json()
    assert [item["unresolvedId"] for item in remaining] == ["unresolved-legacy"]
    evidence_after_decision = client.get(
        "/api/v1/reviews/unresolved-1/evidence"
    )
    assert evidence_after_decision.status_code == 200
    assert evidence_after_decision.json()["lineageStatus"] == "frozen"
    assert evidence_after_decision.json()["sources"][0]["rowNumber"] == 42
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["effectiveLevel"] == "L0"
    learning = capabilities["learning"]
    assert learning["suggestionCount"] == 1
    assert learning["reviewedCount"] == 1
    assert learning["correctionCount"] == 1
    assert learning["evidenceGuardedCount"] == 1
    assert learning["promotionEligible"] is False
    assert "至少需要 20 条已复核样本" in learning["promotionReason"]
    assert "至少需要覆盖 2 个账期" in learning["promotionReason"]
    assert learning["latestEvaluation"]["proposedLevel"] == "L0"
    assert learning["latestEvaluation"]["metrics"]["sample_count"] == 1
    assert all(task["mayWriteLedger"] is False for task in capabilities["tasks"])
    assert capabilities["orchestration"]["independentReviewerConfigured"] is True
    assert purposes == [
        "connection_test",
        "review_explanation_suggestion",
        "review_explanation_review",
    ]
    with DuckDBMemory(workbench.database) as memory:
        assert memory.execute("SELECT count(*) FROM residual_suggestion").fetchone() == (1,)
        assert memory.execute("SELECT count(*) FROM correction").fetchone() == (1,)


def test_business_decision_records_append_only_event(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO business_decision (
                decision_id, contract_id, subject_kind, question,
                business_impact, status
            )
            VALUES ('decision-1', 'contract-1', 'freight_period_attribution',
                    '按哪个日期归属？', '未确认不能计算运费', 'pending')
            """
        )

    client = TestClient(create_app(config))
    csv_export = client.get("/api/v1/reviews.csv")
    assert csv_export.status_code == 200
    assert "decision-1" in csv_export.content.decode("utf-8-sig")
    response = client.post(
        "/api/v1/business-decisions/decision-1",
        json={"answer": "按物流签收日期归属，跨月进入签收月。"},
    )

    assert response.status_code == 204
    current = client.get("/api/v1/business-decisions").json()[0]
    assert current["status"] == "decided"
    assert current["answer"] == "按物流签收日期归属，跨月进入签收月。"
    assert (
        client.post(
            "/api/v1/business-decisions/decision-1",
            json={"answer": "尝试覆盖"},
        ).status_code
        == 409
    )
    with DuckDBMemory(workbench.database) as memory:
        event = memory.execute(
            """
            SELECT action, actor, payload_json
            FROM business_decision_event WHERE decision_id = 'decision-1'
            """
        ).fetchone()
    assert event is not None
    assert event[0:2] == ("decide", "local_business_owner")
    assert "物流签收日期" in str(event[2])


def test_business_decisions_deduplicate_identical_contract_policies(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        for suffix in ("1", "2"):
            memory.execute(
                """
                INSERT INTO reconciliation_contract (
                    contract_id, logical_key, enterprise_id, store_id,
                    platform_code, contract_version, effective_from, status,
                    definition_json
                )
                VALUES (?, ?, 'enterprise-1', ?, 'taobao', 1,
                        DATE '2026-02-01', 'active', '{}')
                """,
                [f"contract-{suffix}", f"taobao-{suffix}", f"store-{suffix}"],
            )
            memory.execute(
                """
                INSERT INTO business_decision (
                    decision_id, contract_id, subject_kind, question,
                    business_impact, status, decision_json, decided_by,
                    decided_at
                )
                VALUES (?, ?, 'shared_cost_attribution',
                        '共享成本如何分配？',
                        '避免无依据平均分摊或在零销售时制造金额。',
                        'decided',
                        '{"answer":"direct_then_positive_net_sales_share"}',
                        'policy:conservative-v1', current_timestamp)
                """,
                [f"decision-{suffix}", f"contract-{suffix}"],
            )

    client = TestClient(create_app(config))

    decisions = client.get("/api/v1/business-decisions")
    progress = client.get("/api/v1/progress")

    assert decisions.status_code == 200
    assert len(decisions.json()) == 1
    assert decisions.json()[0]["subjectKind"] == "shared_cost_attribution"
    assert progress.status_code == 200
    rules_gate = next(gate for gate in progress.json()["gates"] if gate["id"] == "rules")
    assert "1 项系统口径" in rules_gate["detail"]


def test_progress_and_reviews_only_use_latest_certification_state(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active',
                    '{"store_name": "测试店铺"}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('period-1', 'contract-1', 'store-1',
                    DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status,
                started_at, finished_at, metrics_json
            )
            VALUES
                ('run-old', 'contract-1', 'period-1', 'reconcile', 'succeeded',
                 TIMESTAMPTZ '2026-02-28 08:00:00+00',
                 TIMESTAMPTZ '2026-02-28 08:01:00+00',
                 '{"certifiable": true}'),
                ('run-latest', 'contract-1', 'period-1', 'reconcile', 'succeeded',
                 TIMESTAMPTZ '2026-02-28 09:00:00+00',
                 TIMESTAMPTZ '2026-02-28 09:01:00+00',
                 '{"certifiable": false}')
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount, difference_amount, status
            )
            VALUES
                ('balance-old', 'run-old', 'contract-1', 'period-1', 'old-key',
                 100.0000, 90.0000, 90.0000, -10.0000, 'unresolved'),
                ('balance-latest', 'run-latest', 'contract-1', 'period-1',
                 'latest-key', 20.0000, 15.0000, 15.0000, -5.0000, 'unresolved')
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            )
            VALUES
                ('unresolved-old', 'balance-old', 'amount_mismatch', 10.0000, 'open'),
                ('unresolved-latest', 'balance-latest', 'missing_side', 5.0000, 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO baseline (
                baseline_id, contract_id, period_id, baseline_version,
                input_manifest_sha256, rule_set_sha256, code_sha, output_sha256,
                invariant_report_json, status
            )
            VALUES (
                'baseline-candidate', 'contract-1', 'period-1', 1,
                'input-sha', 'rule-sha', 'code-sha', 'output-sha', '{}', 'candidate'
            )
            """
        )

    client = TestClient(create_app(config))
    progress = client.get("/api/v1/progress").json()
    reviews = client.get("/api/v1/reviews").json()
    balances = client.get("/api/v1/balances").json()

    gate_states = {gate["id"]: gate["state"] for gate in progress["gates"]}
    assert gate_states["reconcile"] == "blocked"
    assert gate_states["baseline"] == "pending"
    assert progress["unresolvedCount"] == 1
    assert progress["unexplainedAmount"] == "5.0000"
    assert [item["unresolvedId"] for item in reviews] == ["unresolved-latest"]
    assert [item["balanceKey"] for item in balances] == ["latest-key"]


def test_progress_respects_store_and_period_scope(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        for index, (store_id, period_start, amount) in enumerate(
            (
                ("store-a", "2026-02-01", "3.0000"),
                ("store-b", "2026-03-01", "7.0000"),
            ),
            start=1,
        ):
            contract_id = f"contract-{index}"
            period_id = f"period-{index}"
            run_id = f"run-{index}"
            balance_id = f"balance-{index}"
            memory.execute(
                """
                INSERT INTO reconciliation_contract (
                    contract_id, logical_key, enterprise_id, store_id,
                    platform_code, contract_version, effective_from, status,
                    definition_json
                )
                VALUES (?, ?, 'enterprise-1', ?, 'taobao', 1,
                        DATE '2026-02-01', 'active', ?)
                """,
                [
                    contract_id,
                    f"logical-{index}",
                    store_id,
                    json.dumps({"store_name": f"店铺{index}"}, ensure_ascii=False),
                ],
            )
            memory.execute(
                """
                INSERT INTO accounting_period (
                    period_id, contract_id, store_id, period_start, period_end,
                    status
                )
                VALUES (?, ?, ?, CAST(? AS DATE),
                        last_day(CAST(? AS DATE)), 'open')
                """,
                [period_id, contract_id, store_id, period_start, period_start],
            )
            memory.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, period_id, run_kind, status,
                    finished_at, metrics_json
                )
                VALUES (?, ?, ?, 'reconcile', 'succeeded',
                        current_timestamp, '{"certifiable": false}')
                """,
                [run_id, contract_id, period_id],
            )
            memory.execute(
                """
                INSERT INTO reconciliation_balance (
                    balance_id, run_id, contract_id, period_id, balance_key,
                    expected_amount, actual_amount, matched_amount,
                    difference_amount, status
                )
                VALUES (?, ?, ?, ?, ?, 0.0000, CAST(? AS DECIMAL(20,4)),
                        0.0000, CAST(? AS DECIMAL(20,4)), 'unresolved')
                """,
                [
                    balance_id,
                    run_id,
                    contract_id,
                    period_id,
                    f"scope-{index}",
                    amount,
                    amount,
                ],
            )
            memory.execute(
                """
                INSERT INTO unresolved_balance (
                    unresolved_id, balance_id, reason_code, amount, status
                )
                VALUES (?, ?, 'missing_side', CAST(? AS DECIMAL(20,4)), 'open')
                """,
                [f"unresolved-{index}", balance_id, amount],
            )

    client = TestClient(create_app(config))
    first = client.get(
        "/api/v1/progress?storeId=store-a&period=2602"
    ).json()
    second = client.get(
        "/api/v1/progress?storeId=store-b&period=2603"
    ).json()

    assert (first["shop"], first["period"]) == ("店铺1", "2026-02")
    assert (first["unresolvedCount"], first["unexplainedAmount"]) == (1, "3.0000")
    assert (second["shop"], second["period"]) == ("店铺2", "2026-03")
    assert (second["unresolvedCount"], second["unexplainedAmount"]) == (1, "7.0000")


def test_visible_results_are_bounded_but_csv_contains_full_current_run(
    tmp_path: Path,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id, platform_code,
                contract_version, effective_from, status, definition_json
            )
            VALUES ('contract-1', 'taobao', 'enterprise-1', 'store-1', 'taobao',
                    1, DATE '2026-02-01', 'active', '{}')
            """
        )
        memory.execute(
            """
            INSERT INTO accounting_period (
                period_id, contract_id, store_id, period_start, period_end, status
            )
            VALUES ('period-1', 'contract-1', 'store-1',
                    DATE '2026-02-01', DATE '2026-02-28', 'open')
            """
        )
        memory.execute(
            """
            INSERT INTO run_log (
                run_id, contract_id, period_id, run_kind, status, metrics_json
            )
            VALUES (
                'run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded',
                '{"certifiable": false}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO reconciliation_balance (
                balance_id, run_id, contract_id, period_id, balance_key,
                expected_amount, actual_amount, matched_amount,
                difference_amount, status
            )
            SELECT
                'balance-' || cast(i AS VARCHAR),
                'run-1',
                'contract-1',
                'period-1',
                'order_platform:order-' || cast(i AS VARCHAR),
                cast(i + 1 AS DECIMAL(38,4)),
                0.0000,
                0.0000,
                cast(-(i + 1) AS DECIMAL(38,4)),
                'unresolved'
            FROM range(105) AS generated(i)
            """
        )
        memory.execute(
            """
            INSERT INTO unresolved_balance (
                unresolved_id, balance_id, reason_code, amount, status
            )
            SELECT
                'unresolved-' || cast(i AS VARCHAR),
                'balance-' || cast(i AS VARCHAR),
                'amount_mismatch',
                cast(i + 1 AS DECIMAL(38,4)),
                'open'
            FROM range(105) AS generated(i)
            """
        )

    client = TestClient(create_app(config))
    assert len(client.get("/api/v1/balances").json()) == 100
    assert len(client.get("/api/v1/reviews").json()) == 100
    page = client.get(
        "/api/v1/reviews/page",
        params={
            "storeId": "store-1",
            "period": "2602",
            "offset": 100,
            "limit": 100,
        },
    ).json()
    assert page["total"] == 105
    assert len(page["items"]) == 5
    assert page["hasMore"] is False
    assert {
        (item["storeId"], item["period"]) for item in page["items"]
    } == {("store-1", "2602")}
    grouped = client.get(
        "/api/v1/reviews/groups",
        params={"storeId": "store-1", "period": "2602"},
    ).json()
    assert grouped["groupCount"] == 1
    assert grouped["recordCount"] == 105
    assert grouped["groups"][0] == {
        "groupId": grouped["groups"][0]["groupId"],
        "storeId": "store-1",
        "storeName": "store-1",
        "period": "2602",
        "reasonCode": "amount_mismatch",
        "businessTitle": "订单与平台钱包金额不一致",
        "businessSummary": "同一笔业务在订单明细和平台钱包中的金额没有完全对上。",
        "suggestedAction": "核对退款、平台费用、优惠补贴和跨月结算，确认差额对应的真实业务事件。",
        "itemCount": 105,
        "totalAmount": "5565.0000",
        "absoluteAmount": "5565.0000",
        "evidenceCount": 0,
    }
    filtered = client.get(
        "/api/v1/reviews/page",
        params={
            "storeId": "store-1",
            "period": "2602",
            "reasonCode": "missing_side",
        },
    ).json()
    assert filtered["total"] == 0
    assert filtered["items"] == []

    csv_text = client.get("/api/v1/reviews.csv").content.decode("utf-8-sig")
    assert "unresolved-0" in csv_text
    assert "unresolved-104" in csv_text


def test_llm_runtime_api_discovers_applies_and_disables_without_exposing_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()

    calls: list[dict[str, str]] = []

    def fake_discover_models(**kwargs):
        calls.append(
            {
                "protocol": str(kwargs["protocol"]),
                "base_url": str(kwargs["base_url"]),
                "api_key": str(kwargs["api_key"]),
            }
        )
        return ModelDiscoveryResult(
            protocol="openai_compatible",
            base_url="https://models.example/v1",
            models=("model-a", "model-b"),
            completion_supported=True,
        )

    monkeypatch.setattr(
        "commerce_harness.api.discover_models",
        fake_discover_models,
    )

    def fake_complete_json(*_args, **_kwargs):
        return GatewayResult(
            status="ok",
            model="model-b",
            content={"status": "ok"},
            request_id="configuration-check",
        )

    monkeypatch.setattr(
        "commerce_harness.api.OpenAICompatibleGateway.complete_json",
        fake_complete_json,
    )
    client = TestClient(create_app(config))

    empty = client.get("/api/v1/llm/config")
    assert empty.status_code == 200
    assert empty.json()["configured"] is False
    assert empty.json()["keyConfigured"] is False

    discovered = client.post(
        "/api/v1/llm/discover",
        json={
            "protocol": "auto",
            "baseUrl": "https://models.example/v1",
            "apiKey": "browser-secret",
        },
    )
    assert discovered.status_code == 200
    assert discovered.json()["protocol"] == "openai_compatible"
    assert discovered.json()["models"] == ["model-a", "model-b"]
    assert "browser-secret" not in discovered.text

    applied = client.put(
        "/api/v1/llm/config",
        json={
            "protocol": "openai_compatible",
            "baseUrl": "https://models.example/v1",
            "apiKey": "browser-secret",
            "selectedModel": "model-b",
            "reviewerModel": "model-a",
            "enabled": True,
        },
    )
    assert applied.status_code == 200
    assert applied.json()["enabled"] is True
    assert applied.json()["selectedModel"] == "model-b"
    assert applied.json()["reviewerModel"] == "model-a"
    assert applied.json()["keyConfigured"] is True
    assert applied.json()["lastTaskStatus"] == "ok"
    assert applied.json()["lastTaskPurpose"] == "configuration_verification"
    assert "browser-secret" not in applied.text
    assert "browser-secret" not in (workbench.root / "runtime" / "llm-provider.json").read_text(
        encoding="utf-8"
    )

    status = client.get("/api/v1/status").json()
    assert status["llmEnabled"] is True
    assert status["llmConfigured"] is True

    disabled = client.delete("/api/v1/llm/config")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["configured"] is True
    assert calls == [
        {
            "protocol": "auto",
            "base_url": "https://models.example/v1",
            "api_key": "browser-secret",
        },
        {
            "protocol": "openai_compatible",
            "base_url": "https://models.example/v1",
            "api_key": "browser-secret",
        },
    ]
