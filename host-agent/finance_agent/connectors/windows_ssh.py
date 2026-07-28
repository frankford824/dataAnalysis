from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from ..config import AgentConfig, resolve_ssh_binary
from ..models import FileRecord
from ..path_policy import UnsafePathError, validate_windows_file
from .base import ReadOnlyConnector

SSH_ALIAS = re.compile(r"^[A-Za-z0-9_.-]+$")


class SshCommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScanIssue:
    path: str
    purpose: str
    reason: str
    attributes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attributes"] = list(self.attributes)
        return payload


def encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def parse_json_records(stdout: str) -> list[dict[str, Any]]:
    text = stdout.lstrip("\ufeff").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SshCommandError("finance-win 返回了无法解析的 JSON") from exc
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise SshCommandError("finance-win JSON 顶层必须是对象或对象数组")
    return payload


class WindowsSshConnector(ReadOnlyConnector):
    def __init__(self, config: AgentConfig):
        if not SSH_ALIAS.fullmatch(config.ssh_alias):
            raise ValueError("SSH alias 只能包含字母、数字、点、下划线和连字符")
        self.config = config

    def scan(self) -> list[FileRecord]:
        records, _issues = self.scan_detailed()
        return records

    def scan_detailed(self) -> tuple[list[FileRecord], list[ScanIssue]]:
        records: list[FileRecord] = []
        issues: list[ScanIssue] = []
        for root in self.config.source_roots:
            for item in self._scan_root(root.path):
                record = self._record_from_remote(item, root.purpose)
                try:
                    validate_windows_file(record.path, record.attributes, self.config)
                except UnsafePathError as exc:
                    issues.append(
                        ScanIssue(
                            path=record.path,
                            purpose=record.purpose,
                            reason=str(exc),
                            attributes=record.attributes,
                        )
                    )
                    continue
                if not self._is_stable(record.mtime_utc):
                    issues.append(
                        ScanIssue(
                            path=record.path,
                            purpose=record.purpose,
                            reason="文件仍在稳定等待期内",
                            attributes=record.attributes,
                        )
                    )
                    continue
                records.append(record)
        records.extend(self._scan_recent_shortcuts())
        unique: dict[tuple[str, str], FileRecord] = {}
        for record in records:
            unique[(record.path.casefold(), record.purpose)] = record
        return (
            sorted(
                unique.values(),
                key=lambda item: (item.purpose, item.path.casefold()),
            ),
            sorted(
                issues,
                key=lambda item: (item.purpose, item.path.casefold(), item.reason),
            ),
        )

    def materialize(self, record: FileRecord, target: Path) -> Path:
        validate_windows_file(record.path, record.attributes, self.config)
        if record.size > self.config.max_materialize_bytes:
            raise RuntimeError("文件超过代理允许的单文件读取上限")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        path_b64 = base64.b64encode(record.path.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path_b64}'))
