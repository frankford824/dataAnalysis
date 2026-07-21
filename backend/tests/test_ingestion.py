from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

import polars as pl

from app.ingestion import _bucket, parse_tabular


def test_csv_ingestion_is_activation_scoped_deduplicated_and_publishable(client, commerce_setup):
    _, headers, store, source = commerce_setup
    csv = "order,transaction_date,sales,refund,platform_fee,advertising_fee,shipping_fee,product_cost\nold,2025-12-30,999,0,0,0,0,0\nA,2026-02-01,100,5,2,3,4,20\nA,2026-02-01,100,5,2,3,4,20\nB,2026-02-02,50,0,1,0,2,10\n"
    upload = lambda: client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": source["id"], "store_id": store["id"]}, files={"file": ("orders.csv", csv.encode(), "text/csv")})
    first = upload()
    assert first.status_code == 200, first.text
    assert first.json()["coverage_start"].startswith("2026-02-01")
    assert first.json()["summary"]["row_count"] == 2
    assert first.json()["summary"]["duplicate_rows_removed"] == 1
    second = upload()
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    confirmed = client.post(f"/api/v1/ingestions/{first.json()['id']}/confirm", headers=headers, json={"accepted": True})
    assert confirmed.status_code == 200, confirmed.text
    published = client.post(f"/api/v1/ingestions/{first.json()['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    locked = client.post(f"/api/v1/ingestions/{first.json()['id']}/lock", headers=headers)
    assert locked.status_code == 200
    query = client.post("/api/v1/certified-query", headers=headers, json={"sql": "select sum(revenue) as revenue, sum(profit) as profit from certified_sales"})
    assert query.status_code == 200, query.text
    assert float(query.json()["rows"][0][0]) == 150.0
    assert float(query.json()["rows"][0][1]) == 103.0


def test_pre_activation_file_requires_explicit_backfill(client, commerce_setup):
    _, headers, store, source = commerce_setup
    csv = b"order,transaction_date,sales\nold,2025-12-01,10\n"
    normal = client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": source["id"], "store_id": store["id"]}, files={"file": ("old.csv", csv, "text/csv")})
    assert normal.status_code == 422
    backfill = client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": source["id"], "store_id": store["id"], "backfill": "true"}, files={"file": ("old.csv", csv, "text/csv")})
    assert backfill.status_code == 200, backfill.text
    assert backfill.json()["is_backfill"] is True


def test_zip_and_excel_parsers():
    csv = b"occurred_at,revenue\n2026-01-01,1\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("one.csv", csv)
        archive.writestr("two.csv", csv)
    assert parse_tabular("bundle.zip", buffer.getvalue()).height == 2
    excel = io.BytesIO()
    pl.DataFrame({"occurred_at": ["2026-01-01"], "revenue": [1]}).write_excel(excel)
    assert parse_tabular("orders.xlsx", excel.getvalue()).height == 1


def test_time_grains_are_content_based():
    instant = datetime(2026, 7, 21, 18, 37, tzinfo=timezone.utc)
    assert _bucket(instant, "hour").isoformat() == "2026-07-21T18:00:00+00:00"
    assert _bucket(instant, "day").isoformat() == "2026-07-21T00:00:00+00:00"
    assert _bucket(instant, "month").isoformat() == "2026-07-01T00:00:00+00:00"
