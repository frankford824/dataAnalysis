from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from conftest import TestingSession, tenant_headers
from app.models import AuthSession, CertifiedAggregate, IngestionRun, UserAccount, utcnow


SETUP = {
    "enterprise_name": "Secure Commerce",
    "activation_at": "2026-01-01T00:00:00Z",
    "name": "Initial Owner",
    "email": "secure-owner@example.test",
    "password": "secure-bootstrap-password",
}


def test_authentication_is_default_and_bootstrap_is_one_time(client):
    assert client.get("/api/v1/stores").status_code == 401
    assert client.get("/api/v1/setup").json() == {"initialized": False}

    created = client.post("/api/v1/setup", json=SETUP)
    assert created.status_code == 200, created.text
    assert created.json()["user"]["enterprise_name"] == "Secure Commerce"
    assert created.json()["created"]["semantic_model_id"]
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/setup", json={**SETUP, "email": "other@example.test"}).status_code == 409

    with TestingSession() as db:
        user = db.query(UserAccount).filter_by(email=SETUP["email"]).one()
        assert user.password_hash.startswith("$argon2")
        assert SETUP["password"] not in user.password_hash

    assert client.get("/health", headers={"X-Enterprise-ID": "forged"}).status_code == 400
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_openapi_documents_bearer_and_cookie_authentication(client):
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["BearerAuth"]["scheme"] == "bearer"
    assert schemes["SessionCookie"]["name"] == "commerce_session"
    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]
    assert schema["paths"]["/api/v1/stores"]["get"]["security"] == [{"BearerAuth": []}, {"SessionCookie": []}]


def test_expired_bearer_session_is_rejected(client):
    response = client.post("/api/v1/setup", json=SETUP)
    token = response.json()["access_token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    with TestingSession() as db:
        session = db.query(AuthSession).filter_by(token_hash=digest).one()
        session.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_email_is_globally_unique_and_store_scope_is_enforced(client, tenants):
    first, second = tenants
    allowed = client.post(
        "/api/v1/stores",
        headers=tenant_headers(first),
        json={"name": "Allowed store", "activation_at": "2026-01-01T00:00:00Z"},
    ).json()
    denied = client.post(
        "/api/v1/stores",
        headers=tenant_headers(first),
        json={"name": "Denied store", "activation_at": "2026-01-01T00:00:00Z"},
    ).json()
    invite = client.post(
        "/api/v1/users/invite",
        headers=tenant_headers(first),
        json={
            "name": "Scoped analyst",
            "email": "scoped@example.test",
            "role": "analyst",
            "password": "scoped-analyst-password",
            "store_ids": [allowed["id"]],
        },
    )
    assert invite.status_code == 200, invite.text
    duplicate = client.post(
        "/api/v1/users",
        headers=tenant_headers(second),
        json={"name": "Duplicate", "email": "scoped@example.test", "role": "viewer", "password": "duplicate-password-123"},
    )
    assert duplicate.status_code == 409

    login = client.post("/api/v1/auth/login", json={"email": "scoped@example.test", "password": "scoped-analyst-password"})
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    visible = client.get("/api/v1/stores", headers=auth)
    assert visible.status_code == 200
    assert [item["logical_id"] for item in visible.json()] == [allowed["logical_id"]]
    assert client.get("/api/v1/exports/certified", headers=auth, params={"store_id": denied["id"]}).status_code == 403
    query = client.post("/api/v1/certified-query", headers=auth, json={"sql": "select count(*) as rows from certified_sales"})
    assert query.status_code == 200 and query.json()["rows"] == [[0]]


def test_configuration_import_failure_is_atomic(client, tenants):
    headers = tenant_headers(tenants[0])
    before = client.get("/api/v1/business-entities", headers=headers).json()
    failed = client.post(
        "/api/v1/configuration/import",
        headers=headers,
        json={
            "resources": {
                "business-entities": [{"name": "Must roll back"}],
                "stores": [{"name": "Invalid store"}],
            },
            "dry_run": False,
        },
    )
    assert failed.status_code == 422
    after = client.get("/api/v1/business-entities", headers=headers).json()
    assert {item["id"] for item in after} == {item["id"] for item in before}


def test_supported_business_questions_return_real_rank_and_comparison(client):
    setup = client.post("/api/v1/setup", json=SETUP)
    assert setup.status_code == 200
    enterprise_id = setup.json()["user"]["enterprise_id"]
    store = client.get("/api/v1/stores").json()[0]
    source = client.get("/api/v1/sources").json()[0]
    with TestingSession() as db:
        run = IngestionRun(
            enterprise_id=enterprise_id,
            source_definition_id=source["id"],
            store_id=store["id"],
            source_sha256="a" * 64,
            original_filename="question-test.csv",
            raw_object_key="raw/question-test.csv",
            status="published",
            source_version=1,
            source_config_id=source["id"],
            rule_version=1,
            rule_config_id=source["id"],
            model_version=1,
            semantic_model_id=setup.json()["created"]["semantic_model_id"],
            created_by="test",
        )
        db.add(run)
        db.flush()
        db.add_all([
            CertifiedAggregate(
                enterprise_id=enterprise_id, ingestion_run_id=run.id, store_id=store["id"],
                period_start=datetime(2026, 6, 10, tzinfo=timezone.utc), grain="day",
                row_count=1, order_count=1, revenue=Decimal("80"), refund=Decimal("5"),
                fees=Decimal("10"), product_cost=Decimal("20"), profit=Decimal("45"),
            ),
            CertifiedAggregate(
                enterprise_id=enterprise_id, ingestion_run_id=run.id, store_id=store["id"],
                period_start=datetime(2026, 7, 10, tzinfo=timezone.utc), grain="day",
                row_count=1, order_count=1, revenue=Decimal("100"), refund=Decimal("5"),
                fees=Decimal("10"), product_cost=Decimal("25"), profit=Decimal("60"),
            ),
        ])
        db.commit()

    context = {"store_ids": [store["id"]], "date_from": "2026-07-01T00:00:00Z", "date_to": "2026-08-01T00:00:00Z"}
    ranking = client.post("/api/v1/business-questions", json={**context, "question": "店铺排名", "question_type": "ranking"})
    assert ranking.status_code == 200
    assert store["name"] in ranking.json()["answer"] and "60.0000" in ranking.json()["answer"]
    comparison = client.post("/api/v1/business-questions", json={**context, "question": "与上月比较", "question_type": "month_comparison"})
    assert comparison.status_code == 200
    assert "上月为 80.0000" in comparison.json()["answer"]
    assert comparison.json()["comparison"] == {"previous_month": "80.0000", "change": "20.0000", "change_rate": "25.00"}
    mixed_timezone = client.post(
        "/api/v1/business-questions",
        json={**context, "date_from": "2026-07-01", "question": "本月销售", "question_type": "sales"},
    )
    assert mixed_timezone.status_code == 200
