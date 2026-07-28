import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.config import AgentConfig, SourceRoot
from finance_agent.connectors.windows_ssh import (
    SshCommandError,
    WindowsSshConnector,
    encode_powershell,
    parse_json_records,
)


def test_remote_json_parser_accepts_object_array_and_bom():
    assert parse_json_records('\ufeff{"path":"x"}') == [{"path": "x"}]
    assert parse_json_records('[{"path":"a"},{"path":"b"}]') == [
        {"path": "a"},
        {"path": "b"},
    ]
    assert parse_json_records("") == []
    with pytest.raises(SshCommandError, match="无法解析"):
        parse_json_records("PowerShell banner\n[]")


def test_powershell_encoding_is_utf16le():
    encoded = encode_powershell("Write-Output '中文'")
    import base64

    assert base64.b64decode(encoded).decode("utf-16-le") == "Write-Output '中文'"


def test_ssh_command_forces_utf8_without_interpolating_alias(tmp_path):
    import base64

    connector = WindowsSshConnector(
        AgentConfig(state_dir=tmp_path, ssh_alias="finance-win")
    )
    command = connector._ssh_command("Write-Output '中文'")
    encoded = command[-1].rsplit(" ", 1)[-1]
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert "[Console]::OutputEncoding = $utf8" in decoded
    assert command[-2] == "finance-win"


def test_scan_parses_remote_response_filters_offline_and_unstable(monkeypatch, tmp_path):
    stable = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    unstable = datetime.now(UTC).isoformat()
    responses = [
        json.dumps(
            [
                {
                    "path": r"D:\FinanceData\BI\经营.pbix",
                    "size": 100,
                    "mtime_utc": stable,
                    "extension": ".pbix",
                    "attributes": ["Archive", "Pinned", "ReparsePoint"],
                },
                {
                    "path": r"D:\FinanceData\BI\云端.pbix",
                    "size": 100,
                    "mtime_utc": stable,
                    "extension": ".pbix",
                    "attributes": ["Offline", "RecallOnDataAccess"],
                },
                {
                    "path": r"D:\FinanceData\BI\写入中.pbix",
                    "size": 100,
                    "mtime_utc": unstable,
                    "extension": ".pbix",
                    "attributes": ["Archive"],
                },
            ],
            ensure_ascii=False,
        ),
        "[]",
    ]

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, responses.pop(0).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            stable_for_seconds=600,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\BI", "pbix_asset", (".pbix",)
                ),
            ),
        )
    )
    records = connector.scan()
    assert [item.path for item in records] == [
        r"D:\FinanceData\BI\经营.pbix"
    ]
    assert records[0].purpose == "pbix_asset"


def test_detailed_scan_reports_offline_and_unstable_rejections(monkeypatch, tmp_path):
    stable = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    unstable = datetime.now(UTC).isoformat()
    responses = [
        json.dumps(
            [
                {
                    "path": r"D:\FinanceData\BI\云端.pbix",
                    "size": 100,
                    "mtime_utc": stable,
                    "extension": ".pbix",
                    "attributes": ["Offline", "RecallOnDataAccess"],
                },
                {
                    "path": r"D:\FinanceData\BI\写入中.pbix",
                    "size": 100,
                    "mtime_utc": unstable,
                    "extension": ".pbix",
                    "attributes": ["Archive"],
                },
            ],
            ensure_ascii=False,
        ),
        "[]",
    ]

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, responses.pop(0).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            stable_for_seconds=600,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\BI", "pbix_asset", (".pbix",)
                ),
            ),
        )
    )

    records, issues = connector.scan_detailed()

    assert records == []
    assert [issue.reason for issue in issues] == [
        "文件属性不安全: Offline, RecallOnDataAccess",
        "文件仍在稳定等待期内",
    ]


def test_invalid_ssh_alias_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="SSH alias"):
        WindowsSshConnector(
            AgentConfig(state_dir=tmp_path, ssh_alias="finance-win; rm -rf /")
        )


def test_materialize_streams_stdout_and_validates_size(monkeypatch, tmp_path):
    content = b"order_id,sales\nA,1.00\n"

    def fake_run(_command, stdout, stderr, timeout, check):
        assert timeout >= 60
        stdout.write(content)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\订单",
                    "orders",
                    (".csv",),
                ),
            ),
        )
    )
    from finance_agent.models import FileRecord

    record = FileRecord(
        source_id="source",
        path=r"D:\FinanceData\订单\orders.csv",
        purpose="orders",
        extension=".csv",
        size=len(content),
        mtime_utc="2026-01-01T00:00:00Z",
    )
    target = tmp_path / "source.csv"
    assert connector.materialize(record, target).read_bytes() == content


def test_stable_hash_uses_read_only_remote_command(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, b"ABCDEF\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\BI", "pbix_asset", (".pbix",)
                ),
            ),
        )
    )
    from finance_agent.models import FileRecord

    record = FileRecord(
        source_id="source",
        path=r"D:\FinanceData\BI\经营.pbix",
        purpose="pbix_asset",
        extension=".pbix",
        size=10,
        mtime_utc="2026-01-01T00:00:00Z",
    )
    assert connector.stable_sha256(record) == "abcdef"


def test_stat_record_returns_exact_stable_file(monkeypatch, tmp_path):
    stable = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    def fake_run(*_args, **_kwargs):
        payload = {
            "path": r"D:\FinanceData\订单\orders.csv",
            "size": 27,
            "mtime_utc": stable,
            "extension": ".csv",
            "attributes": ["Archive"],
        }
        return subprocess.CompletedProcess(
            [], 0, json.dumps(payload).encode("utf-8"), b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\订单",
                    "orders",
                    (".csv",),
                ),
            ),
        )
    )
    from finance_agent.models import FileRecord

    record = FileRecord(
        source_id="old",
        path=r"D:\FinanceData\订单\orders.csv",
        purpose="orders",
        extension=".csv",
        size=27,
        mtime_utc="2026-01-01T00:00:00Z",
    )
    result = connector.stat_record(record)
    assert result.path == record.path
    assert result.size == 27
    assert result.source_id != "old"


def test_iter_chunks_streams_without_building_full_file(monkeypatch, tmp_path):
    content = b"order_id,sales\nA,1.00\n"

    class FakeStdout:
        def __init__(self):
            self.offset = 0

        def read(self, size):
            chunk = content[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class FakeStderr:
        def read(self):
            return b""

    class FakeProcess:
        stdout = FakeStdout()
        stderr = FakeStderr()

        def wait(self, timeout=None):
            assert timeout
            return 0

        def kill(self):
            raise AssertionError("successful stream must not be killed")

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    connector = WindowsSshConnector(
        AgentConfig(
            state_dir=tmp_path,
            source_roots=(
                SourceRoot(
                    r"D:\FinanceData\订单",
                    "orders",
                    (".csv",),
                ),
            ),
        )
    )
    from finance_agent.models import FileRecord

    record = FileRecord(
        source_id="source",
        path=r"D:\FinanceData\订单\orders.csv",
        purpose="orders",
        extension=".csv",
        size=len(content),
        mtime_utc="2026-01-01T00:00:00Z",
    )
    assert b"".join(connector.iter_chunks(record, 7)) == content
