import duckdb

from finance_agent.engine import RecomputeSpec, deterministic_recompute


def test_recompute_uses_business_key_dedup_and_decimal(tmp_path):
    source = tmp_path / "orders.csv"
    source.write_text(
        "订单号,销售额,退款\n"
        "A,900.0000,70.0000\n"
        "A,999.0000,999.0000\n"
        "B,0.0001,0.0000\n",
        encoding="utf-8",
    )
    output = tmp_path / "normalized.parquet"
    result = deterministic_recompute(
        source,
        RecomputeSpec("订单号", ("销售额", "退款")),
        output,
    )
    assert result["business_key_count"] == 2
    assert result["totals"] == {"销售额": "900.0001", "退款": "70.0000"}
    assert len(result["normalized_sha256"]) == 64
    with duckdb.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*), SUM(销售额) FROM read_parquet(?)", [str(output)]
        ).fetchone()
    assert row[0] == 2
    assert str(row[1]) == "900.0001"
