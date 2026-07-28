from __future__ import annotations

from decimal import Decimal

from conftest import TestingSession
from app.models import CertifiedAggregate, CrossSourceReconciliation, IngestionRun, NormalizedRecord


def _source(client, headers, name: str, *, kind: str = "orders", mode: str = "monthly_snapshot", validations=None):
    response = client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": name,
            "status": "active",
            "activation_at": "2026-01-01T00:00:00Z",
            "coverage_time_field": "date",
            "data_granularity": "month",
            "arrival_frequency": "monthly",
            "file_types": ["csv"],
            "recognition": {"required_headers": ["date", "event_type"]},
            "field_aliases": {
                "occurred_at": ["date"],
                "order_id": ["order"],
                "revenue": ["sales"],
                "refund": ["refund"],
                "platform_fee": ["platform_fee"],
                "advertising_fee": ["advertising_fee"],
                "shipping_fee": ["shipping_fee"],
                "product_cost": ["cost"],
            },
            "dedupe_keys": ["order_id"],
            "validations": validations or [],
            "import_mode": mode,
            "source_kind": kind,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client, headers, source_id: str, store_id: str, name: str, content: str):
    response = client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        data={"source_definition_id": source_id, "store_id": store_id},
        files={"file": (name, content.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client, headers, run_id: str):
    confirmed = client.post(f"/api/v1/ingestions/{run_id}/confirm", headers=headers, json={"accepted": True})
    assert confirmed.status_code == 200, confirmed.text
    published = client.post(f"/api/v1/ingestions/{run_id}/publish", headers=headers)
    assert published.status_code == 200, published.text
    return published.json()


def test_monthly_snapshot_revision_replaces_current_certified_values(client, commerce_setup):
    _, headers, store, source = commerce_setup
    first = _upload(client, headers, source["id"], store["id"], "orders-v1.csv", "order,transaction_date,sales\nA,2026-07-01,100\nB,2026-07-02,50\n")
    _publish(client, headers, first["id"])
    revision = _upload(client, headers, source["id"], store["id"], "orders-v2.csv", "order,transaction_date,sales\nA,2026-07-01,120\nB,2026-07-02,50\n")
    _publish(client, headers, revision["id"])

    exported = client.get("/api/v1/exports/certified", headers=headers, params={"format": "json", "period": "2026-07"})
    assert exported.status_code == 200
    assert sum(Decimal(str(row["revenue"])) for row in exported.json()["rows"]) == Decimal("170.0000")
    with TestingSession() as db:
        first_run = db.get(IngestionRun, first["id"])
        second_run = db.get(IngestionRun, revision["id"])
        assert first_run.status == "superseded"
        assert second_run.supersedes_run_ids == [first_run.id]
        assert db.query(CertifiedAggregate).filter_by(ingestion_run_id=first_run.id, is_current=True).count() == 0
        assert db.query(NormalizedRecord).filter_by(ingestion_run_id=first_run.id, is_current=True).count() == 0

    identical = client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        data={"source_definition_id": source["id"], "store_id": store["id"]},
        files={"file": ("renamed.csv", b"order,transaction_date,sales\nA,2026-07-01,120\nB,2026-07-02,50\n", "text/csv")},
    )
    assert identical.status_code == 200
    assert identical.json()["deduplicated"] is True
    assert identical.json()["id"] == revision["id"]


