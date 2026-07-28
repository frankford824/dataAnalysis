"""Tests for claims API router endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from commerce_harness.api import create_app
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _setup(tmp_path: Path) -> tuple[TestClient, DuckDBMemory]:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    db = DuckDBMemory(workbench.database)
    db.initialize()
    _seed_data(db)
    db.close()
    client = TestClient(create_app(config))
    return client, DuckDBMemory(workbench.database)


def _seed_data(db: DuckDBMemory) -> None:
    db.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES ('contract-1', 'taobao', 'ent-1', 'store-1', 'taobao',
                  1, DATE '2026-03-01', 'active', '{}')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        ) VALUES ('period-1', 'contract-1', 'store-1',
                  DATE '2026-03-01', DATE '2026-03-31', 'open')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO invariant_definition (
            invariant_id, domain, family, title, definition_json, origin
        ) VALUES ('inv-1', 'settlement', 'equality', '差额',
                  '{"family":"equality"}', 'builtin')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO invariant_version (
            invariant_version_id, invariant_id, semver, status
        ) VALUES ('inv-1:1.0.0', 'inv-1', '1.0.0', 'active')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO claim (
            claim_id, contract_id, period_id, store_id,
            invariant_version_id, subject_kind, subject_key,
            reason_code, claimed_amount, currency, status
        ) VALUES ('claim-api-1', 'contract-1', 'period-1', 'store-1',
                  'inv-1:1.0.0', 'unresolved_balance', 'ub-1',
                  'amount_mismatch', 100.5000, 'CNY', 'draft')
        """
    )
    db.execute(
        """
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status
        ) VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO reconciliation_balance (
            balance_id, run_id, contract_id, period_id, balance_key,
            expected_amount, actual_amount, matched_amount, difference_amount,
            status
        ) VALUES (
            'balance-1', 'run-1', 'contract-1', 'period-1',
            'key-1', 100, 50, 50, 50, 'unresolved'
        )
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO unresolved_balance (
            unresolved_id, balance_id, reason_code, amount, status
        ) VALUES ('ub-api-2', 'balance-1', 'amount_mismatch', 75.0000, 'open')
        ON CONFLICT DO NOTHING
        """
    )


class TestListEndpoints:
    def test_get_claims_returns_list(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["claim_id"] == "claim-api-1"
        db.close()

    def test_get_claims_page(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/page", params={"limit": 10})
        assert response.status_code == 200
        assert "total" in response.json()
        db.close()

    def test_get_claim_by_id(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/claim-api-1")
        assert response.status_code == 200
        assert response.json()["claim_id"] == "claim-api-1"
        db.close()

    def test_get_nonexistent_claim_returns_404(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/nonexistent")
        assert response.status_code == 404
        db.close()


class TestSubmitEndpoint:
    def test_submit_from_draft_packages_and_submits(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三", "externalRef": "EXT-001"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "submitted"
        claim = client.get("/api/v1/claims/claim-api-1").json()
        assert claim["status"] == "submitted"
        assert claim["packet_sha256"] is not None
        db.close()

    def test_submit_already_submitted_returns_409(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三"},
        )
        response = client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三"},
        )
        assert response.status_code == 409
        db.close()


class TestResponseEndpoint:
    def test_accept_claim(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三"},
        )
        response = client.post(
            "/api/v1/claims/claim-api-1/response",
            json={
                "operatorName": "李四",
                "verdict": "accepted",
                "acceptedAmount": "100.5000",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        db.close()

    def test_reject_claim(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三"},
        )
        response = client.post(
            "/api/v1/claims/claim-api-1/response",
            json={
                "operatorName": "李四",
                "verdict": "rejected",
                "responseText": "证据不足",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        db.close()


class TestRecoveryEndpoint:
    def test_recover_claim(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        client.post(
            "/api/v1/claims/claim-api-1/submit",
            json={"operatorName": "张三"},
        )
        client.post(
            "/api/v1/claims/claim-api-1/response",
            json={
                "operatorName": "李四",
                "verdict": "accepted",
                "acceptedAmount": "100.5000",
            },
        )
        response = client.post(
            "/api/v1/claims/claim-api-1/recovery",
            json={
                "operatorName": "王五",
                "recoveredAmount": "100.5000",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "recovered"
        db.close()

    def test_invalid_recovery_amount(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.post(
            "/api/v1/claims/claim-api-1/recovery",
            json={
                "operatorName": "王五",
                "recoveredAmount": "not_a_number",
            },
        )
        assert response.status_code == 422
        db.close()


class TestDetectEndpoint:
    def test_detect_creates_drafts(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.post(
            "/api/v1/claims/detect",
            json={},
        )
        assert response.status_code == 200
        claims = response.json()
        assert len(claims) >= 1
        assert all(c["status"] == "draft" for c in claims)
        db.close()

    def test_detect_with_filters(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.post(
            "/api/v1/claims/detect",
            json={"periodId": "period-1", "storeId": "store-1"},
        )
        assert response.status_code == 200
        db.close()


class TestPacketEndpoint:
    def test_download_packet_zip(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/claim-api-1/packet")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "claim-claim-api-1.zip" in response.headers.get("content-disposition", "")
        assert len(response.content) > 0
        db.close()

    def test_packet_for_nonexistent_returns_404(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/nonexistent/packet")
        assert response.status_code == 404
        db.close()

    def test_packet_refuses_path_traversal(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        workbench_root = tmp_path / "workbench"
        secrets = workbench_root / "exports" / "certifications"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "secret.json").write_text("SENSITIVE", encoding="utf-8")

        for probe in ("%2e%2e", "%2e%2e%2f%2e%2e", "a/../..", "..%2f.."):
            response = client.get(f"/api/v1/claims/{probe}/packet")
            assert response.status_code in {400, 404}, probe
            assert b"SENSITIVE" not in response.content, probe
        db.close()


class TestStatsEndpoint:
    def test_stats_returns_after_claims(self, tmp_path: Path) -> None:
        client, db = _setup(tmp_path)
        response = client.get("/api/v1/claims/stats")
        assert response.status_code == 200
        stats = response.json()
        assert isinstance(stats, list)
        if stats:
            assert "invariant_version_id" in stats[0]
        db.close()
