from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from commerce_harness.analytics import analytics_catalog
from commerce_harness.api import create_app
from commerce_harness.bootstrap import stable_identity
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _insert_contract(
    memory: DuckDBMemory,
    contract_id: str,
    store_id: str,
    store_name: str,
    platform_code: str = "taobao",
) -> None:
    memory.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        )
        VALUES (?, ?, ?, ?, ?, 1,
                DATE '2026-01-01', 'active', ?)
        """,
        [
            contract_id,
            f"logical-{store_id}",
            stable_identity("enterprise", "local-enterprise"),
            store_id,
            platform_code,
            json.dumps({"store_name": store_name}, ensure_ascii=False),
        ],
    )


def _insert_period(
    memory: DuckDBMemory,
    period_id: str,
    contract_id: str,
    store_id: str,
    start: str,
    end: str,
) -> None:
    memory.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        )
        VALUES (?, ?, ?, cast(? AS DATE), cast(? AS DATE), 'open')
        """,
        [period_id, contract_id, store_id, start, end],
    )


def _insert_run(
    memory: DuckDBMemory,
    run_id: str,
    contract_id: str,
    period_id: str,
    finished_at: str,
    *,
    certifiable: bool,
) -> None:
    memory.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status,
            started_at, finished_at, metrics_json
        )
        VALUES (?, ?, ?, 'reconcile', 'succeeded',
                cast(? AS TIMESTAMPTZ) - INTERVAL 1 MINUTE,
                cast(? AS TIMESTAMPTZ), ?)
        """,
        [
            run_id,
            contract_id,
            period_id,
            finished_at,
            finished_at,
            json.dumps({"certifiable": certifiable}),
        ],
    )


def _insert_item(
    memory: DuckDBMemory,
    item_id: str,
    run_id: str,
    contract_id: str,
    period_id: str,
    source_kind: str,
    side: str,
    business_key: str,
    event_date: str,
    amount: str,
    attributes: dict[str, str],
) -> None:
    memory.execute(
        """
        INSERT INTO reconciliation_item (
            item_id, run_id, contract_id, period_id, source_kind,
            source_record_key, side, business_key, event_date, amount,
            attributes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, cast(? AS DATE),
                cast(? AS DECIMAL(38,4)), ?)
        """,
        [
            item_id,
            run_id,
            contract_id,
            period_id,
            source_kind,
            f"record-{item_id}",
            side,
            business_key,
            event_date,
            amount,
            json.dumps(attributes, ensure_ascii=False),
        ],
    )


def _client_with_facts(tmp_path: Path) -> tuple[TestClient, Path]:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    with DuckDBMemory(workbench.database) as memory:
        memory.initialize()
        _insert_contract(memory, "contract-a", "store-a", "甲店")
        _insert_contract(memory, "contract-b", "store-b", "乙店", "jd")
        _insert_period(
            memory,
            "period-a-feb",
            "contract-a",
            "store-a",
            "2026-02-01",
            "2026-02-28",
        )
        _insert_period(
            memory,
            "period-a-mar",
            "contract-a",
            "store-a",
            "2026-03-01",
            "2026-03-31",
        )
        _insert_period(
            memory,
            "period-b-feb",
            "contract-b",
            "store-b",
            "2026-02-01",
            "2026-02-28",
        )

        _insert_run(
            memory,
            "run-a-old",
            "contract-a",
            "period-a-feb",
            "2026-04-01T00:00:00+00:00",
            certifiable=True,
        )
        _insert_run(
            memory,
            "run-a-new",
            "contract-a",
            "period-a-feb",
            "2026-04-02T00:00:00+00:00",
            certifiable=False,
        )
        _insert_run(
            memory,
            "run-a-mar",
            "contract-a",
            "period-a-mar",
            "2026-04-03T00:00:00+00:00",
            certifiable=True,
        )
        _insert_run(
            memory,
            "run-b-new",
            "contract-b",
            "period-b-feb",
            "2026-04-04T00:00:00+00:00",
            certifiable=True,
        )

        _insert_item(
            memory,
            "old-order",
            "run-a-old",
            "contract-a",
            "period-a-feb",
            "baobei_order",
            "order",
            "old-1",
            "2026-02-01",
            "999.0000",
            {
                "business_date": "2026-02-01 01:00:00",
                "gross_paid_amount": "999.0000",
                "refund_amount": "0.0000",
            },
        )
        _insert_item(
            memory,
            "a-order",
            "run-a-new",
            "contract-a",
            "period-a-feb",
            "baobei_order",
            "order",
            "order-a",
            "2026-02-01",
            "90.0000",
            {
                "business_date": "2026-02-10 11:22:33",
                "gross_paid_amount": "100.0000",
                "refund_amount": "10.0000",
            },
        )
        _insert_item(
            memory,
            "a-wallet",
            "run-a-new",
            "contract-a",
            "period-a-feb",
            "alipay_ledger",
            "platform",
            "wallet-a",
            "2026-02-01",
            "88.0000",
            {
                "accounting_date": "2026-02-11 12:00:00",
                "business_description": "交易收款",
            },
        )
        _insert_item(
            memory,
            "a-march",
            "run-a-mar",
            "contract-a",
            "period-a-mar",
            "baobei_order",
            "order",
            "order-march",
            "2026-03-02",
            "50.0000",
            {
                "occurred_at": "2026-03-02 08:00:00",
                "gross_paid_amount": "50.0000",
                "refund_amount": "0.0000",
            },
        )
        _insert_item(
            memory,
            "b-order",
            "run-b-new",
            "contract-b",
            "period-b-feb",
            "baobei_order",
            "order",
            "order-b",
            "2026-02-12",
            "200.0000",
            {
                "gross_paid_amount": "200.0000",
                "refund_amount": "0.0000",
            },
        )

        memory.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                original_name, captured_at, manifest_json
            )
            VALUES (
                'history-snapshot', repeat('a', 64), 1, '/snapshot/history',
                'finance-win-ro://D:\\data\\history.xlsx', 'history.xlsx',
                current_timestamp, '{}'
            )
            """
        )
        memory.execute(
            """
            INSERT INTO historical_output (
                historical_output_id, contract_id, period_id, snapshot_id,
                output_kind, source_label, totals_json, status
            )
            VALUES (
                'history-a', 'contract-a', 'period-a-feb', 'history-snapshot',
                'pnl_16', '历史经营表', ?, 'competing'
            )
            """,
            [
                json.dumps(
                    {
                        "sales": {"value": "90.1234", "evidence": []},
                        "profit": {"value": "12.3400", "evidence": []},
                    },
                    ensure_ascii=False,
                )
            ],
        )
    return TestClient(create_app(config)), workbench.database


