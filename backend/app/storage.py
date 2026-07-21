from __future__ import annotations

import io
from pathlib import Path, PurePosixPath
from typing import Protocol

import boto3

from .config import get_settings


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalObjectStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        clean = PurePosixPath(key)
        if clean.is_absolute() or ".." in clean.parts:
            raise ValueError("unsafe object key")
        target = (self.root / Path(*clean.parts)).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError("unsafe object key")
        return target

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStorage:
    def __init__(self):
        settings = get_settings()
        self.raw_bucket = settings.s3_raw_bucket
        self.intermediate_bucket = settings.s3_intermediate_bucket
        self.export_bucket = settings.s3_export_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )

    def _bucket(self, key: str) -> str:
        if "/normalized/" in key or "/intermediate/" in key:
            return self.intermediate_bucket
        if "/exports/" in key:
            return self.export_bucket
        return self.raw_bucket

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self._bucket(key), Key=key, Body=io.BytesIO(data), ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self._bucket(key), Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self._bucket(key), Key=key)
            return True
        except self.client.exceptions.ClientError:
            return False


def get_storage() -> ObjectStorage:
    settings = get_settings()
    if settings.object_storage_backend == "s3":
        return S3ObjectStorage()
    return LocalObjectStorage(settings.object_storage_path)
