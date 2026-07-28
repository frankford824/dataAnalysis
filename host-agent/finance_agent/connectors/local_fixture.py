from __future__ import annotations

import hashlib
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import AgentConfig
from ..models import FileRecord
from ..path_policy import UnsafePathError, validate_local_file
from .base import ReadOnlyConnector


class LocalFixtureConnector(ReadOnlyConnector):
    def __init__(self, config: AgentConfig):
        if config.fixture_root is None:
            raise ValueError("local_fixture 连接器需要 fixture_root")
        self.config = config
        self.root = config.fixture_root.resolve(strict=True)

    def scan(self) -> list[FileRecord]:
        now = time.time()
        records: list[FileRecord] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            try:
                validate_local_file(path, self.root, self.config)
            except (UnsafePathError, OSError):
                continue
            stat = path.stat()
            if now - stat.st_mtime < self.config.stable_for_seconds:
                continue
            relative = path.relative_to(self.root).as_posix()
            purpose = relative.split("/", 1)[0] if "/" in relative else "fixture"
            records.append(
                FileRecord(
                    source_id=self._source_id(relative, stat.st_size, stat.st_mtime_ns),
                    path=str(path),
                    purpose=purpose,
                    extension=path.suffix.casefold(),
                    size=stat.st_size,
                    mtime_utc=datetime.fromtimestamp(
                        stat.st_mtime, UTC
                    ).isoformat().replace("+00:00", "Z"),
                )
            )
        return records

    def materialize(self, record: FileRecord, target: Path) -> Path:
        source = Path(record.path)
        validate_local_file(source, self.root, self.config)
        if source.stat().st_size != record.size:
            raise RuntimeError("文件在扫描后发生变化，拒绝读取")
        if record.size > self.config.max_materialize_bytes:
            raise RuntimeError("文件超过代理允许的单文件读取上限")
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        return target

    def stable_sha256(self, record: FileRecord) -> str:
        source = Path(record.path)
        validate_local_file(source, self.root, self.config)
        if source.stat().st_size != record.size:
            raise RuntimeError("文件在扫描后发生变化，拒绝计算哈希")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _source_id(relative: str, size: int, mtime_ns: int) -> str:
        value = f"{relative.casefold()}|{size}|{mtime_ns}".encode()
        return hashlib.sha256(value).hexdigest()