def test_overview_uses_latest_run_and_decimal_business_metrics(
    tmp_path: Path,
) -> None:
    client, _ = _client_with_facts(tmp_path)

    response = client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"] == {
        "orderGross": "350.0000",
        "refunds": "10.0000",
        "netSales": "340.0000",
        "orderCount": 3,
        "walletNet": "88.0000",
        "transactionCount": 1,
    }
    assert "999.0000" not in json.dumps(body)
    assert body["filters"]["dateRange"] == {
        "min": "2026-02-01",
        "max": "2026-03-31",
    }
    assert body["selection"] == {
        "platformId": "all",
        "storeId": "all",
        "period": "all",
        "fromDate": None,
        "toDate": None,
    }
    assert body["coverage"]["status"] == "review_required"
    assert body["coverage"]["profitStatus"] == "historical_pending"
    assert "不能作为正式经营结果" in body["coverage"]["message"]
    assert body["monthlyPnl"] == [
        {
            "period": "2026-02",
            "storeId": "store-a",
            "storeName": "甲店",
            "sourceLabel": "历史经营表",
            "sourceStatus": "competing",
            "status": "historical_reference",
            "metrics": {"sales": "90.1234", "profit": "12.3400"},
        }
    ]


def test_overview_uses_only_latest_contract_for_same_logical_store(
    tmp_path: Path,
) -> None:
    client, database_path = _client_with_facts(tmp_path)
    with DuckDBMemory(database_path) as memory:
        _insert_contract(memory, "contract-a-v2", "store-a-v2", "甲店")
        memory.execute(
            """
            UPDATE reconciliation_contract
            SET created_at = current_timestamp + INTERVAL 1 DAY
            WHERE contract_id = 'contract-a-v2'
            """
        )
        _insert_period(
            memory,
            "period-a-v2-feb",
            "contract-a-v2",
            "store-a-v2",
            "2026-02-01",
            "2026-02-28",
        )
        _insert_run(
            memory,
            "run-a-v2",
            "contract-a-v2",
            "period-a-v2-feb",
            "2026-04-05T00:00:00+00:00",
            certifiable=True,
        )
        _insert_item(
            memory,
            "item-a-v2",
            "run-a-v2",
            "contract-a-v2",
            "period-a-v2-feb",
            "baobei_order",
            "order",
            "order-a-v2",
            "2026-02-11",
            "50.0000",
            {
                "paid_amount": "50.0000",
                "refund_amount": "0.0000",
                "business_date": "2026-02-11",
            },
        )

    body = client.get("/api/v1/analytics/overview").json()

    assert [item["name"] for item in body["filters"]["stores"]] == ["乙店", "甲店"]
    assert body["metrics"]["orderGross"] == "250.0000"
    assert "store-a-v2" in json.dumps(body, ensure_ascii=False)
    assert '"store-a"' not in json.dumps(body, ensure_ascii=False)


