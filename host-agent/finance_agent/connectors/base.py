from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import FileRecord


class ReadOnlyConnector(ABC):
    @abstractmethod
    def scan(self) -> list[FileRecord]:
        raise NotImplementedError

    @abstractmethod
    def materialize(self, record: FileRecord, target: Path) -> Path:
        """只读获取文件到代理自己的临时目录。"""
        raise NotImplementedError

    def stable_sha256(self, record: FileRecord) -> str:
        raise NotImplementedError
