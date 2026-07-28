from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath

from .config import AgentConfig, SourceRoot

UNSAFE_ATTRIBUTES = frozenset(
    {"Offline", "Unpinned", "RecallOnOpen", "RecallOnDataAccess"}
)
TEMPORARY_NAMES = re.compile(r"(^~\$)|(\.tmp$)|(\.partial$)|(\.crdownload$)", re.IGNORECASE)


class UnsafePathError(ValueError):
    pass


def _windows_normalized(path: str) -> str:
    candidate = str(PureWindowsPath(path))
    if ".." in PureWindowsPath(candidate).parts:
        raise UnsafePathError("路径包含父级逃逸")
    return candidate.rstrip("\\").casefold()


def windows_is_within(path: str, root: str) -> bool:
    normalized_path = _windows_normalized(path)
    normalized_root = _windows_normalized(root)
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root + "\\"
    )


def find_source_root(path: str, roots: tuple[SourceRoot, ...]) -> SourceRoot:
    matches = [root for root in roots if windows_is_within(path, root.path)]
    if not matches:
        raise UnsafePathError("路径不在允许读取范围")
    return max(matches, key=lambda item: len(item.path))


def validate_windows_file(
    path: str,
    attributes: tuple[str, ...],
    config: AgentConfig,
) -> SourceRoot:
    root = find_source_root(path, config.source_roots)
    normalized = _windows_normalized(path)
    if (
        not root.allow_excluded_fragments
        and any(
            fragment.casefold() in normalized
            for fragment in config.excluded_fragments
        )
    ):
        raise UnsafePathError("路径命中明确排除规则")
    suffix = PureWindowsPath(path).suffix.casefold()
    if suffix not in {item.casefold() for item in root.extensions}:
        raise UnsafePathError("扩展名不在该来源允许范围")
    if suffix in {item.casefold() for item in config.sensitive_extensions}:
        raise UnsafePathError("扩展名属于敏感文件")
    if TEMPORARY_NAMES.search(PureWindowsPath(path).name):
        raise UnsafePathError("临时文件不采集")
    unsafe = UNSAFE_ATTRIBUTES.intersection(attributes)
    if unsafe:
        raise UnsafePathError(f"文件属性不安全: {', '.join(sorted(unsafe))}")
    if "ReparsePoint" in attributes and "Pinned" not in attributes:
        raise UnsafePathError("仅允许已固定到本机的 OneDrive 文件 reparse point")
    return root


def validate_local_file(path: Path, root: Path, config: AgentConfig) -> None:
    resolved_root = root.resolve(strict=True)
    if path.is_symlink():
        raise UnsafePathError("符号链接不采集")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError("路径逃逸本地允许目录") from exc
    if any(part.startswith(".") for part in resolved.relative_to(resolved_root).parts):
        raise UnsafePathError("隐藏路径不采集")
    suffix = resolved.suffix.casefold()
    if suffix not in set(DEFAULT_LOCAL_EXTENSIONS):
        raise UnsafePathError("本地扩展名不允许")
    if suffix in {item.casefold() for item in config.sensitive_extensions}:
        raise UnsafePathError("本地敏感扩展名不采集")
    if TEMPORARY_NAMES.search(resolved.name):
        raise UnsafePathError("本地临时文件不采集")
    if not os.access(resolved, os.R_OK):
        raise UnsafePathError("本地文件不可读")


DEFAULT_LOCAL_EXTENSIONS = (".pbix", ".csv", ".xlsx", ".xls", ".xlsm")
