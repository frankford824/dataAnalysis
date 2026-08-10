import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import ledger.api as api
from ledger.security import Principal, SecurityError, authenticate, authorize


def test_unconfigured_auth_is_loopback_only(monkeypatch):
    monkeypatch.delenv("LEDGER_AUTH_FILE", raising=False)
    assert authenticate("127.0.0.1").role == "admin"
    with pytest.raises(SecurityError) as exc:
        authenticate("192.168.1.20")
    assert exc.value.status == 403


def test_bearer_digest_maps_to_named_role(tmp_path, monkeypatch):
    token = "correct horse battery staple"
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"users": [{
        "name": "财务甲",
        "role": "finance",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }]}), encoding="utf-8")
    monkeypatch.setenv("LEDGER_AUTH_FILE", str(path))
    assert authenticate("10.0.0.8", f"Bearer {token}") == Principal("财务甲", "finance")
    with pytest.raises(SecurityError) as exc:
        authenticate("127.0.0.1", "Bearer wrong")
    assert exc.value.status == 401


def test_role_policy_separates_operations_finance_and_admin():
    operator = Principal("店长", "operator")
    authorize(operator, "POST", "/api/upload")
    with pytest.raises(SecurityError):
        authorize(operator, "POST", "/api/stores/s1/periods/2025-05/close")
    finance = Principal("财务", "finance")
    authorize(finance, "POST", "/api/stores/s1/periods/2025-05/close")
    with pytest.raises(SecurityError):
        authorize(finance, "PATCH", "/api/stores/s1")


def test_api_enforces_configured_role_and_exposes_identity(tmp_path, monkeypatch):
    token = "read-only-token"
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"users": [{
        "name": "审计员",
        "role": "viewer",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }]}), encoding="utf-8")
    monkeypatch.setenv("LEDGER_AUTH_FILE", str(path))
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(api.app) as client:
        assert client.get("/api/bootstrap").status_code == 401
        body = client.get("/api/bootstrap", headers=headers).json()
        assert body["principal"] == {"name": "审计员", "role": "viewer"}
        assert client.post("/api/upload", headers=headers, files=[]).status_code == 403