def test_overview_infers_finite_platform_and_excludes_other_enterprise(
    tmp_path: Path,
) -> None:
    client, database_path = _client_with_facts(tmp_path)
    with DuckDBMemory(database_path) as memory:
        _insert_contract(
            memory,
            "contract-pdd-old",
            "store-pdd-old",
            "PDD测试店",
            "taobao",
        )
        _insert_period(
            memory,
            "period-pdd-old",
            "contract-pdd-old",
            "store-pdd-old",
            "2026-02-01",
            "2026-02-28",
        )
        _insert_contract(
            memory,
            "contract-pdd-current",
            "store-pdd-current",
            "PDD测试店",
            "pinduoduo",
        )
        memory.execute(
            """
            UPDATE reconciliation_contract
            SET created_at = current_timestamp + INTERVAL 1 DAY
            WHERE contract_id = 'contract-pdd-current'
            """
        )
        _insert_period(
            memory,
            "period-pdd-current",
            "contract-pdd-current",
            "store-pdd-current",
            "2026-02-01",
            "2026-02-28",
        )
        memory.execute(
            """
            INSERT INTO reconciliation_contract (
                contract_id, logical_key, enterprise_id, store_id,
                platform_code, contract_version, effective_from,
                status, definition_json
            )
            VALUES (
                'contract-external', 'logical-external', 'enterprise-external',
                'store-external', 'taobao', 1, DATE '2026-02-01',
                'active', '{"store_name": "外部测试店"}'
            )
            """
        )
        _insert_period(
            memory,
            "period-external",
            "contract-external",
            "store-external",
            "2026-02-01",
            "2026-02-28",
        )

    body = client.get("/api/v1/analytics/overview").json()
    pdd_stores = [
        item
        for item in body["filters"]["stores"]
        if item["name"] == "PDD测试店"
    ]

    assert pdd_stores == [
        {
            "id": "store-pdd-current",
            "name": "PDD测试店",
            "platformId": "pinduoduo",
            "platformName": "拼多多",
        }
    ]
    assert "外部测试店" not in {
        item["name"] for item in body["filters"]["stores"]
    }


