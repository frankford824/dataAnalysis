from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from commerce_harness.code_identity import (
    require_committed_code,
    resolve_code_identity,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_code_identity_distinguishes_clean_and_dirty_harness(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "harness" / "commerce_harness"
    package.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    clean = resolve_code_identity(package / "module.py")
    assert clean.committed is True
    assert clean.value == clean.commit_sha

    (package / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty = resolve_code_identity(package / "module.py")
    assert dirty.committed is False
    assert dirty.value.startswith(f"{dirty.commit_sha}+dirty.")
    with pytest.raises(RuntimeError, match="必须先提交"):
        require_committed_code(package / "module.py")


def test_web_only_change_does_not_invalidate_compute_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "harness" / "commerce_harness"
    web = repo / "harness" / "web" / "src"
    package.mkdir(parents=True)
    web.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (web / "App.tsx").write_text("export default null;\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    before = resolve_code_identity(package / "module.py")
    (web / "App.tsx").write_text("export default true;\n", encoding="utf-8")
    after = resolve_code_identity(package / "module.py")

    assert before.committed is True
    assert after.committed is True
    assert after.value == before.value
