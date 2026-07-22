from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

import polars as pl

from conftest import TestingSession
from app.ingestion import _bucket, _decimal, parse_tabular
from app.models import CertifiedAggregate, MetricDefinition


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


def test_decimal_amounts_are_exact_and_xlsx_export_is_real(client, commerce_setup):
    _, headers, store, source = commerce_setup
    csv = b"order,transaction_date,sales,refund,platform_fee,advertising_fee,shipping_fee,product_cost\nA,2026-03-01,0.1,0,0,0,0,0\nB,2026-03-01,0.2,0,0,0,0,0\n"
    run = client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        data={"source_definition_id": source["id"], "store_id": store["id"]},
        files={"file": ("decimal.csv", csv, "text/csv")},
    )
    assert run.status_code == 200, run.text
    assert run.json()["summary"]["revenue"] == "0.3000"
    assert client.post(f"/api/v1/ingestions/{run.json()['id']}/confirm", headers=headers, json={"accepted": True}).status_code == 200
    assert client.post(f"/api/v1/ingestions/{run.json()['id']}/publish", headers=headers).status_code == 200
    query = client.post("/api/v1/certified-query", headers=headers, json={"sql": "select sum(revenue) as revenue from certified_sales"})
    assert Decimal(str(query.json()["rows"][0][0])) == Decimal("0.3")
    with TestingSession() as db:
        assert db.query(CertifiedAggregate).filter_by(ingestion_run_id=run.json()["id"]).one().revenue == Decimal("0.3000")
    xlsx = client.get("/api/v1/exports/certified", headers=headers, params={"format": "xlsx", "period": "2026-03"})
    assert xlsx.status_code == 200
    assert xlsx.content.startswith(b"PK\x03\x04")
    assert _decimal("0.1") + _decimal("0.2") == Decimal("0.3000")


def test_file_hash_is_deduplicated_across_source_definitions(client, commerce_setup):
    _, headers, store, source = commerce_setup
    second_source = client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "Alternate orders",
            "status": "active",
            "activation_at": "2026-01-01T00:00:00Z",
            "coverage_time_field": "transaction_date",
            "data_granularity": "day",
            "arrival_frequency": "adhoc",
            "file_types": ["csv"],
            "field_aliases": {"order_id": ["order"], "revenue": ["sales"]},
            "dedupe_keys": ["order_id"],
        },
    ).json()
    data = b"order,transaction_date,sales\nD-1,2026-04-01,12.34\n"
    first = client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": source["id"], "store_id": store["id"]}, files={"file": ("one.csv", data, "text/csv")})
    second = client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": second_source["id"], "store_id": store["id"]}, files={"file": ("renamed.csv", data, "text/csv")})
    assert first.status_code == second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["duplicate_of_run_id"] == first.json()["id"]


def test_publish_requires_the_pinned_complete_semantic_model(client, commerce_setup):
    _, headers, store, source = commerce_setup
    data = b"order,transaction_date,sales\nM-1,2026-05-01,10\n"
    run = client.post("/api/v1/ingestions/upload", headers=headers, data={"source_definition_id": source["id"], "store_id": store["id"]}, files={"file": ("model-gate.csv", data, "text/csv")})
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["semantic_model_id"] and body["model_version"] == 1
    assert client.post(f"/api/v1/ingestions/{body['id']}/confirm", headers=headers, json={"accepted": True}).status_code == 200
    with TestingSession() as db:
        metric = db.query(MetricDefinition).filter_by(enterprise_id=body["enterprise_id"], semantic_model_id=body["semantic_model_id"], key="profit").one()
        db.delete(metric)
        db.commit()
    blocked = client.post(f"/api/v1/ingestions/{body['id']}/publish", headers=headers)
    assert blocked.status_code == 409
    assert "profit" in blocked.text


def test_store_version_keeps_logical_identity(client, tenants):
    from conftest import tenant_headers

    headers = tenant_headers(tenants[0])
    original = client.post("/api/v1/stores", headers=headers, json={"name": "Stable", "status": "active", "activation_at": "2026-01-01T00:00:00Z"}).json()
    changed = client.patch(f"/api/v1/stores/{original['id']}", headers=headers, json={"name": "Stable renamed"})
    assert changed.status_code == 200
    assert changed.json()["logical_id"] == original["logical_id"]


def test_upload_auto_recognition_and_honest_review_queue(client, tenants):
    from conftest import tenant_headers

    headers = tenant_headers(tenants[0])
    store = client.get("/api/v1/stores", headers=headers).json()[0]
    recognized = client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        data={"store_id": store["id"]},
        files={"file": ("orders.csv", b"order,transaction_date,sales\nAUTO-1,2026-06-01,9.99\n", "text/csv")},
    )
    assert recognized.status_code == 200, recognized.text
    assert recognized.json()["source_definition_id"]

    unknown = client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        data={"store_id": store["id"]},
        files={"file": ("unknown.csv", b"when,value\n2026-06-01,10\n", "text/csv")},
    )
    assert unknown.status_code == 409
    detail = unknown.json()["detail"]
    assert detail["code"] == "source_not_recognized"
    assert len(detail["options"]) <= 3
    issues = client.get("/api/v1/issues", headers=headers)
    assert issues.status_code == 200
    assert any(item["id"] == detail["problem_id"] and item["status"] == "open" for item in issues.json())