def test_overview_filters_store_period_date_and_uses_business_time(
    tmp_path: Path,
) -> None:
    client, _ = _client_with_facts(tmp_path)

    response = client.get(
        "/api/v1/analytics/overview",
        params={
            "storeId": "store-a",
            "period": "2026-02",
            "fromDate": "2026-02-10",
            "toDate": "2026-02-10",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["orderGross"] == "100.0000"
    assert body["metrics"]["walletNet"] == "0.0000"
    assert body["metrics"]["orderCount"] == 1
    assert body["trend"] == [
        {
            "date": "2026-02-10",
            "orderGross": "100.0000",
            "refunds": "10.0000",
            "netSales": "90.0000",
            "walletNet": "0.0000",
        }
    ]
    assert body["transactions"] == [
        {
            "occurredAt": "2026-02-10T11:22:33",
            "storeId": "store-a",
            "storeName": "甲店",
            "sourceKind": "baobei_order",
            "sourceLabel": "订单明细",
            "amount": "90.0000",
            "direction": "income",
            "businessDescription": "订单收入",
            "businessKey": "order-a",
        }
    ]
    assert "run-a-new" not in json.dumps(body)


def test_overview_filters_platform_and_returns_store_platform_catalog(
    tmp_path: Path,
) -> None:
    client, _ = _client_with_facts(tmp_path)

    body = client.get(
        "/api/v1/analytics/overview",
        params={"platformId": "jd"},
    ).json()

    assert body["selection"]["platformId"] == "jd"
    assert body["metrics"]["orderGross"] == "200.0000"
    assert body["metrics"]["orderCount"] == 1
    assert body["filters"]["platforms"] == [
        {"id": "jd", "name": "京东"},
        {"id": "taobao", "name": "淘宝"},
    ]
    assert {
        (item["name"], item["platformId"], item["platformName"])
        for item in body["filters"]["stores"]
    } == {
        ("乙店", "jd", "京东"),
        ("甲店", "taobao", "淘宝"),
    }


def test_overview_profit_is_certified_only_with_certifiable_current_pnl(
    tmp_path: Path,
) -> None:
    client, database_path = _client_with_facts(tmp_path)
    with DuckDBMemory(database_path) as memory:
        memory.execute(
            """
            UPDATE run_log
            SET metrics_json = '{"certifiable": true}'
            WHERE run_id = 'run-a-new'
            """
        )
        memory.execute(
            """
            INSERT INTO pnl_cell (
                pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
                definition_id, value, evidence_json
            )
            VALUES (
                'pnl-a', 'run-a-new', 'period-a-feb', 'store-a', 'all',
                'profit', 'pnl-v1', 12.3400, '[]'
            )
            """
        )

    body = client.get(
        "/api/v1/analytics/overview",
        params={"storeId": "store-a", "period": "2026-02"},
    ).json()

    assert body["coverage"]["status"] == "system_checked"
    assert body["coverage"]["profitStatus"] == "system_checked"
    assert body["monthlyPnl"][0]["status"] == "historical_reference"


def test_overview_rejects_unknown_filters_and_invalid_dates(tmp_path: Path) -> None:
    client, _ = _client_with_facts(tmp_path)

    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"platformId": "unknown"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"storeId": "store-does-not-exist"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"period": "2025-12"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"fromDate": "2026-03-01", "toDate": "2026-02-01"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"fromDate": "not-a-date"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/analytics/overview",
            params={"limit": 101},
        ).status_code
        == 422
    )


def test_catalog_discovers_stores_periods_processed_and_repairs_mojibake(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "source-inventory.json"
    correct_name = "喜必顺旗舰店"
    mojibake_name = correct_name.encode("utf-8").decode("latin1")
    first_path = f"D:\\内贸\\宝贝报表\\店铺\\{mojibake_name}\\202602订单.xlsx"
    payload = {
        "counts": {"all": 5, "candidates": 2},
        "candidate_source_ids": ["source-1", "source-2"],
        "records": [
            {
                "source_id": "source-1",
                "purpose": "orders",
                "path": first_path,
            },
            {
                "source_id": "source-2",
                "purpose": "settlement",
                "path": "D:\\内贸\\支付宝收支\\另一家店\\2603\\账单.xlsx",
            },
            {
                "source_id": "source-3",
                "purpose": "orders",
                "path": "D:\\内贸\\宝贝报表\\店铺\\模板\\202604模板.xlsx",
            },
            {
                "source_id": "source-4",
                "purpose": "product_cost",
                "path": "D:\\内贸\\聚水潭成本\\2026\\另一家店\\202604成本.xlsx",
            },
            {
                "source_id": "source-5",
                "purpose": "rule_corpus",
                "path": "C:\\temp\\fa_sample\\shop_xbs_2604.csv",
            },
        ],
    }
    inventory.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    body = analytics_catalog(
        inventory,
        processed_source_uris=[f"finance-win-ro://{first_path}"],
        known_stores={correct_name: "store-known"},
        known_store_platforms={
            correct_name: "taobao",
            "另一家店": "pinduoduo",
        },
    )

    assert body["allRecordCount"] == 5
    assert body["candidateRecordCount"] == 2
    assert body["discoveredStoreCount"] == 2
    assert body["processedStoreCount"] == 1
    assert body["stores"] == [
        {
            "id": "discovered_f70db19d1104e851",
            "name": "另一家店",
            "platformId": "pinduoduo",
            "platformName": "拼多多",
            "periods": ["2026-03", "2026-04"],
            "fileCount": 2,
            "processed": False,
        },
        {
            "id": "store-known",
            "name": correct_name,
            "platformId": "taobao",
            "platformName": "淘宝",
            "periods": ["2026-02"],
            "fileCount": 1,
            "processed": True,
        },
    ]
    assert body["platforms"] == [
        {"id": "pinduoduo", "name": "拼多多"},
        {"id": "taobao", "name": "淘宝"},
    ]
