from __future__ import annotations

import hashlib
import base64
import json

from conftest import tenant_headers


def test_tenant_isolation_and_rbac(client, tenants):
    first, second = tenants
    created = client.post("/api/v1/stores", headers=tenant_headers(first), json={"name": "Private Store", "activation_at": "2026-01-01T00:00:00Z"})
    assert created.status_code == 200
    assert client.get("/api/v1/stores", headers=tenant_headers(second)).json() == []
    assert client.get(f"/api/v1/stores/{created.json()['id']}", headers=tenant_headers(second)).status_code == 404
    spoof = client.get("/api/v1/stores", headers={"X-Enterprise-ID": second, "X-Role": "platform_admin", "X-User-ID": "forged"})
    assert spoof.status_code == 400
    invited = client.post("/api/v1/users/invite", headers=tenant_headers(first), json={"name": "Viewer", "email": "viewer@example.test", "role": "viewer", "password": "viewer-password-123", "store_ids": []})
    assert invited.status_code == 200, invited.text
    login = client.post("/api/v1/auth/login", json={"email": "viewer@example.test", "password": "viewer-password-123"})
    assert login.status_code == 200
    denied = client.post("/api/v1/stores", json={"name": "No", "activation_at": "2026-01-01T00:00:00Z"})
    assert denied.status_code == 403


def test_active_store_change_creates_effective_version(client, tenants):
    first = tenants[0]
    headers = tenant_headers(first)
    store = client.post("/api/v1/stores", headers=headers, json={"name": "Migrating", "status": "active", "activation_at": "2026-01-01T00:00:00Z"}).json()
    changed = client.patch(f"/api/v1/stores/{store['id']}", headers=headers, json={"name": "Migrating v2"})
    assert changed.status_code == 200
    assert changed.json()["id"] != store["id"]
    assert changed.json()["version"] == 2
    assert changed.json()["status"] == "draft"