def test_locked_snapshot_requires_audited_administrator_correction(client, commerce_setup):
    _, headers, store, source = commerce_setup
    first = _upload(client, headers, source["id"], store["id"], "locked-v1.csv", "order,transaction_date,sales\nA,2026-08-01,100\n")
    _publish(client, headers, first["id"])
    assert client.post(f"/api/v1/ingestions/{first['id']}/lock", headers=headers).status_code == 200
    revision = _upload(client, headers, source["id"], store["id"], "locked-v2.csv", "order,transaction_date,sales\nA,2026-08-01,125\n")
    assert client.post(f"/api/v1/ingestions/{revision['id']}/confirm", headers=headers, json={"accepted": True}).status_code == 200
    blocked = client.post(f"/api/v1/ingestions/{revision['id']}/publish", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "locked_correction_required"
    authorized = client.post(
        f"/api/v1/ingestions/{revision['id']}/authorize-correction",
        headers=headers,
        json={"locked_run_id": first["id"], "reason": "管理员确认原文件金额录入错误，需要更正"},
    )
    assert authorized.status_code == 200, authorized.text
    assert client.post(f"/api/v1/ingestions/{revision['id']}/publish", headers=headers).status_code == 200
    rows = client.get("/api/v1/exports/certified", headers=headers, params={"format": "json", "period": "2026-08"}).json()["rows"]
    assert sum(Decimal(str(row["revenue"])) for row in rows) == Decimal("125.0000")


def test_incremental_source_deduplicates_business_keys_across_runs(client, commerce_setup):
    _, headers, store, _ = commerce_setup
    source = _source(client, headers, "Incremental orders", mode="incremental")
    first = _upload(client, headers, source["id"], store["id"], "increment-1.csv", "order,date,event_type,sales\nA,2026-07-01,sale,100\n")
    _publish(client, headers, first["id"])
    second = _upload(client, headers, source["id"], store["id"], "increment-2.csv", "order,date,event_type,sales\nA,2026-07-01,sale,100\nB,2026-07-02,sale,50\n")
    published = _publish(client, headers, second["id"])
    assert published["summary"]["cross_run_duplicate_rows_removed"] == 1
    rows = client.get("/api/v1/exports/certified", headers=headers, params={"format": "json", "period": "2026-07"}).json()["rows"]
    incremental_rows = [row for row in rows if row["ingestion_run_id"] == second["id"]]
    assert sum(Decimal(str(row["revenue"])) for row in incremental_rows) == Decimal("50.0000")


def test_cross_source_required_match_transitions_pending_to_passed(client, commerce_setup):
    _, headers, store, _ = commerce_setup
    dependency = _source(client, headers, "Monthly fees", kind="fees")
    orders = _source(
        client,
        headers,
        "Monthly orders with fees",
        validations=[{
            "type": "cross_source_match",
            "mode": "required_source",
            "dependency_source_logical_id": dependency["logical_id"],
            "label": "平台费用文件",
        }],
    )
    pending = _upload(client, headers, orders["id"], store["id"], "required-orders.csv", "order,date,event_type,sales\nA,2026-07-01,sale,100\n")
    assert pending["status"] == "quality_pending"
    check = next(item for item in pending["checks"] if item["key"].startswith("cross_source:"))
    assert check["status"] == "pending" and "等待本月平台费用文件" in check["message"]

    fee_run = _upload(client, headers, dependency["id"], store["id"], "required-fees.csv", "order,date,event_type,platform_fee\nF-1,2026-07-01,fee,10\n")
    assert fee_run["status"] == "awaiting_confirmation"
    refreshed = client.get(f"/api/v1/ingestions/{pending['id']}", headers=headers).json()
    assert refreshed["status"] == "awaiting_confirmation"
    assert next(item for item in refreshed["checks"] if item["key"].startswith("cross_source:"))["status"] == "passed"
    with TestingSession() as db:
        result = db.query(CrossSourceReconciliation).filter_by(ingestion_run_id=pending["id"]).one()
        assert result.status == "passed" and result.dependency_run_id == fee_run["id"]


def test_cross_source_control_total_difference_fails_and_creates_problem(client, commerce_setup):
    _, headers, store, _ = commerce_setup
    dependency = _source(client, headers, "Control total")
    checked = _source(
        client,
        headers,
        "Checked total",
        validations=[{
            "type": "cross_source_match",
            "mode": "control_total",
            "dependency_source_logical_id": dependency["logical_id"],
            "field": "revenue",
            "dependency_field": "revenue",
            "tolerance": "0.0100",
            "label": "销售控制总额",
        }],
    )
    run = _upload(client, headers, checked["id"], store["id"], "checked.csv", "order,date,event_type,sales\nA,2026-07-01,sale,100\n")
    _upload(client, headers, dependency["id"], store["id"], "control.csv", "order,date,event_type,sales\nA,2026-07-01,sale,99\n")
    failed = client.get(f"/api/v1/ingestions/{run['id']}", headers=headers).json()
    check = next(item for item in failed["checks"] if item["key"].startswith("cross_source:"))
    assert failed["status"] == "quality_failed"
    assert check["status"] == "failed"
    assert check["details"][0]["actual"] == "100.0000"
    assert check["details"][0]["expected"] == "99.0000"
    assert check["details"][0]["difference"] == "1.0000"
    assert client.post(f"/api/v1/ingestions/{run['id']}/confirm", headers=headers, json={"accepted": True}).status_code == 409
    issues = client.get("/api/v1/issues", headers=headers).json()
    assert any(item["ingestion_run_id"] == run["id"] and item["kind"] == "cross_source_reconciliation" for item in issues)


def test_fee_rows_do_not_increase_standard_order_count(client, commerce_setup):
    _, headers, store, _ = commerce_setup
    orders = _source(client, headers, "Six orders", kind="orders")
    fees = _source(client, headers, "Six fees", kind="fees")
    order_lines = [f"O-{index},2026-07-{index:02d},sale,150,0,0,0,0,62.5" for index in range(1, 7)]
    fee_lines = [
        "F-1,2026-07-01,fee,0,70,0,0,0,0",
        "F-2,2026-07-02,fee,0,0,42,0,0,0",
        "F-3,2026-07-03,fee,0,0,0,35,0,0",
        "F-4,2026-07-04,fee,0,0,0,0,25,0",
        "F-5,2026-07-05,fee,0,0,10,0,0,0",
        "F-6,2026-07-06,fee,0,0,10,0,0,0",
    ]
    header = "order,date,event_type,sales,refund,platform_fee,advertising_fee,shipping_fee,cost\n"
    _publish(client, headers, _upload(client, headers, orders["id"], store["id"], "six-orders.csv", header + "\n".join(order_lines) + "\n")["id"])
    _publish(client, headers, _upload(client, headers, fees["id"], store["id"], "six-fees.csv", header + "\n".join(fee_lines) + "\n")["id"])
    overview = client.get("/api/v1/analytics/overview", headers=headers, params={"date_from": "2026-07-01", "date_to": "2026-08-01"}).json()["metrics"]
    assert overview["row_count"] == 12
    assert overview["order_count"] == 6
    assert overview["revenue"] == "900.0000"
    assert overview["refund"] == "70.0000"
    assert overview["fees"] == "122.0000"
    assert overview["product_cost"] == "375.0000"
    assert overview["profit"] == "333.0000"


def test_role_matrix_and_first_password_change_gate(client):
    setup = client.post("/api/v1/setup", json={
        "enterprise_name": "Role Matrix Commerce",
        "activation_at": "2026-01-01T00:00:00Z",
        "name": "Owner",
        "email": "roles-owner@example.test",
        "password": "owner-password-12345",
    })
    assert setup.status_code == 200
    temporary = client.post("/api/v1/users/invite", json={"name": "Implementation", "email": "impl@example.test", "role": "implementer", "store_ids": []}).json()["temporary_password"]
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json={"email": "impl@example.test", "password": temporary}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/stores").status_code == 403
    assert client.get("/api/v1/exports/certified").status_code == 403
    assert client.get("/api/v1/users").status_code == 403
    changed = client.post("/api/v1/auth/change-password", json={"current_password": temporary, "new_password": "implementation-new-password"})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/v1/stores").status_code == 200
    assert client.get("/api/v1/issues").status_code == 200
    assert client.get("/api/v1/users").status_code == 403
    assert client.post("/api/v1/users/invite", json={"name": "No", "email": "no@example.test", "role": "viewer"}).status_code == 403
