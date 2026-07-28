import os
import time

from finance_agent.config import AgentConfig
from finance_agent.connectors.local_fixture import LocalFixtureConnector


def test_local_fixture_scan_is_stable_safe_and_deterministic(tmp_path):
    root = tmp_path / "fixtures"
    orders = root / "orders"
    orders.mkdir(parents=True)
    stable = orders / "orders.csv"
    stable.write_text("订单号,销售额\nA1,10.00\n", encoding="utf-8")
    old = time.time() - 1200
    os.utime(stable, (old, old))
    (orders / "~$locked.xlsx").write_bytes(b"ignored")
    (orders / "secret.db").write_bytes(b"ignored")

    config = AgentConfig(
        connector="local_fixture",
        fixture_root=root,
        state_dir=tmp_path / "state",
        stable_for_seconds=600,
    )
    connector = LocalFixtureConnector(config)
    first = connector.scan()
    second = connector.scan()

    assert [item.path for item in first] == [str(stable)]
    assert first[0].source_id == second[0].source_id
    assert first[0].purpose == "orders"


def test_local_materialize_is_streamed_to_agent_workdir(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "orders.csv"
    source.write_bytes(b"order_id,sales\nA,1.00\n")
    old = time.time() - 1200
    os.utime(source, (old, old))
    connector = LocalFixtureConnector(
        AgentConfig(
            connector="local_fixture",
            fixture_root=root,
            state_dir=tmp_path / "state",
            stable_for_seconds=0,
        )
    )
    record = connector.scan()[0]
    target = tmp_path / "work" / "source.csv"

    assert connector.materialize(record, target) == target
    assert target.read_bytes() == source.read_bytes()
    import hashlib

    assert connector.stable_sha256(record) == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
