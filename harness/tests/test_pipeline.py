"""One-command pipeline: honest exit codes and role boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from commerce_harness import pipeline as pipeline_module
from commerce_harness.config import load_config
from commerce_harness.pipeline import ROLE_ENV, RunResult, run_pipeline
from commerce_harness.role import LEGACY_CORE_SOURCE_READ_ENV
from commerce_harness.workbench import initialize


@dataclass
class _Boxed:
    """Stand-in for the pipeline steps' dataclass results."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@pytest.fixture
def _stubbed(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"scan": 0, "freeze": 0}

    def _scan(*_: object, **__: object) -> _Boxed:
        calls["scan"] += 1
        return _Boxed({"path": Path("/tmp/source"), "files": 0})

    def _freeze(*_: object, **__: object) -> _Boxed:
        calls["freeze"] += 1
        return _Boxed({"frozen": 0})

    monkeypatch.setattr(pipeline_module, "scan_inventory", _scan)
    monkeypatch.setattr(pipeline_module, "freeze_candidates", _freeze)
    monkeypatch.setattr(
        pipeline_module, "profile_snapshots", lambda *_, **__: _Boxed({"profiled": 0})
    )
    monkeypatch.setattr(
        pipeline_module,
        "normalize_workspace",
        lambda *_, **__: _Boxed({"rows": 0}),
    )
    monkeypatch.setattr(
        pipeline_module,
        "adjudicate_workspace",
        lambda *_, **__: _Boxed({"adjudicated": 0}),
    )
    monkeypatch.setattr(pipeline_module, "refresh_target_plan", lambda *_, **__: None)
    return calls


def _run(tmp_path: Path, **kwargs: Any) -> RunResult:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    return run_pipeline(config, workbench, period="2026-03", **kwargs)


def test_failed_period_is_reported_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed: dict[str, int]
) -> None:
    def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("对账口径缺失")

    monkeypatch.setattr(pipeline_module, "reconcile_period", _boom)
    result = _run(tmp_path)

    assert result.ok is False
    assert result.failures and "对账口径缺失" in result.failures[0]
    assert result.to_dict()["ok"] is False


def test_successful_period_is_reported_as_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed: dict[str, int]
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "reconcile_period",
        lambda *_, **__: _Boxed({"period": "2026-03"}),
    )
    result = _run(tmp_path)

    assert result.ok is True
    assert result.failures == ()


def test_core_role_never_reads_customer_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed: dict[str, int]
) -> None:
    monkeypatch.setenv(ROLE_ENV, "core")
    monkeypatch.setattr(
        pipeline_module,
        "reconcile_period",
        lambda *_, **__: _Boxed({"period": "2026-03"}),
    )
    result = _run(tmp_path)

    assert _stubbed == {"scan": 0, "freeze": 0}
    assert result.inventory["skipped"] is True
    assert result.freeze["skipped"] is True
    assert result.ok is True


def test_edge_role_does_read_customer_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed: dict[str, int]
) -> None:
    monkeypatch.setenv(ROLE_ENV, "edge")
    monkeypatch.setattr(
        pipeline_module,
        "reconcile_period",
        lambda *_, **__: _Boxed({"period": "2026-03"}),
    )
    _run(tmp_path)

    assert _stubbed == {"scan": 1, "freeze": 1}


def test_single_machine_core_may_still_read_sources_while_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed: dict[str, int]
) -> None:
    monkeypatch.setenv(ROLE_ENV, "core")
    monkeypatch.setenv(LEGACY_CORE_SOURCE_READ_ENV, "1")
    monkeypatch.setattr(
        pipeline_module,
        "reconcile_period",
        lambda *_, **__: _Boxed({"period": "2026-03"}),
    )
    _run(tmp_path)

    assert _stubbed == {"scan": 1, "freeze": 1}
