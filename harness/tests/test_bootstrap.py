from __future__ import annotations

import json

import pytest

from commerce_harness.bootstrap import (
    SOURCE_CONTRACT_VERSION,
    VERIFIED_RULES,
    StoreTarget,
    _period_tokens,
    bootstrap_target,
    bootstrap_targets,
    source_contract_for,
    stable_identity,
)
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _mark_snapshots_matched(
    database: DuckDBMemory,
    *,
    run_id: str,
    snapshot_ids: list[str],
) -> None:
    for index, snapshot_id in enumerate(snapshot_ids):
        database.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri,
                source_uri, original_name, captured_at, manifest_json
            )
            VALUES (?, ?, 1, ?, ?, ?, current_timestamp, '{}')
            """,
            [
                snapshot_id,
                f"{index + 1:064x}",
                f"object://{snapshot_id}",
                f"source://{snapshot_id}",
                f"{snapshot_id}.xlsx",
            ],
        )
        database.execute(
            """
            INSERT INTO source_profile (
                profile_id, run_id, snapshot_id, parser_version, status,
                source_kind, template_id
            )
            VALUES (?, ?, ?, 'test-parser', 'matched', 'test', 'test-v1')
            """,
            [f"profile-{snapshot_id}", run_id, snapshot_id],
        )


def test_annual_file_is_attached_to_same_year_but_rows_still_decide_month() -> None:
    configured = ["2602", "2603", "2604", "2701"]

    assert _period_tokens(
        r"D:\运费\店铺\测试店铺\26年发货运费.xlsx",
        configured,
    ) == {"2602", "2603", "2604"}
    assert _period_tokens(
        r"D:\运费\店铺\测试店铺\2027年发货运费.xlsx",
        configured,
    ) == {"2701"}


def test_bootstrap_creates_versioned_contract_rules_and_period_checklist(tmp_path) -> None:
    workbench = initialize(load_config(workspace=tmp_path / "workbench"))
    records = [
        {
            "source_id": "orders-2602",
            "purpose": "orders",
            "path": r"D:\测试店铺\2602\订单明细.xlsx",
        },
        {
            "source_id": "ad-2602",
            "purpose": "advertising",
            "path": r"D:\测试店铺\2602\直通车报表.csv",
        },
        {
            "source_id": "cost-2602",
            "purpose": "product_cost",
            "path": r"D:\测试店铺\2602\商品成本.xlsx",
        },
        {
            "source_id": "wechat-2602",
            "purpose": "settlement",
            "path": r"D:\测试店铺\2602\微信\221933收入.csv",
        },
        {
            "source_id": "platform-2602",
            "purpose": "settlement",
            "path": r"D:\测试店铺\2602\千牛明细\费用.csv",
        },
        {
            "source_id": "alipay-2602",
            "purpose": "settlement",
            "path": r"D:\测试店铺\2602\账务明细\支付宝.csv",
        },
    ]
    snapshot_ids = {record["source_id"]: f"snapshot-{record['source_id']}" for record in records}

    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        bootstrap_target(
            database,
            freeze_run_id="freeze-1",
            period_tokens=[],
            records=[],
            snapshot_by_source_id={},
            store_name="测试店铺",
        )
        contract_id = str(
            database.execute("SELECT contract_id FROM reconciliation_contract").fetchone()[0]
        )
        database.execute(
            """
            INSERT INTO run_log (run_id, contract_id, run_kind, status)
            VALUES ('freeze-1', ?, 'freeze', 'succeeded')
            """,
            [contract_id],
        )
        _mark_snapshots_matched(
            database,
            run_id="freeze-1",
            snapshot_ids=list(snapshot_ids.values()),
        )
        bootstrap_target(
            database,
            freeze_run_id="freeze-1",
            period_tokens=["2602"],
            records=records,
            snapshot_by_source_id=snapshot_ids,
            store_name="测试店铺",
        )
        bootstrap_target(
            database,
            freeze_run_id="freeze-1",
            period_tokens=["2602"],
            records=records,
            snapshot_by_source_id=snapshot_ids,
            store_name="测试店铺",
        )

        contract = database.execute(
            "SELECT definition_json FROM reconciliation_contract WHERE contract_id = ?",
            [contract_id],
        ).fetchone()
        contract_definition = json.loads(contract[0])
        assert contract_definition["money_engine"] == "decimal_38_4"
        assert (
            contract_definition["source_contract_version"]
            == SOURCE_CONTRACT_VERSION
        )
        effective_from = database.execute(
            """
            SELECT effective_from
            FROM reconciliation_contract
            WHERE contract_id = ?
            """,
            [contract_id],
        ).fetchone()[0]
        assert str(effective_from) == "2026-02-01"
        assert database.execute("SELECT count(*) FROM accounting_period").fetchone() == (1,)
        assert database.execute("SELECT count(*) FROM rule_version").fetchone() == (
            len(VERIFIED_RULES),
        )
        assert database.execute("SELECT count(*) FROM business_decision").fetchone() == (3,)

        results = dict(
            database.execute(
                """
                SELECT r.source_kind, c.status
                FROM checklist_result c
                JOIN checklist_requirement r USING (requirement_id)
                """
            ).fetchall()
        )
        expected_kinds = {
            requirement.kind for requirement in source_contract_for("taobao")
        }
        assert set(results) == expected_kinds
        assert results["orders"] == "present"
        assert results["platform_wallet"] == "present"
        assert results["platform_fee_details"] == "present"
        assert results["advertising"] == "present"
        assert results["product_cost"] == "present"
        assert results["shipping"] == "not_applicable"


def test_bootstrap_targets_isolates_stores_and_produces_stable_ids(tmp_path) -> None:
    workbench = initialize(load_config(workspace=tmp_path / "workbench"))
    targets = [
        StoreTarget("一店", ["2603", "2601"], "taobao"),
        StoreTarget("二店", ["2601"], "jd"),
    ]
    records = [
        {
            "source_id": "one-orders",
            "purpose": "orders",
            "path": r"D:\财务\一店\2601\订单明细.xlsx",
        },
        {
            "source_id": "two-orders",
            "purpose": "orders",
            "path": r"D:\财务\二店\2601\订单明细.xlsx",
        },
        {
            "source_id": "period-only",
            "purpose": "orders",
            "path": r"D:\财务\未绑定店铺\2601\订单明细.xlsx",
        },
    ]
    snapshots = {record["source_id"]: f"snapshot-{record['source_id']}" for record in records}

    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-multi', 'freeze', 'succeeded')
            """
        )
        _mark_snapshots_matched(
            database,
            run_id="freeze-multi",
            snapshot_ids=list(snapshots.values()),
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-multi",
            targets=targets,
            records=records,
            snapshot_by_source_id=snapshots,
        )
        first_contract_ids = [
            row[0]
            for row in database.execute(
                "SELECT contract_id FROM reconciliation_contract ORDER BY contract_id"
            ).fetchall()
        ]
        bootstrap_targets(
            database,
            freeze_run_id="freeze-multi",
            targets=targets,
            records=records,
            snapshot_by_source_id=snapshots,
        )

        assert database.execute("SELECT count(*) FROM reconciliation_contract").fetchone() == (2,)
        assert database.execute("SELECT count(*) FROM accounting_period").fetchone() == (3,)
        expected_requirement_count = len(source_contract_for("taobao")) + len(
            source_contract_for("jd")
        )
        assert database.execute(
            "SELECT count(*) FROM checklist_requirement"
        ).fetchone() == (expected_requirement_count,)
        assert first_contract_ids == [
            row[0]
            for row in database.execute(
                "SELECT contract_id FROM reconciliation_contract ORDER BY contract_id"
            ).fetchall()
        ]

        order_results = database.execute(
            """
            SELECT c.definition_json, p.period_start, r.observed_json
            FROM checklist_result r
            JOIN checklist_requirement q USING (requirement_id)
            JOIN accounting_period p USING (period_id)
            JOIN reconciliation_contract c
              ON c.contract_id = p.contract_id
            WHERE q.source_kind = 'orders'
              AND p.period_start = DATE '2026-01-01'
            ORDER BY c.definition_json
            """
        ).fetchall()
        sources_by_store = {
            json.loads(definition)["store_name"]: json.loads(observed)["source_ids"]
            for definition, _, observed in order_results
        }
        assert sources_by_store == {
            "一店": ["one-orders"],
            "二店": ["two-orders"],
        }
        assert "period-only" not in {
            source_id for source_ids in sources_by_store.values() for source_id in source_ids
        }

        effective = {
            json.loads(definition)["store_name"]: str(effective_from)
            for definition, effective_from in database.execute(
                """
                SELECT definition_json, effective_from
                FROM reconciliation_contract
                """
            ).fetchall()
        }
        assert effective == {
            "一店": "2026-01-01",
            "二店": "2026-01-01",
        }


