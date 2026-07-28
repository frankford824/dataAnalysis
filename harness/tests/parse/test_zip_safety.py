from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from commerce_harness.parse import ArchiveRoute, SafeZipPolicy, TemplateRouter, UnsafeZipError
from commerce_harness.parse.zip_safe import inspect_zip


def _zip_bytes(files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w", compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return target.getvalue()


@pytest.mark.parametrize("name", ["../escape.csv", "/absolute.csv", "C:\\escape.csv"])
def test_zip_rejects_path_traversal_and_absolute_paths(name: str) -> None:
    archive = _zip_bytes({name: b"a,b\n1,2\n"})
    with pytest.raises(UnsafeZipError):
        inspect_zip(archive)


def test_zip_rejects_member_and_total_limits() -> None:
    archive = _zip_bytes({"large.csv": b"x" * 100}, compression=zipfile.ZIP_STORED)
    with pytest.raises(UnsafeZipError):
        inspect_zip(
            archive,
            policy=SafeZipPolicy(
                max_members=2,
                max_total_uncompressed=50,
                max_member_uncompressed=200,
                max_compression_ratio=200,
            ),
        )


def test_zip_rejects_duplicate_member_names() -> None:
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("orders.csv", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("orders.csv", b"second")
    with pytest.raises(UnsafeZipError, match="duplicate"):
        inspect_zip(target.getvalue())


def test_zip_routes_every_supported_member_and_reports_unmatched() -> None:
    archive = _zip_bytes(
        {
            "orders.csv": (
                "子订单编号,订单创建时间,买家实际支付金额\nA-1,2026-02-01,10\n"
            ).encode(),
            "advertising.csv": "数据日期,消耗,广告计划\n2026-02-01,1,计划A\n".encode(),
            "readme.txt": b"synthetic fixture",
        }
    )

    result = TemplateRouter().route_bytes("bundle.zip", archive)

    assert isinstance(result, ArchiveRoute)
    assert {entry.source_kind.value for entry in result.entries} == {
        "order",
        "advertising",
    }
    assert result.unmatched_members == ("readme.txt",)


def test_zip_with_no_supported_template_is_rejected() -> None:
    archive = _zip_bytes({"unknown.csv": b"alpha,beta\n1,2\n"})
    with pytest.raises(Exception, match="no uniquely routable"):
        TemplateRouter().route_bytes("unknown.zip", archive)
