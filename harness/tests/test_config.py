from pathlib import Path

import pytest
from pydantic import ValidationError

from commerce_harness.config import HarnessConfig, SourceScope, load_config
from commerce_harness.workbench import initialize, require_initialized


def test_workspace_is_outside_git_and_initializes(tmp_path: Path) -> None:
    config = load_config(workspace=tmp_path / "workbench")
    created = initialize(config)

    assert created.root == (tmp_path / "workbench").resolve()
    assert (created.root / ".fa-workbench.json").is_file()
    assert require_initialized(config) == created


def test_workspace_cannot_be_initialized_inside_git_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    config = load_config(workspace=repository / "customer-workbench")

    with pytest.raises(ValueError, match="Git 仓库"):
        initialize(config)


def test_external_redaction_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError, match="脱敏不能关闭"):
        HarnessConfig.model_validate({"llm": {"redaction_required": False}})


def test_source_scope_accepts_legacy_single_shop() -> None:
    scope = SourceScope.model_validate({"shop": " 测试店铺 ", "periods": ["2602"]})

    assert scope.bound_shops == ("测试店铺",)
    assert scope.shops == []
    assert scope.include_all_discovered is False


def test_source_scope_accepts_explicit_multiple_shops_and_all_period_discovery() -> None:
    scope = SourceScope.model_validate({"shops": ["一店", "二店", "一店"], "periods": []})

    assert scope.bound_shops == ("一店", "二店")
    assert scope.periods == []


@pytest.mark.parametrize(
    "scope",
    [
        {"shop": "一店", "shops": ["二店"]},
        {"shops": ["一店"], "include_all_discovered": True},
    ],
)
def test_source_scope_rejects_conflicting_binding_modes(scope: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="不能同时"):
        SourceScope.model_validate(scope)
