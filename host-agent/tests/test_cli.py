import os
import time

from finance_agent.cli import main


def test_cli_scan_and_profile_local_fixture(tmp_path, capsys):
    root = tmp_path / "fixtures"
    root.mkdir()
    source = root / "orders.csv"
    source.write_text(
        "订单号,业务日期,销售额\nA,2026-07-01,1.00\n", encoding="utf-8"
    )
    old = time.time() - 100
    os.utime(source, (old, old))
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[agent]
connector = "local_fixture"
fixture_root = "{root}"
state_dir = "{tmp_path / 'state'}"
[safety]
stable_for_seconds = 0
""",
        encoding="utf-8",
    )

    assert main(["--config", str(config), "scan"]) == 0
    assert '"count": 1' in capsys.readouterr().out
    assert main(["profile", str(source), "--purpose", "orders"]) == 0
    output = capsys.readouterr().out
    assert '"classification": "orders"' in output

    result_path = tmp_path / "normalized.parquet"
    assert (
        main(
            [
                "recompute",
                str(source),
                "--business-key",
                "订单号",
                "--amount-column",
                "销售额",
                "--output",
                str(result_path),
            ]
        )
        == 0
    )
    assert '"销售额": "1.0000"' in capsys.readouterr().out
    assert result_path.exists()
