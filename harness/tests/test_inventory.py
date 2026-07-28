from __future__ import annotations

import json

from finance_agent.models import FileRecord

from commerce_harness.config import HarnessConfig
from commerce_harness.inventory import scan_inventory
from commerce_harness.workbench import initialize


class FakeConnector:
    def scan_detailed(self):
        return [
            FileRecord(
                source_id="orders-2602",
                path=r"D:\source\测试店铺\2602订单.csv",
                purpose="orders",
                extension=".csv",
                size=10,
                mtime_utc="2026-03-01T00:00:00Z",
            ),
            FileRecord(
                source_id="other-shop",
                path=r"D:\source\另一店铺\2602订单.csv",
                purpose="orders",
                extension=".csv",
                size=10,
                mtime_utc="2026-03-01T00:00:00Z",
            ),
            FileRecord(
                source_id="history-2603",
                path=r"C:\history\测试店铺\2603历史结果.xlsx",
                purpose="historical_workspace",
                extension=".xlsx",
                size=10,
                mtime_utc="2026-04-01T00:00:00Z",
            ),
            FileRecord(
                source_id="pbix",
                path=r"D:\BI\测试店铺经营.pbix",
                purpose="pbix_asset",
                extension=".pbix",
                size=10,
                mtime_utc="2026-04-01T00:00:00Z",
            ),
            FileRecord(
                source_id="rule-corpus",
                path=r"C:\temp\fa_sample\formula-notes.txt",
                purpose="rule_corpus",
                extension=".txt",
                size=10,
                mtime_utc="2026-04-01T00:00:00Z",
            ),
        ], []


def test_inventory_keeps_full_metadata_but_scopes_candidates(monkeypatch, tmp_path):
    config = HarnessConfig.model_validate(
        {
            "workspace": {"root": tmp_path / "workbench"},
            "source": {
                "scope": {
                    "shop": "测试店铺",
                    "periods": ["2602", "2603", "2604"],
                }
            },
        }
    )
    workbench = initialize(config)
    monkeypatch.setattr(
        "commerce_harness.inventory._connector",
        lambda _config: FakeConnector(),
    )

    result = scan_inventory(config, workbench)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.record_count == 5
    assert result.candidate_count == 4
    assert payload["candidate_source_ids"] == [
        "orders-2602",
        "history-2603",
        "pbix",
        "rule-corpus",
    ]


def test_inventory_scopes_multiple_shops_and_never_binds_by_period_only(
    monkeypatch,
    tmp_path,
):
    config = HarnessConfig.model_validate(
        {
            "workspace": {"root": tmp_path / "workbench"},
            "source": {
                "scope": {
                    "shops": ["测试店铺", "另一店铺"],
                    "periods": [],
                }
            },
        }
    )
    workbench = initialize(config)
    monkeypatch.setattr(
        "commerce_harness.inventory._connector",
        lambda _config: FakeConnector(),
    )

    result = scan_inventory(config, workbench)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.candidate_count == 5
    assert payload["candidate_source_ids"] == [
        "orders-2602",
        "other-shop",
        "history-2603",
        "pbix",
        "rule-corpus",
    ]


def test_inventory_all_discovered_stays_inside_configured_roots(
    monkeypatch,
    tmp_path,
):
    config = HarnessConfig.model_validate(
        {
            "workspace": {"root": tmp_path / "workbench"},
            "source": {
                "scope": {
                    "include_all_discovered": True,
                    "periods": [],
                },
                "roots": [
                    {
                        "path": r"D:\source",
                        "purpose": "orders",
                        "extensions": [".csv"],
                    },
                    {
                        "path": r"D:\BI",
                        "purpose": "pbix_asset",
                        "extensions": [".pbix"],
                    },
                ],
            },
        }
    )
    workbench = initialize(config)
    monkeypatch.setattr(
        "commerce_harness.inventory._connector",
        lambda _config: FakeConnector(),
    )

    result = scan_inventory(config, workbench)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.candidate_count == 4
    assert payload["candidate_source_ids"] == [
        "orders-2602",
        "other-shop",
        "pbix",
        "rule-corpus",
    ]


def test_inventory_without_explicit_scope_does_not_expand_customer_data(
    monkeypatch,
    tmp_path,
):
    config = HarnessConfig.model_validate({"workspace": {"root": tmp_path / "workbench"}})
    workbench = initialize(config)
    monkeypatch.setattr(
        "commerce_harness.inventory._connector",
        lambda _config: FakeConnector(),
    )

    result = scan_inventory(config, workbench)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.candidate_count == 1
    assert payload["candidate_source_ids"] == ["rule-corpus"]


def test_inventory_includes_annual_store_file_then_uses_content_time(
    monkeypatch,
    tmp_path,
):
    class AnnualConnector:
        def scan_detailed(self):
            return [
                FileRecord(
                    source_id="shipping-2026",
                    path=r"D:\shipping\店铺\测试店铺\26年发货运费.xlsx",
                    purpose="shipping",
                    extension=".xlsx",
                    size=10,
                    mtime_utc="2026-07-20T00:00:00Z",
                ),
                FileRecord(
                    source_id="shipping-2025",
                    path=r"D:\shipping\店铺\测试店铺\25年发货运费.xlsx",
                    purpose="shipping",
                    extension=".xlsx",
                    size=10,
                    mtime_utc="2026-01-13T00:00:00Z",
                ),
            ], []

    config = HarnessConfig.model_validate(
        {
            "workspace": {"root": tmp_path / "workbench"},
            "source": {
                "scope": {
                    "shop": "测试店铺",
                    "periods": ["2602", "2603", "2604"],
                }
            },
        }
    )
    workbench = initialize(config)
    monkeypatch.setattr(
        "commerce_harness.inventory._connector",
        lambda _config: AnnualConnector(),
    )

    result = scan_inventory(config, workbench)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.candidate_count == 1
    assert payload["candidate_source_ids"] == ["shipping-2026"]