def test_user_roles_are_persisted_and_tenant_scoped(client, tenants):
    first, second = tenants
    created = client.post(
        "/api/v1/users",
        headers=tenant_headers(first),
        json={"name": "Store Analyst", "email": "analyst@example.invalid", "role": "analyst", "store_ids": [], "password": "analyst-password-123"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["role"] == "analyst"
    assert client.get("/api/v1/users", headers=tenant_headers(second)).json() == []
    invalid = client.post(
        "/api/v1/users",
        headers=tenant_headers(first),
        json={"name": "Invalid", "email": "invalid@example.invalid", "role": "platform_admin"},
    )
    assert invalid.status_code == 422


def test_diagnostics_reports_optional_dependency_state(client, tenants):
    response = client.get("/api/v1/health/diagnostics", headers=tenant_headers(tenants[0]))
    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"healthy", "degraded"}
    assert response.json()["ai_required"] is False


def test_resumable_upload_contract(client, commerce_setup):
    _, headers, store, source = commerce_setup
    data = b"order,transaction_date,sales\nA,2026-02-01,12.50\n"
    initiated = client.post("/api/v1/ingestions/upload/initiate", headers=headers, json={"source_definition_id": source["id"], "store_id": store["id"], "filename": "orders.csv", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "part_size": 262144})
    assert initiated.status_code == 200, initiated.text
    upload_id = initiated.json()["upload_id"]
    part = client.put(f"/api/v1/ingestions/upload/{upload_id}/parts/1", headers={**headers, "Content-Type": "application/octet-stream"}, content=data)
    assert part.status_code == 200
    completed = client.post(f"/api/v1/ingestions/upload/{upload_id}/complete", headers=headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "awaiting_confirmation"


def test_pbix_parser_gracefully_falls_back_to_manual(client, tenants):
    headers = tenant_headers(tenants[0])
    response = client.post("/api/v1/model-assets/pbix", headers=headers, data={"name": "Existing finance model"}, files={"file": ("model.pbix", b"not-a-pbix", "application/octet-stream")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation_status"] == "manual_required"
    manual = client.post(f"/api/v1/model-assets/{body['id']}/manual-metadata", headers=headers, json={"tables": ["Sales"], "measures": ["Revenue"], "expected_inputs": ["orders"]})
    assert manual.status_code == 200
    assert manual.json()["validation_status"] == "manually_registered"


def test_ai_provider_secret_is_never_returned_and_disabled_mode_works(client, tenants):
    headers = tenant_headers(tenants[0])
    provider = client.post("/api/v1/ai/providers", headers=headers, json={"name": "Optional gateway", "mode": "disabled", "api_base": "http://litellm:4000", "api_key": "super-secret"})
    assert provider.status_code == 200, provider.text
    assert provider.json()["has_api_key"] is True
    assert "encrypted_api_key" not in provider.text and "super-secret" not in provider.text
    question = client.post("/api/v1/business-questions", headers=headers, json={"question": "How much revenue do we have?"})
    assert question.status_code == 200
    assert question.json()["ai_used"] is False
    assert len(question.json()["options"]) <= 3


def test_readonly_query_rejects_physical_tables(client, tenants):
    headers = tenant_headers(tenants[0])
    bad = client.post("/api/v1/certified-query", headers=headers, json={"sql": "select * from enterprises"})
    assert bad.status_code == 422
    mutation = client.post("/api/v1/certified-query", headers=headers, json={"sql": "delete from certified_sales"})
    assert mutation.status_code == 422
    good = client.post("/api/v1/certified-query", headers=headers, json={"sql": "select sum(revenue) as revenue from certified_sales"})
    assert good.status_code == 200, good.text


def test_configuration_export_redacts_secrets_and_import_dry_run(client, tenants):
    headers = tenant_headers(tenants[0])
    client.post("/api/v1/ai/providers", headers=headers, json={"name": "Cloud AI", "mode": "cloud", "api_key": "must-not-export"})
    exported = client.get("/api/v1/configuration/export", headers=headers)
    assert exported.status_code == 200
    assert "must-not-export" not in exported.text
    assert "encrypted_api_key" not in exported.text
    assert exported.json()["resources"]["ai/providers"][0]["has_api_key"] is True
    dry_run = client.post("/api/v1/configuration/import", headers=headers, json={"resources": {"dashboards": [{"name": "Imported dashboard", "bi_adapter": "superset"}]}, "dry_run": True})
    assert dry_run.status_code == 200
    assert dry_run.json()["counts"]["dashboards"] == 1


def test_certified_export_is_tenant_scoped(client, tenants):
    headers = tenant_headers(tenants[0])
    result = client.get("/api/v1/exports/certified", headers=headers)
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/csv")
    assert result.text.startswith("store_id,period_start")
    internal = client.get(
        "/api/v1/exports/certified",
        headers=headers,
        params={"format": "json", "date_from": "2026-07-01", "date_to": "2026-08-01T00:00:00Z"},
    )
    assert internal.status_code == 200 and internal.json() == {"rows": []}
    overview = client.get(
        "/api/v1/analytics/overview",
        headers=headers,
        params={"date_from": "2026-07-01", "date_to": "2026-08-01T00:00:00Z"},
    )
    assert overview.status_code == 200


def test_superset_embed_token_is_short_lived_and_tenant_scoped(client, tenants):
    first, second = tenants
    dashboard = client.post(
        "/api/v1/dashboards",
        headers=tenant_headers(first),
        json={"name": "Embedded commerce", "status": "published", "bi_adapter": "superset", "external_id": "741fec6d-5c6b-4f81-8df2-ec59cf16fb55"},
    ).json()
    response = client.post(f"/api/v1/dashboards/{dashboard['id']}/embed-token", headers=tenant_headers(first, "viewer"))
    assert response.status_code == 200, response.text
    payload_segment = response.json()["token"].split(".")[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_segment))
    assert payload["resources"][0]["id"] == dashboard["external_id"]
    assert first in payload["rls_rules"][0]["clause"]
    assert payload["exp"] - payload["iat"] == 300
    assert client.post(f"/api/v1/dashboards/{dashboard['id']}/embed-token", headers=tenant_headers(second, "viewer")).status_code == 404


def test_source_and_pbix_assets_bind_to_multiple_scopes(client, tenants):
    headers = tenant_headers(tenants[0])
    platform = client.post(
        "/api/v1/platforms",
        headers=headers,
        json={"name": "Marketplace account", "platform": "marketplace"},
    ).json()
    stores = [
        client.post(
            "/api/v1/stores",
            headers=headers,
            json={
                "name": name,
                "platform_account_id": platform["id"],
                "activation_at": "2026-01-01T00:00:00Z",
            },
        ).json()
        for name in ("North store", "South store")
    ]
    source = client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "Multi-store sales",
            "file_types": ["csv"],
            "coverage_time_field": "occurred_at",
            "data_granularity": "day",
            "arrival_frequency": "daily",
            "activation_at": "2026-01-01T00:00:00Z",
        },
    ).json()
    source_bindings = [
        client.post(
            "/api/v1/source-bindings",
            headers=headers,
            json={
                "name": f"Sales for {store['name']}",
                "source_definition_id": source["id"],
                "scope_type": "store",
                "scope_id": store["id"],
            },
        )
        for store in stores
    ]
    assert all(response.status_code == 200 for response in source_bindings)

    asset = client.post(
        "/api/v1/model-assets",
        headers=headers,
        json={"name": "Shared PBIX", "asset_type": "pbix"},
    ).json()
    scopes = [
        ("store", stores[0]["id"]),
        ("store", stores[1]["id"]),
        ("platform_account", platform["id"]),
    ]
    model_bindings = [
        client.post(
            "/api/v1/model-scope-bindings",
            headers=headers,
            json={
                "name": f"PBIX scope {index}",
                "model_asset_id": asset["id"],
                "scope_type": scope_type,
                "scope_id": scope_id,
            },
        )
        for index, (scope_type, scope_id) in enumerate(scopes)
    ]
    assert all(response.status_code == 200 for response in model_bindings)
    assert len([item for item in client.get("/api/v1/source-bindings", headers=headers).json() if item["source_definition_id"] == source["id"]]) == 2
    assert len(client.get("/api/v1/model-scope-bindings", headers=headers).json()) == 3


def test_store_platform_migration_preserves_the_effective_history(client, tenants):
    headers = tenant_headers(tenants[0])
    old_platform = client.post(
        "/api/v1/platforms",
        headers=headers,
        json={"name": "Old platform", "platform": "old", "status": "active"},
    ).json()
    new_platform = client.post(
        "/api/v1/platforms",
        headers=headers,
        json={"name": "New platform", "platform": "new", "status": "active"},
    ).json()
    original = client.post(
        "/api/v1/stores",
        headers=headers,
        json={
            "name": "Migrating store",
            "status": "active",
            "effective_from": "2026-01-01T00:00:00Z",
            "activation_at": "2026-01-01T00:00:00Z",
            "platform_account_id": old_platform["id"],
        },
    ).json()
    migrated = client.patch(
        f"/api/v1/stores/{original['id']}",
        headers=headers,
        json={"platform_account_id": new_platform["id"]},
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["id"] != original["id"]
    assert migrated.json()["version"] == 2
    assert migrated.json()["platform_account_id"] == new_platform["id"]
    historical = client.get(f"/api/v1/stores/{original['id']}", headers=headers).json()
    assert historical["platform_account_id"] == old_platform["id"]
    assert historical["effective_to"] is not None
