from __future__ import annotations

from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.profiling import PARSER_VERSION, profile_snapshots
from commerce_harness.snapshot import BytesReader, SnapshotStore
from commerce_harness.workbench import initialize


def test_profile_snapshots_routes_finite_template_and_is_repeatable(tmp_path):
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    manifest = SnapshotStore(workbench.snapshots).capture(
        BytesReader(
            "子订单编号,订单创建时间,买家实际支付金额\n"
            "A-1,2026-02-01,10.00\n".encode(),
            uri="synthetic://orders.csv",
        ),
        original_name="orders.csv",
        media_type="text/csv",
    )
    with DuckDBMemory(workbench.database) as database:
        database.initialize()
        database.register_snapshot(manifest)

    first = profile_snapshots(workbench)
    second = profile_snapshots(workbench)

    assert first.total == 1
    assert first.matched == 1
    assert second.total == 1
    assert second.matched == 1
    with DuckDBMemory(workbench.database) as database:
        assert database.execute(
            """
            SELECT parser_version, status, template_id
            FROM source_profile
            """
        ).fetchone() == (PARSER_VERSION, "matched", "taobao_order_v1")
        assert database.execute(
            "SELECT count(*) FROM source_profile"
        ).fetchone() == (1,)