$item = Get-Item -LiteralPath $path -Force
$attrs = [Int64]$item.Attributes
if (($attrs -band 0x1000) -ne 0 -or ($attrs -band 0x40000) -ne 0 -or ($attrs -band 0x100000) -ne 0 -or ($attrs -band 0x400000) -ne 0) {{ throw 'offline or recall file rejected' }}
if (($attrs -band 0x400) -ne 0 -and ($attrs -band 0x80000) -eq 0) {{ throw 'unpinned reparse file rejected' }}
if ($item.Length -ne {record.size}) {{ throw 'file changed after scan' }}
$inputStream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {{
  $outputStream = [Console]::OpenStandardOutput()
  $buffer = New-Object byte[] 1048576
  while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {{
    $outputStream.Write($buffer, 0, $read)
  }}
  $outputStream.Flush()
}} finally {{
  $inputStream.Dispose()
}}
"""
        command = self._ssh_command(script)
        try:
            with target.open("xb") as writer:
                process = subprocess.run(
                    command,
                    stdout=writer,
                    stderr=subprocess.PIPE,
                    timeout=max(60, self.config.materialize_timeout_seconds),
                    check=False,
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if process.returncode != 0:
            target.unlink(missing_ok=True)
            error = process.stderr.decode("utf-8", errors="replace")[-1000:]
            raise SshCommandError(f"只读流式读取失败: {error}")
        if target.stat().st_size != record.size:
            target.unlink(missing_ok=True)
            raise SshCommandError("读取后的文件大小与扫描记录不一致")
        return target

    def stat_record(self, record: FileRecord) -> FileRecord:
        """Re-read one known path without enumerating or recalling placeholders."""

        validate_windows_file(record.path, record.attributes, self.config)
        path_b64 = base64.b64encode(record.path.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path_b64}'))
$item = Get-Item -LiteralPath $path -Force
$fileAttrs = [Int64]$item.Attributes
$attributeNames = New-Object System.Collections.Generic.List[string]
if (($fileAttrs -band 0x20) -ne 0) {{ $attributeNames.Add('Archive') }}
if (($fileAttrs -band 0x400) -ne 0) {{ $attributeNames.Add('ReparsePoint') }}
if (($fileAttrs -band 0x1000) -ne 0) {{ $attributeNames.Add('Offline') }}
if (($fileAttrs -band 0x40000) -ne 0) {{ $attributeNames.Add('RecallOnOpen') }}
if (($fileAttrs -band 0x80000) -ne 0) {{ $attributeNames.Add('Pinned') }}
if (($fileAttrs -band 0x100000) -ne 0) {{ $attributeNames.Add('Unpinned') }}
if (($fileAttrs -band 0x400000) -ne 0) {{ $attributeNames.Add('RecallOnDataAccess') }}
[PSCustomObject]@{{
  path = $item.FullName
  size = [Int64]$item.Length
  mtime_utc = $item.LastWriteTimeUtc.ToString('o')
  extension = $item.Extension.ToLowerInvariant()
  attributes = @($attributeNames)
}} | ConvertTo-Json -Compress -Depth 4
"""
        items = parse_json_records(self._run_text(script))
        if len(items) != 1:
            raise SshCommandError("精确文件状态查询没有返回唯一结果")
        current = self._record_from_remote(items[0], record.purpose)
        validate_windows_file(current.path, current.attributes, self.config)
        if not self._is_stable(current.mtime_utc):
            raise SshCommandError("文件仍在稳定等待期内")
        return current

    def iter_chunks(
        self,
        record: FileRecord,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        """Stream one source over stdout without assembling it in memory."""

        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        validate_windows_file(record.path, record.attributes, self.config)
        if record.size > self.config.max_materialize_bytes:
            raise RuntimeError("文件超过代理允许的单文件读取上限")
        path_b64 = base64.b64encode(record.path.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path_b64}'))
$item = Get-Item -LiteralPath $path -Force
$attrs = [Int64]$item.Attributes
if (($attrs -band 0x1000) -ne 0 -or ($attrs -band 0x40000) -ne 0 -or ($attrs -band 0x100000) -ne 0 -or ($attrs -band 0x400000) -ne 0) {{ throw 'offline or recall file rejected' }}
if (($attrs -band 0x400) -ne 0 -and ($attrs -band 0x80000) -eq 0) {{ throw 'unpinned reparse file rejected' }}
if ($item.Length -ne {record.size}) {{ throw 'file changed after scan' }}
$inputStream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {{
  $outputStream = [Console]::OpenStandardOutput()
  $buffer = New-Object byte[] 1048576
  while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {{
    $outputStream.Write($buffer, 0, $read)
  }}
  $outputStream.Flush()
}} finally {{
  $inputStream.Dispose()
}}
"""
        process = subprocess.Popen(
            self._ssh_command(script),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            while chunk := process.stdout.read(chunk_size):
                yield chunk
            return_code = process.wait(
                timeout=max(60, self.config.materialize_timeout_seconds)
            )
            if return_code != 0:
                error = process.stderr.read().decode("utf-8", errors="replace")[-1000:]
                raise SshCommandError(f"只读流式读取失败: {error}")
        except Exception:
            process.kill()
            process.wait()
            raise

    def stable_sha256(self, record: FileRecord) -> str:
        validate_windows_file(record.path, record.attributes, self.config)
        path_b64 = base64.b64encode(record.path.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path_b64}'))
$item = Get-Item -LiteralPath $path -Force
if ($item.Length -ne {record.size}) {{ throw 'file changed after scan' }}
(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
"""
        return self._run_text(script).strip().casefold()

    def _scan_root(self, root: str) -> list[dict[str, Any]]:
        root_b64 = base64.b64encode(root.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{root_b64}'))
$result = New-Object System.Collections.Generic.List[object]
$stack = New-Object System.Collections.Generic.Stack[string]
$stack.Push($root)
while ($stack.Count -gt 0) {{
    $current = $stack.Pop()
  Get-ChildItem -LiteralPath $current -Force -ErrorAction SilentlyContinue | ForEach-Object {{
    if ($_.PSIsContainer) {{
      $directoryAttrs = [Int64]$_.Attributes
      $isReparse = ($directoryAttrs -band 0x400) -ne 0
      $isPinned = ($directoryAttrs -band 0x80000) -ne 0
      $isUnsafeCloud = (($directoryAttrs -band 0x1000) -ne 0) -or (($directoryAttrs -band 0x100000) -ne 0) -or (($directoryAttrs -band 0x400000) -ne 0)
      if (-not $isReparse -or ($isPinned -and -not $isUnsafeCloud)) {{
        $stack.Push($_.FullName)
      }}
    }} else {{
      $fileAttrs = [Int64]$_.Attributes
      $attributeNames = New-Object System.Collections.Generic.List[string]
      if (($fileAttrs -band 0x20) -ne 0) {{ $attributeNames.Add('Archive') }}
      if (($fileAttrs -band 0x400) -ne 0) {{ $attributeNames.Add('ReparsePoint') }}
      if (($fileAttrs -band 0x1000) -ne 0) {{ $attributeNames.Add('Offline') }}
      if (($fileAttrs -band 0x40000) -ne 0) {{ $attributeNames.Add('RecallOnOpen') }}
      if (($fileAttrs -band 0x80000) -ne 0) {{ $attributeNames.Add('Pinned') }}
      if (($fileAttrs -band 0x100000) -ne 0) {{ $attributeNames.Add('Unpinned') }}
      if (($fileAttrs -band 0x400000) -ne 0) {{ $attributeNames.Add('RecallOnDataAccess') }}
      $result.Add([PSCustomObject]@{{
        path = $_.FullName
        size = [Int64]$_.Length
        mtime_utc = $_.LastWriteTimeUtc.ToString('o')
        extension = $_.Extension.ToLowerInvariant()
        attributes = @($attributeNames)
      }})
    }}
  }}
}}
$result | ConvertTo-Json -Compress -Depth 4
"""
        # 属性枚举在不同 PowerShell 版本行为不一致，因此 Python 侧还会保守校验。
        return parse_json_records(self._run_text(script))

    def _scan_recent_shortcuts(self) -> list[FileRecord]:
        root_b64 = base64.b64encode(
            self.config.recent_shortcuts_root.encode("utf-8")
        ).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{root_b64}'))
$shell = New-Object -ComObject WScript.Shell
$items = Get-ChildItem -LiteralPath $root -Filter '*.pbix.lnk' -File -Force -ErrorAction SilentlyContinue |
  ForEach-Object {{
    $target = $shell.CreateShortcut($_.FullName).TargetPath
    if ($target -and (Test-Path -LiteralPath $target -PathType Leaf)) {{
      $targetItem = Get-Item -LiteralPath $target -Force
      $fileAttrs = [Int64]$targetItem.Attributes
      $attributeNames = New-Object System.Collections.Generic.List[string]
      if (($fileAttrs -band 0x20) -ne 0) {{ $attributeNames.Add('Archive') }}
      if (($fileAttrs -band 0x400) -ne 0) {{ $attributeNames.Add('ReparsePoint') }}
      if (($fileAttrs -band 0x1000) -ne 0) {{ $attributeNames.Add('Offline') }}
      if (($fileAttrs -band 0x40000) -ne 0) {{ $attributeNames.Add('RecallOnOpen') }}
      if (($fileAttrs -band 0x80000) -ne 0) {{ $attributeNames.Add('Pinned') }}
      if (($fileAttrs -band 0x100000) -ne 0) {{ $attributeNames.Add('Unpinned') }}
      if (($fileAttrs -band 0x400000) -ne 0) {{ $attributeNames.Add('RecallOnDataAccess') }}
      [PSCustomObject]@{{
        shortcut_path = $_.FullName
        path = $targetItem.FullName
        size = [Int64]$targetItem.Length
        mtime_utc = $targetItem.LastWriteTimeUtc.ToString('o')
        extension = $targetItem.Extension.ToLowerInvariant()
        attributes = @($attributeNames)
      }}
    }}
  }}
@($items) | ConvertTo-Json -Compress -Depth 4
"""
        records: list[FileRecord] = []
        for item in parse_json_records(self._run_text(script)):
            try:
                root = validate_windows_file(
                    str(item["path"]),
                    tuple(str(value) for value in item.get("attributes", [])),
                    self.config,
                )
            except UnsafePathError:
                continue
            records.append(
                self._record_from_remote(
                    item,
                    "recent_pbix",
                    recent_target=str(item.get("shortcut_path") or ""),
                    source_purpose=root.purpose,
                )
            )
        return records

    def _record_from_remote(
        self,
        item: dict[str, Any],
        purpose: str,
        recent_target: str | None = None,
        source_purpose: str | None = None,
    ) -> FileRecord:
        path = str(item["path"])
        size = int(item["size"])
        mtime = str(item["mtime_utc"])
        source_value = (
            f"{path.casefold()}|{size}|{mtime}|{source_purpose or purpose}".encode()
        )
        attributes_raw = item.get("attributes") or []
        if isinstance(attributes_raw, str):
            attributes_raw = [part.strip() for part in attributes_raw.split(",")]
        return FileRecord(
            source_id=hashlib.sha256(source_value).hexdigest(),
            path=path,
            purpose=purpose,
            extension=str(
                item.get("extension") or PureWindowsPath(path).suffix
            ).casefold(),
            size=size,
            mtime_utc=mtime,
            attributes=tuple(str(value) for value in attributes_raw),
            recent_target=recent_target,
        )

    def _is_stable(self, mtime_utc: str) -> bool:
        parsed = datetime.fromisoformat(mtime_utc)
        return time.time() - parsed.timestamp() >= self.config.stable_for_seconds

    def _run_text(self, script: str) -> str:
        process = subprocess.run(
            self._ssh_command(script),
            capture_output=True,
            timeout=max(30, self.config.request_timeout_seconds),
            check=False,
        )
        if process.returncode != 0:
            error = process.stderr.decode("utf-8", errors="replace")[-1000:]
            raise SshCommandError(f"finance-win 只读命令失败: {error}")
        return process.stdout.decode("utf-8-sig", errors="strict")

    def _ssh_command(self, script: str) -> list[str]:
        script = (
            "$utf8 = [Text.UTF8Encoding]::new($false)\n"
            "$OutputEncoding = $utf8\n"
            "[Console]::OutputEncoding = $utf8\n"
            + script
        )
        remote = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -EncodedCommand {encode_powershell(script)}"
        )
        return [
            resolve_ssh_binary(self.config.ssh_binary),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=12",
            self.config.ssh_alias,
            remote,
        ]
