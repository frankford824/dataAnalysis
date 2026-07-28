from __future__ import annotations

import json
from dataclasses import dataclass

import uvicorn

from commerce_harness.cli import _parser, main
from commerce_harness.pipeline import RunResult


def test_cli_exposes_all_phase_a_execution_commands() -> None:
    parser = _parser()
    choices = next(
        action.choices
        for action in parser._actions
        if getattr(action, "choices", None)
    )
    assert {"normalize", "recon", "diff", "baseline"} <= set(choices)


@dataclass(frozen=True)
class _CommandResult:
    command: str


def test_cli_dispatches_all_phase_a_commands(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workspace = tmp_path / "workbench"
    assert main(["init", "--workspace", str(workspace)]) == 0
    capsys.readouterr()
    calls: list[tuple[str, object]] = []

    def normalize(_workbench, *, periods, store_id):
        calls.append(("normalize", (periods, store_id)))
        return _CommandResult("normalize")

    def reconcile(_workbench, *, period_token, store_id, mode):
        calls.append(("recon", (period_token, store_id, mode)))
        return _CommandResult("recon")

    def compare(_workbench, *, period_token, store_id):
        calls.append(("diff", (period_token, store_id)))
        return _CommandResult("diff")

    def baseline(_workbench, *, period_token, store_id, freeze, actor):
        calls.append(("baseline", (period_token, store_id, freeze, actor)))
        return _CommandResult("baseline")

    monkeypatch.setattr(
        "commerce_harness.phase_a.normalize_workspace",
        normalize,
    )
    monkeypatch.setattr(
        "commerce_harness.phase_a.reconcile_period",
        reconcile,
    )
    monkeypatch.setattr(
        "commerce_harness.phase_a.compare_period",
        compare,
    )
    monkeypatch.setattr(
        "commerce_harness.phase_a.create_baseline",
        baseline,
    )

    commands = (
        [
            "normalize",
            "--workspace",
            str(workspace),
            "--period",
            "2602",
            "--store",
            "store-a",
        ],
        [
            "recon",
            "--workspace",
            str(workspace),
            "--period",
            "2602",
            "--store",
            "store-a",
        ],
        [
            "diff",
            "--workspace",
            str(workspace),
            "--period",
            "2602",
            "--store",
            "store-a",
        ],
        [
            "baseline",
            "--workspace",
            str(workspace),
            "--period",
            "2602",
            "--store",
            "store-a",
            "--freeze",
            "--actor",
            "tester",
        ],
    )
    for command in commands:
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out)["command"] == command[0]

    assert calls == [
        ("normalize", (("2602",), "store-a")),
        ("recon", ("2602", "store-a", "platform_wallet")),
        ("diff", ("2602", "store-a")),
        ("baseline", ("2602", "store-a", True, "tester")),
    ]


def test_cli_run_exit_code_follows_reconcile_outcome(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workspace = tmp_path / "workbench"
    assert main(["init", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    def _pipeline(outcome: RunResult):
        return lambda *_args, **_kwargs: outcome

    succeeded = RunResult(
        inventory={"skipped": True},
        freeze={"skipped": True},
        profile={},
        normalize={},
        adjudication={},
        reconcile=[{"period": "2603"}],
    )
    monkeypatch.setattr(
        "commerce_harness.pipeline.run_pipeline",
        _pipeline(succeeded),
    )
    assert main(["run", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    failed = RunResult(
        inventory={"skipped": True},
        freeze={"skipped": True},
        profile={},
        normalize={},
        adjudication={},
        reconcile=[],
        failures=("2603: 对账口径缺失",),
    )
    monkeypatch.setattr(
        "commerce_harness.pipeline.run_pipeline",
        _pipeline(failed),
    )
    assert main(["run", "--workspace", str(workspace)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is False
    assert "对账口径缺失" in captured.err


def test_cli_initializes_reports_status_and_schema(tmp_path, capsys) -> None:
    workspace = tmp_path / "workbench"

    assert main(["init", "--workspace", str(workspace)]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["customer_data_in_git"] is False

    assert main(["status", "--workspace", str(workspace)]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["initialized"] is True
    assert status_payload["llm_required"] is False

    assert main(["schema", "--workspace", str(workspace)]) == 0
    schema_payload = json.loads(capsys.readouterr().out)
    assert schema_payload["schema_ready"] is True


def test_cli_rejects_network_bind_unless_container_boundary_is_explicit(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workspace = tmp_path / "workbench"
    web_dist = tmp_path / "dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<main>ready</main>", encoding="utf-8")
    assert main(["init", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "serve",
                "--workspace",
                str(workspace),
                "--host",
                "0.0.0.0",
            ]
        )
        == 1
    )
    assert "默认只允许绑定本机回环地址" in capsys.readouterr().err

    calls: list[dict[str, object]] = []
    monkeypatch.setenv("FA_CONTAINER_BIND", "1")
    monkeypatch.setenv("FA_WEB_DIST", str(web_dist))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))

    assert (
        main(
            [
                "serve",
                "--workspace",
                str(workspace),
                "--host",
                "0.0.0.0",
                "--port",
                "9876",
            ]
        )
        == 0
    )
    assert calls == [{"host": "0.0.0.0", "port": 9876, "log_level": "info"}]
