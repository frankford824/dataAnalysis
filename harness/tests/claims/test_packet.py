"""Tests for claim evidence packet export."""

from __future__ import annotations

import json
from pathlib import Path

from commerce_harness.claims.packet import export_packet
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.workbench import initialize


def _setup_db(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    db = DuckDBMemory(tmp_path / "workbench" / "harness.duckdb")
    db.initialize()
    return db


def _seed_claim_with_evidence(db: DuckDBMemory) -> None:
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
        INSERT INTO run_log (
            run_id, contract_id, period_id, run_kind, status
        ) VALUES ('run-1', 'contract-1', 'period-1', 'reconcile', 'succeeded')
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO source_snapshot (
            snapshot_id, content_sha256, byte_size, object_uri, source_uri,
            original_name, captured_at, manifest_json
        ) VALUES (
            'snap-1', repeat('a', 64), 100, '/objects/snap-1',
            'finance-win-ro://ledger.xlsx', 'ledger.xlsx',
            current_timestamp, '{}'
        )
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO evidence_record (
            evidence_id, run_id, snapshot_id, evidence_kind,
            payload_json, payload_sha256
        ) VALUES (
            'ev-1', 'run-1', 'snap-1', 'source_row',
            '[]', repeat('b', 64)
        )
        ON CONFLICT DO NOTHING
        """
    )
    db.execute(
        """
        INSERT INTO evidence_binding (
            binding_id, evidence_id, ordinal, snapshot_id, source_sheet,
            row_no, field, source_value, normalization_version
        ) VALUES (
            'bind-1', 'ev-1', 0, 'snap-1', '账务明细',
            42, 'amount', '50.0000', 'finite-normalization-v5'
        )
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
            unresolved_id, balance_id, reason_code, amount, status, evidence_id
        ) VALUES ('ub-1', 'balance-1', 'amount_mismatch', 50.0000, 'open', 'ev-1')
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
        ) VALUES ('claim-pkt', 'contract-1', 'period-1', 'store-1',
                  'inv-1:1.0.0', 'unresolved_balance', 'ub-1',
                  'amount_mismatch', 50.0000, 'CNY', 'draft')
        """
    )


class TestExportPacket:
    def test_creates_manifest_and_evidence(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_with_evidence(db)
        exports_root = tmp_path / "workbench" / "exports"

        result = export_packet(db, "claim-pkt", exports_root)

        assert result["claim_id"] == "claim-pkt"
        assert result["evidence_count"] == 1
        assert result["packet_sha256"]

        packet_dir = Path(result["packet_dir"])
        assert (packet_dir / "manifest.json").is_file()
        assert (packet_dir / "evidence" / "summary.json").is_file()

        manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] == "claim-packet-v1"
        assert manifest["claim_id"] == "claim-pkt"
        assert manifest["claimed_amount"] == "50.0000"
        assert manifest["invariant"] is not None
        assert manifest["invariant"]["title"] == "差额"

        summary = json.loads((packet_dir / "evidence" / "summary.json").read_text(encoding="utf-8"))
        assert summary["binding_count"] == 1
        assert summary["bindings"][0]["row_no"] == 42
        db.close()

    def test_sha256_is_stable_for_same_content(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_with_evidence(db)
        exports_root = tmp_path / "workbench" / "exports"

        r1 = export_packet(db, "claim-pkt", exports_root)
        assert r1["packet_sha256"]
        assert len(r1["packet_sha256"]) == 64
        db.close()

    def test_packet_without_evidence(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        _seed_claim_with_evidence(db)
        db.execute("DELETE FROM evidence_binding")
        exports_root = tmp_path / "workbench" / "exports"

        result = export_packet(db, "claim-pkt", exports_root)
        assert result["evidence_count"] == 0
        db.close()
