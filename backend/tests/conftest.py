from __future__ import annotations

import os
from pathlib import Path

os.environ["COMMERCE_DATABASE_URL"] = "sqlite:///./test-suite.db"
os.environ["COMMERCE_OBJECT_STORAGE_PATH"] = "./test-objects"
os.environ["COMMERCE_AUTO_CREATE_SCHEMA"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import Base, engine, get_db
from app.main import app


TestingSession = sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    def override_db():
        with TestingSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as value:
        yield value
    app.dependency_overrides.clear()


def platform_headers() -> dict[str, str]:
    return {"X-Role": "platform_admin", "X-User-ID": "platform-owner"}


def tenant_headers(enterprise_id: str, role: str = "admin") -> dict[str, str]:
    return {"X-Enterprise-ID": enterprise_id, "X-Role": role, "X-User-ID": f"{role}-user"}


@pytest.fixture
def tenants(client):
    ids = []
    for name in ["Northwind Commerce", "Contoso Retail"]:
        response = client.post("/api/v1/enterprises", headers=platform_headers(), json={"name": name, "activation_at": "2026-01-01T00:00:00Z"})
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    return ids


@pytest.fixture
def commerce_setup(client, tenants):
    enterprise_id = tenants[0]
    headers = tenant_headers(enterprise_id)
    store = client.post("/api/v1/stores", headers=headers, json={"name": "Web Store", "status": "active", "activation_at": "2026-01-01T00:00:00Z"})
    assert store.status_code == 200, store.text
    source = client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "Daily orders",
            "status": "active",
            "activation_at": "2026-01-01T00:00:00Z",
            "coverage_time_field": "transaction_date",
            "data_granularity": "day",
            "arrival_frequency": "daily",
            "file_types": ["csv", "xlsx", "zip"],
            "field_aliases": {"order_id": ["order"], "revenue": ["sales"]},
            "dedupe_keys": ["order_id"],
            "validations": [{"type": "required_field", "field": "order_id"}],
        },
    )
    assert source.status_code == 200, source.text
    binding = client.post("/api/v1/source-bindings", headers=headers, json={"name": "Store binding", "source_definition_id": source.json()["id"], "scope_type": "store", "scope_id": store.json()["id"]})
    assert binding.status_code == 200, binding.text
    return enterprise_id, headers, store.json(), source.json()
