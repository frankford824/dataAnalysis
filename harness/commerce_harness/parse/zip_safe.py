from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass
from os import PathLike
from pathlib import Path, PurePosixPath


class UnsafeZipError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SafeZipPolicy:
    max_members: int = 128
    max_total_uncompressed: int = 512 * 1024 * 1024
    max_member_uncompressed: int = 128 * 1024 * 1024
    max_compression_ratio: float = 200.0


@dataclass(frozen=True, slots=True)
class SafeZipMember:
    name: str
    compressed_size: int
    uncompressed_size: int


def inspect_zip(
    source: bytes | str | PathLike[str],
    *,
    policy: SafeZipPolicy | None = None,
) -> tuple[SafeZipMember, ...]:
    selected_policy = policy or SafeZipPolicy()
    with _open_zip(source) as archive:
        members = archive.infolist()
        if len(members) > selected_policy.max_members:
            raise UnsafeZipError("ZIP contains too many members")
        total_size = 0
        names: set[str] = set()
        safe_members: list[SafeZipMember] = []
        for member in members:
            if member.is_dir():
                continue
            _validate_member_path(member.filename)
            normalized_name = member.filename.replace("\\", "/")
            if normalized_name in names:
                raise UnsafeZipError(f"duplicate ZIP member is not allowed: {member.filename}")
            names.add(normalized_name)
            if member.flag_bits & 0x1:
                raise UnsafeZipError(f"encrypted ZIP member is not allowed: {member.filename}")
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise UnsafeZipError(f"symbolic-link ZIP member is not allowed: {member.filename}")
            if member.file_size > selected_policy.max_member_uncompressed:
                raise UnsafeZipError(f"ZIP member is too large: {member.filename}")
            ratio = (
                float("inf")
                if member.compress_size == 0 and member.file_size > 0
                else member.file_size / max(member.compress_size, 1)
            )
            if ratio > selected_policy.max_compression_ratio:
                raise UnsafeZipError(f"ZIP compression ratio is unsafe: {member.filename}")
            total_size += member.file_size
            if total_size > selected_policy.max_total_uncompressed:
                raise UnsafeZipError("ZIP uncompressed total exceeds the configured limit")
            safe_members.append(
                SafeZipMember(
                    name=member.filename,
                    compressed_size=member.compress_size,
                    uncompressed_size=member.file_size,
                )
            )
        return tuple(safe_members)


def read_safe_member(
    source: bytes | str | PathLike[str],
    member_name: str,
    *,
    policy: SafeZipPolicy | None = None,
) -> bytes:
    members = {member.name: member for member in inspect_zip(source, policy=policy)}
    if member_name not in members:
        raise KeyError(member_name)
    with _open_zip(source) as archive, archive.open(member_name, "r") as item:
        data = item.read(members[member_name].uncompressed_size + 1)
    if len(data) != members[member_name].uncompressed_size:
        raise UnsafeZipError(f"ZIP member size changed while reading: {member_name}")
    return data


def _open_zip(source: bytes | str | PathLike[str]) -> zipfile.ZipFile:
    if isinstance(source, bytes):
        return zipfile.ZipFile(io.BytesIO(source), mode="r")
    return zipfile.ZipFile(Path(source), mode="r")


def _validate_member_path(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized)
        or normalized.startswith("//")
    ):
        raise UnsafeZipError(f"unsafe ZIP member path: {name}")
