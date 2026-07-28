from __future__ import annotations

import json
from pathlib import Path

import pytest

from commerce_harness.bootstrap import StoreTarget, bootstrap_target, bootstrap_targets
from commerce_harness.config import HarnessConfig, SourceScope, load_config
from commerce_harness.freeze import _configured_targets
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.phase_a import _period_row
from commerce_harness.workbench import WorkbenchPaths, initialize


def _workbench(tmp_path: Path) -> WorkbenchPaths:
    return initialize(load_config(workspace=tmp_path / "workbench"))


def test_freeze_builds_explicit_targets_with_canonical_periods() -> None:
    config = HarnessConfig.model_validate(
        {
            "source": {
                "scope": {
                    "shops": ["North Shop", "South Shop"],
                    "periods": ["2602", "2026-03"],
                }
            }
        }
    )

    targets = _configured_targets(config, records=[])

    assert targets == [
        StoreTarget("North Shop", ["2602", "2603"]),
        StoreTarget("South Shop", ["2602", "2603"]),
    ]


def test_freeze_discovers_only_business_store_paths() -> None:
    config = HarnessConfig.model_validate(
        {"source": {"scope": {"include_all_discovered": True}}}
    )
    records = [
        {
            "purpose": "orders",
            "path": r"C:\data\店铺\North Shop\202603\orders.xlsx",
        },
        {
            "purpose": "settlement",
            "path": r"C:\data\店铺\North Shop\202604\wallet.csv",
        },
        {
            "purpose": "pbix_asset",
            "path": r"C:\data\店铺\Dashboard Name\202603\report.pbix",
        },
    ]

    assert _configured_targets(config, records) == [
        StoreTarget("North Shop", ["2603", "2604"])
    ]


def _bootstrap_same_month_stores(
    database: DuckDBMemory,
    *,
    run_id: str,
) -> dict[str, tuple[str, str]]:
    database.execute(
        """
        INSERT INTO run_log (run_id, run_kind, status)
        VALUES (?, 'freeze', 'succeeded')
        """,
        [run_id],
    )
    bootstrap_targets(
        database,
        freeze_run_id=run_id,
        targets=[
            StoreTarget("North Shop", ["2603"], "taobao"),
            StoreTarget("South Shop", ["2603"], "jd"),
        ],
        records=[],
        snapshot_by_source_id={},
    )
    rows = database.execute(
        """
        SELECT json_extract_string(contract.definition_json, '$.store_name'),
               period.period_id,
               period.store_id
        FROM accounting_period period
        JOIN reconciliation_contract contract USING (contract_id)
        WHERE period.period_start = DATE '2026-03-01'
        ORDER BY 1
        """
    ).fetchall()
    return {
        str(store_name): (str(period_id), str(store_id))
        for store_name, period_id, store_id in rows
    }


def test_phase_a_resolves_same_month_by_store_without_crossing(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        periods = _bootstrap_same_month_stores(database, run_id="freeze-multi")

        assert set(periods) == {"North Shop", "South Shop"}
        assert periods["North Shop"] != periods["South Shop"]

        with pytest.raises(RuntimeError, match="必须明确选择店铺"):
            _period_row(database, "2603")

        for store_name, (expected_period_id, store_id) in periods.items():
            selected = _period_row(database, "2603", store_id=store_id)
            assert selected[0] == expected_period_id, store_name
            assert selected[2] == store_id, store_name


def test_each_freeze_run_can_resolve_a_stable_store_and_period(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        first = _bootstrap_same_month_stores(database, run_id="freeze-first")
        second = _bootstrap_same_month_stores(database, run_id="freeze-second")

        assert second == first
        rows = database.execute(
            """
            SELECT result.run_id,
                   json_extract_string(contract.definition_json, '$.store_name'),
                   period.period_id,
                   period.store_id,
                   requirement.store_scope
            FROM checklist_result result
            JOIN accounting_period period USING (period_id)
            JOIN reconciliation_contract contract USING (contract_id)
            JOIN checklist_requirement requirement USING (requirement_id)
            WHERE result.run_id IN ('freeze-first', 'freeze-second')
              AND requirement.source_kind = 'orders'
            ORDER BY result.run_id, 2
            """
        ).fetchall()

        assert len(rows) == 4
        for run_id, store_name, period_id, store_id, store_scope in rows:
            assert run_id in {"freeze-first", "freeze-second"}
            assert (str(period_id), str(store_id)) == first[str(store_name)]
            assert str(store_scope) == str(store_id)


def test_legacy_single_shop_scope_and_bootstrap_remain_compatible(
    tmp_path: Path,
) -> None:
    scope = SourceScope(shop="Legacy Shop", periods=["2603"])
    assert scope.bound_shops == ("Legacy Shop",)

    workbench = _workbench(tmp_path)
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.execute(
            """
            INSERT INTO run_log (run_id, run_kind, status)
            VALUES ('freeze-legacy', 'freeze', 'succeeded')
            """
        )
        bootstrap_target(
            database,
            freeze_run_id="freeze-legacy",
            period_tokens=scope.periods,
            records=[],
            snapshot_by_source_id={},
            store_name=scope.shop,
        )

        period_id, contract_id, store_id, period_start, period_end = _period_row(
            database,
            "2603",
        )
        definition = database.execute(
            """
            SELECT definition_json
            FROM reconciliation_contract
            WHERE contract_id = ?
            """,
            [contract_id],
        ).fetchone()

        assert period_id
        assert store_id
        assert str(period_start) == "2026-03-01"
        assert str(period_end) == "2026-03-31"
        assert json.loads(str(definition[0]))["store_name"] == "Legacy Shop"
