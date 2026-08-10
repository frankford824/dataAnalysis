"""Cross-thread and cross-process transactions for model configuration."""

from __future__ import annotations

import hashlib
import tempfile
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator, ParamSpec, TypeVar

from .loader import ModelError

P = ParamSpec("P")
R = TypeVar("R")

_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_local = threading.local()


def model_revision(model_dir: str | Path) -> str:
    """Hash all model YAML bytes so stale clients cannot overwrite newer edits."""
    root = Path(model_dir).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.yaml")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def assert_revision(model_dir: str | Path, expected: str | None) -> None:
    if expected is None:
        return
    current = model_revision(model_dir)
    if current != expected:
        raise ModelError(
            "模型在你试跑之后已被其他操作改过。为避免覆盖新配置，本次没有落库；"
            "请刷新页面、重新试跑后再提交。"
        )


def _lock_file(root: Path):
    key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    directory = Path(tempfile.gettempdir()) / "ledger-model-locks"
    directory.mkdir(parents=True, exist_ok=True)
    return (directory / f"{key}.lock").open("a+b")


def _file_lock(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows deployment
        import msvcrt
        fh.seek(0)
        if fh.read(1) == b"":
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _file_unlock(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows deployment
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def model_lock(model_dir: str | Path) -> Iterator[None]:
    """Serialize the whole read-modify-validate-or-rollback transaction."""
    root = Path(model_dir).resolve()
    key = str(root)
    with _guard:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    with thread_lock:
        depths = getattr(_local, "depths", {})
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        _local.depths = depths
        fh = None
        try:
            if depth == 0:
                fh = _lock_file(root)
                _file_lock(fh)
            yield
        finally:
            depths[key] -= 1
            if depths[key] == 0:
                del depths[key]
            if fh is not None:
                _file_unlock(fh)
                fh.close()


def locked_model(fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap config mutators whose first argument is the model directory."""
    @wraps(fn)
    def wrapped(model_dir: str | Path, *args: P.args, **kwargs: P.kwargs) -> R:
        with model_lock(model_dir):
            return fn(model_dir, *args, **kwargs)
    return wrapped  # type: ignore[return-value]
