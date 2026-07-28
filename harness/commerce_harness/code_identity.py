from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

COMPUTE_CODE_PATHS = (
    "harness/commerce_harness",
    "harness/pyproject.toml",
    "host-agent/finance_agent",
    "host-agent/pyproject.toml",
)


@dataclass(frozen=True, slots=True)
class CodeIdentity:
    commit_sha: str
    worktree_sha256: str | None

    @property
    def value(self) -> str:
        if self.worktree_sha256 is None:
            return self.commit_sha
        return f"{self.commit_sha}+dirty.{self.worktree_sha256[:16]}"

    @property
    def committed(self) -> bool:
        return self.worktree_sha256 is None


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        stderr=subprocess.STDOUT,
    )


def repository_root(start: Path | None = None) -> Path:
    location = (start or Path(__file__)).resolve()
    try:
        raw = _git(location.parent, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("无法定位 Harness 所在 Git 仓库") from exc
    return Path(raw.decode("utf-8").strip())


def resolve_code_identity(start: Path | None = None) -> CodeIdentity:
    try:
        repo = repository_root(start)
    except RuntimeError:
        identity_path = Path(
            os.getenv("FA_CODE_IDENTITY_FILE", "/app/.fa-code-identity.json")
        )
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "无法定位 Git 仓库，且容器代码身份文件不可用"
            ) from exc
        commit_sha = str(payload.get("commit_sha") or "").strip()
        worktree_sha256 = str(payload.get("worktree_sha256") or "").strip()
        if not commit_sha or len(worktree_sha256) != 64:
            raise RuntimeError("容器代码身份文件无效") from None
        return CodeIdentity(
            commit_sha=commit_sha,
            worktree_sha256=worktree_sha256,
        )
    commit_sha = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *COMPUTE_CODE_PATHS,
    )
    if not status:
        return CodeIdentity(commit_sha=commit_sha, worktree_sha256=None)

    digest = hashlib.sha256()
    digest.update(status)
    tracked_diff = _git(
        repo,
        "diff",
        "--binary",
        "HEAD",
        "--",
        *COMPUTE_CODE_PATHS,
    )
    digest.update(tracked_diff)
    for line in status.decode("utf-8", errors="surrogateescape").splitlines():
        if not line.startswith("?? "):
            continue
        relative = line[3:]
        path = repo / relative
        if path.is_file():
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(path.read_bytes())
    return CodeIdentity(commit_sha=commit_sha, worktree_sha256=digest.hexdigest())


def require_committed_code(start: Path | None = None) -> CodeIdentity:
    identity = resolve_code_identity(start)
    if not identity.committed:
        raise RuntimeError(
            "冻结基线或盲测前必须先提交 Harness 代码；当前代码仍有未提交改动"
        )
    return identity
