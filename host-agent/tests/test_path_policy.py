
import pytest

from finance_agent.config import AgentConfig, SourceRoot
from finance_agent.path_policy import (
    UnsafePathError,
    validate_windows_file,
    windows_is_within,
)


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        state_dir=tmp_path,
        source_roots=(
            SourceRoot(
                r"D:\FinanceData\BI", "pbix_asset", (".pbix",)
            ),
            SourceRoot(
                r"D:\FinanceData\结算",
                "settlement",
                (".csv", ".xlsx"),
            ),
        ),
    )


def test_windows_path_must_be_inside_allowed_root(config):
    root = validate_windows_file(
        r"D:\FinanceData\BI\经营.pbix", (), config
    )
    assert root.purpose == "pbix_asset"
    assert windows_is_within(
        r"d:\financedata\BI\经营.pbix",
        r"D:\FinanceData\BI",
    )

    with pytest.raises(UnsafePathError, match="允许读取范围"):
        validate_windows_file(r"D:\财务\学习\教程.pbix", (), config)


@pytest.mark.parametrize(
    "attributes",
    [
        ("Offline",),
        ("Unpinned",),
        ("RecallOnDataAccess",),
        ("RecallOnOpen",),
        ("ReparsePoint",),
    ],
)
def test_onedrive_or_reparse_attributes_are_rejected(config, attributes):
    with pytest.raises(UnsafePathError, match="文件属性不安全|已固定"):
        validate_windows_file(
            r"D:\FinanceData\结算\2026-07.csv",
            attributes,
            config,
        )


def test_pinned_onedrive_file_reparse_is_allowed(config):
    root = validate_windows_file(
        r"D:\FinanceData\BI\经营.pbix",
        ("Archive", "Pinned", "ReparsePoint"),
        config,
    )
    assert root.purpose == "pbix_asset"


def test_path_escape_and_sensitive_extension_are_rejected(config):
    with pytest.raises(UnsafePathError, match="父级逃逸"):
        validate_windows_file(
            r"D:\FinanceData\BI\..\工资\员工.pbix", (), config
        )
    with pytest.raises(UnsafePathError, match="扩展名"):
        validate_windows_file(
            r"D:\FinanceData\结算\secret.db", (), config
        )


def test_explicit_governance_root_can_override_only_path_fragment_exclusion(
    tmp_path,
):
    governance = AgentConfig(
        state_dir=tmp_path,
        source_roots=(
            SourceRoot(
                r"D:\FinanceData\工资\2026",
                "performance_reference",
                (".csv", ".xlsx"),
                allow_excluded_fragments=True,
            ),
        ),
    )
    root = validate_windows_file(
        r"D:\FinanceData\工资\2026\阿里单算\2602.csv",
        ("Archive", "Pinned", "ReparsePoint"),
        governance,
    )
    assert root.purpose == "performance_reference"

    default_guard = AgentConfig(
        state_dir=tmp_path,
        source_roots=(
            SourceRoot(
                r"D:\FinanceData\工资",
                "historical_workspace",
                (".csv", ".xlsx"),
            ),
        ),
    )
    with pytest.raises(UnsafePathError, match="排除规则"):
        validate_windows_file(
            r"D:\FinanceData\工资\2026\阿里单算\2602.csv",
            ("Archive", "Pinned", "ReparsePoint"),
            default_guard,
        )