def test_platform_contracts_do_not_borrow_taobao_only_requirements(
    tmp_path,
) -> None:
    workbench = initialize(load_config(workspace=tmp_path / "workbench"))
    records = [
        {
            "source_id": "pdd-orders",
            "purpose": "orders",
            "path": r"D:\财务\PDD一店\2026\2603.csv",
        },
        {
            "source_id": "pdd-wallet",
            "purpose": "settlement",
            "path": r"D:\财务\PDD一店\2026\2603PDD一店.xlsx",
        },
        {
            "source_id": "pdd-penalty",
            "purpose": "settlement",
            "path": r"D:\财务\PDD一店\订单扣款\2026\2603延迟发货.xlsx",
        },
    ]
    snapshots = {
        record["source_id"]: f"snapshot-{record['source_id']}"
        for record in records
    }

    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-pdd', 'freeze', 'succeeded')
            """
        )
        _mark_snapshots_matched(
            database,
            run_id="freeze-pdd",
            snapshot_ids=list(snapshots.values()),
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-pdd",
            targets=[StoreTarget("PDD一店", ["2603"], "pinduoduo")],
            records=records,
            snapshot_by_source_id=snapshots,
        )

        results = dict(
            database.execute(
                """
                SELECT requirement.source_kind, result.status
                FROM checklist_result result
                JOIN checklist_requirement requirement
                  USING (requirement_id)
                """
            ).fetchall()
        )
        assert results["orders"] == "present"
        assert results["platform_wallet"] == "present"
        assert results["platform_adjustments"] == "present"
        assert "alipay_settlement" not in results
        assert "wechat_funds" not in results
        required = {
            source_kind
            for source_kind, in database.execute(
                """
                SELECT source_kind
                FROM checklist_requirement
                WHERE required = true
                """
            ).fetchall()
        }
        assert required == {"orders", "platform_wallet"}


def test_stable_identity_is_repeatable_and_scope_sensitive() -> None:
    first = stable_identity("store", "enterprise-a", "taobao", "一店")

    assert first == stable_identity("store", "enterprise-a", "taobao", "一店")
    assert first != stable_identity("store", "enterprise-a", "taobao", "二店")
    assert first != stable_identity("store", "enterprise-b", "taobao", "一店")


def test_bootstrap_retires_obsolete_targets_without_deleting_history(tmp_path) -> None:
    workbench = initialize(load_config(workspace=tmp_path / "workbench"))
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-retire', 'freeze', 'succeeded')
            """
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-retire",
            targets=[
                StoreTarget("PDD真实店铺", ["2602"], "pinduoduo"),
                StoreTarget("PDD旧店铺", ["2602"], "taobao"),
            ],
            records=[],
            snapshot_by_source_id={},
            retire_missing=True,
        )
        obsolete_contract_id = str(
            database.execute(
                """
                SELECT contract_id
                FROM reconciliation_contract
                WHERE json_extract_string(definition_json, '$.store_name') = 'PDD旧店铺'
                """
            ).fetchone()[0]
        )
        bootstrap_targets(
            database,
            freeze_run_id="freeze-retire",
            targets=[StoreTarget("PDD真实店铺", ["2602"], "pinduoduo")],
            records=[],
            snapshot_by_source_id={},
            retire_missing=True,
        )

        assert database.execute(
            """
            SELECT status, effective_to IS NOT NULL
            FROM reconciliation_contract
            WHERE contract_id = ?
            """,
            [obsolete_contract_id],
        ).fetchone() == ("retired", True)
        assert database.execute(
            """
            SELECT count(*)
            FROM accounting_period
            WHERE contract_id = ?
            """,
            [obsolete_contract_id],
        ).fetchone() == (1,)


@pytest.mark.parametrize("token", ["2600", "2613", "bad"])
def test_bootstrap_rejects_invalid_period_tokens(tmp_path, token: str) -> None:
    workbench = initialize(load_config(workspace=tmp_path / "workbench"))
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        with pytest.raises(ValueError, match="账期"):
            bootstrap_target(
                database,
                freeze_run_id="freeze-invalid",
                period_tokens=[token],
                records=[],
                snapshot_by_source_id={},
            )
