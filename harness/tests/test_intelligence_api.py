"""Model-facing endpoints: grounded numbers, server-owned proposals."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from commerce_harness.api import create_app
from commerce_harness.config import load_config
from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.questions import generate_review_question
from commerce_harness.understanding import (
    FileUnderstanding,
    freeze_understanding,
    propose_file_understanding,
    record_proposal,
)
from commerce_harness.workbench import initialize


@pytest.fixture(autouse=True)
def _no_auto_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FA_AUTO_COMPUTE", "0")


class _Result:
    def __init__(self, content: dict[str, Any] | None) -> None:
        self.status = "ok" if content is not None else "unavailable"
        self.model = "test-model"
        self.content = content
        self.reason = None


class _Gateway:
    def __init__(self, content: dict[str, Any] | None) -> None:
        self._content = content

    def complete_json(self, **_: Any) -> _Result:
        return _Result(self._content)


def _client(tmp_path: Path) -> TestClient:
    config = load_config(workspace=tmp_path / "workbench")
    initialize(config)
    return TestClient(create_app(config))


def _database(tmp_path: Path) -> DuckDBMemory:
    config = load_config(workspace=tmp_path / "workbench")
    workbench = initialize(config)
    database = DuckDBMemory(workbench.database)
    database.initialize()
    return database


def _question(content: dict[str, Any] | None):
    return generate_review_question(
        _Gateway(content),  # type: ignore[arg-type]
        model="test-model",
        reason_code="amount_mismatch",
        amount=Decimal("120.50"),
        count=2,
        explanation=None,
        ledger=None,
    )


def test_generated_copy_may_only_repeat_given_numbers() -> None:
    invented = _question(
        {
            "what": "有 2 笔对不上，合计 98765.00 元。",
            "why": "需要你确认。",
            "options": [{"code": "keep_open", "label": "先放着", "recommended": True}],
        }
    )
    assert invented.fallback is True
    assert invented.evidence_guard == "fallback"

    grounded = _question(
        {
            "what": "有 2 笔对不上，合计 120.50 元。",
            "why": "需要你确认这是不是跨月到账。",
            "options": [
                {"code": "explain_timing", "label": "是跨月到账", "recommended": True}
            ],
        }
    )
    assert grounded.fallback is False
    assert grounded.evidence_guard == "numbers_grounded"


def test_question_falls_back_when_model_is_unavailable() -> None:
    question = _question(None)
    assert question.fallback is True
    assert "120.50" in question.what


def test_questions_endpoint_rejects_bad_amount(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/intelligence/questions",
        json={"reasonCode": "amount_mismatch", "amount": "not-a-number"},
    )
    assert response.status_code == 422

    missing = client.post("/api/v1/intelligence/questions", json={"count": 2})
    assert missing.status_code == 422


def test_questions_endpoint_rejects_unknown_unresolved_ids(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/intelligence/questions",
        json={"unresolvedIds": ["nope"]},
    )
    assert response.status_code == 404


def test_freeze_uses_stored_draft_not_client_content(tmp_path: Path) -> None:
    database = _database(tmp_path)
    proposal = propose_file_understanding(
        _Gateway(
            {
                "source_kind": "platform_settlement",
                "confidence": 0.9,
                "field_map": {"订单号": "business_key", "金额": "amount"},
                "rationale": "表头符合结算单",
            }
        ),  # type: ignore[arg-type]
        model="test-model",
        headers=["订单号", "金额"],
        sample_rows=[],
        original_name="settlement.csv",
    )
    stored = record_proposal(database, proposal)
    frozen = freeze_understanding(database, stored.proposal_id, operator_name="alice")

    assert frozen.frozen is True
    assert frozen.field_map == {"订单号": "business_key", "金额": "amount"}
    row = database.execute(
        "SELECT status, approved_by FROM rule_version WHERE rule_version_id = ?",
        [frozen.rule_version_id],
    ).fetchone()
    assert row is not None and row[0] == "approved" and row[1] == "alice"
    # The draft survives as the record of what the model first proposed.
    draft = database.execute(
        "SELECT status FROM rule_version WHERE rule_version_id = ?",
        [stored.proposal_id],
    ).fetchone()
    assert draft is not None and draft[0] == "draft"
    database.close()


def test_freeze_rejects_unknown_proposal_and_requires_operator(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    unknown = client.post(
        "/api/v1/intelligence/understand/freeze",
        json={"operatorName": "alice", "proposalId": "rvd_does_not_exist"},
    )
    assert unknown.status_code == 404

    no_operator = client.post(
        "/api/v1/intelligence/understand/freeze",
        json={"operatorName": "  ", "proposalId": "rvd_x"},
    )
    assert no_operator.status_code == 400

    smuggled = client.post(
        "/api/v1/intelligence/understand/freeze",
        json={
            "operatorName": "alice",
            "proposalId": "rvd_x",
            "proposal": {"sourceKind": "platform_settlement"},
        },
    )
    assert smuggled.status_code == 422


def test_proposal_drops_fields_the_file_does_not_have() -> None:
    proposal = propose_file_understanding(
        _Gateway(
            {
                "source_kind": "made_up_kind",
                "confidence": 7,
                "field_map": {
                    "订单号": "business_key",
                    "不存在的列": "amount",
                    "金额": "invented_target",
                },
            }
        ),  # type: ignore[arg-type]
        model="test-model",
        headers=["订单号", "金额"],
        sample_rows=[],
        original_name="orders.csv",
    )
    assert isinstance(proposal, FileUnderstanding)
    assert proposal.source_kind == "unknown"
    assert proposal.confidence == 1.0
    assert proposal.field_map == {"订单号": "business_key"}
