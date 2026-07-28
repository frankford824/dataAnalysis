"""Wire a counterfactual experiment to real frozen inputs from the workbench.

Keeps the runner free of database concerns: this module resolves the period,
loads the frozen canonical rows and the currently approved rule set, and hands
the runner a shadow function that re-runs the production kernel.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.spec.rule import RuleDefinition, parse_rule

from .runner import ShadowRunFn
from .shadow import ShadowReconciler


class ExperimentScopeError(RuntimeError):
    """Raised when an experiment scope cannot be resolved to frozen inputs."""


def _candidate_rules(
    baseline: list[RuleDefinition],
    hypothesis: dict[str, Any],
) -> list[RuleDefinition]:
    """Rule set implied by the hypothesis, on top of the approved baseline."""
    kind = str(hypothesis.get("kind") or "")
    if kind == "rule_add":
        raw = hypothesis.get("rule")
        if not isinstance(raw, dict):
            raise ExperimentScopeError("rule_add hypothesis must carry a 'rule' object")
        return [*baseline, parse_rule(raw)]
    if kind == "rule_remove":
        rule_id = str(hypothesis.get("rule_id") or "")
        if not rule_id:
            raise ExperimentScopeError("rule_remove hypothesis must carry 'rule_id'")
        return [rule for rule in baseline if rule.rule_id != rule_id]
    raise ExperimentScopeError(f"unsupported hypothesis kind for shadow run: {kind!r}")


def period_is_locked(database: DuckDBMemory, *, period_id: str) -> bool:
    row = database.execute(
        "SELECT status FROM accounting_period WHERE period_id = ?",
        [period_id],
    ).fetchone()
    if not row:
        return False
    return str(row[0]) in ("preclosed", "closed")


def build_shadow_run(
    database: DuckDBMemory,
    *,
    period_token: str,
    store_id: str | None = None,
    mode: str = "platform_wallet",
) -> tuple[ShadowRunFn, str, Decimal]:
    """Build a shadow run bound to one period's frozen inputs.

    Returns ``(shadow_run, period_id, materiality_floor)``.
    """
    from commerce_harness.kernel.contract import (
        platform_wallet_contract,
        taobao_three_way_contract,
    )
    from commerce_harness.phase_a import (
        _current_canonical_rows,
        _load_active_invariants,
        _load_approved_route_rules,
        _materiality_floor,
        _period_row,
    )

    period_id, contract_id, _store_id, _start, _end = _period_row(
        database, period_token, store_id=store_id,
    )
    platform_code = str(
        database.fetchone_required(
            "SELECT platform_code FROM reconciliation_contract WHERE contract_id = ?",
            [contract_id],
        )[0]
    )
    if mode == "platform_wallet":
        contract = platform_wallet_contract(platform_code)
    elif platform_code == "taobao":
        contract = taobao_three_way_contract()
    else:
        raise ExperimentScopeError(
            f"{platform_code} 尚未启用银行三方桥接合同，无法开展影子实验"
        )

    rows, artifact_manifest = _current_canonical_rows(database, period_id=period_id)
    if not artifact_manifest:
        raise ExperimentScopeError("该账期没有已确认的标准化输入版本，无法开展影子实验")

    invariants = _load_active_invariants(database)
    baseline_rules = _load_approved_route_rules(database)
    floor = _materiality_floor(invariants)

    reconciler = ShadowReconciler(
        rows=rows,
        contract=contract,
        invariants=invariants,
        materiality_floor=floor,
    )

    def shadow_run(
        hypothesis: dict[str, Any],
        _context: dict[str, Any],
    ) -> tuple[Any, Any]:
        before = reconciler.run(list(baseline_rules))
        after = reconciler.run(_candidate_rules(list(baseline_rules), hypothesis))
        return before, after

    return shadow_run, period_id, floor


def persist_experiment_results(
    database: DuckDBMemory,
    record: Any,
    *,
    period_id: str | None,
    store_id: str | None,
) -> None:
    """Write the measured metrics and per-subject deltas of a finished run."""
    from .shadow import deltas_evidence_json, evidence_digest_for

    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM experiment_metric WHERE experiment_id = ?",
            [record.experiment_id],
        )
        connection.execute(
            "DELETE FROM experiment_delta WHERE experiment_id = ?",
            [record.experiment_id],
        )
        for metric, values in sorted((record.metrics or {}).items()):
            connection.execute(
                """
                INSERT INTO experiment_metric (
                    experiment_id, period_id, store_id, metric,
                    before_value, after_value, delta_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record.experiment_id,
                    period_id or "",
                    store_id or "",
                    metric,
                    values["before"],
                    values["after"],
                    values["delta"],
                ],
            )
        for index, delta in enumerate(record.deltas or []):
            connection.execute(
                """
                INSERT INTO experiment_delta (
                    delta_id, experiment_id, subject_kind, subject_key,
                    before_amount, after_amount, is_material, is_reversal,
                    evidence_binding_digest, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    f"{record.experiment_id}:{index:06d}",
                    record.experiment_id,
                    delta["subject_kind"],
                    delta["subject_key"],
                    delta["before_amount"],
                    delta["after_amount"],
                    delta["is_material"],
                    delta["is_reversal"],
                    evidence_digest_for(delta),
                    deltas_evidence_json(delta),
                ],
            )
        connection.execute(
            """
            UPDATE experiment
            SET verdict = ?, verdict_reasons = ?, shadow_run_id = ?,
                output_sha256 = ?, shadow_code_sha = ?,
                decided_at = current_timestamp
            WHERE experiment_id = ?
            """,
            [
                record.verdict,
                json.dumps(record.verdict_reasons, ensure_ascii=False),
                record.shadow_run_id,
                record.output_sha256,
                record.shadow_code_sha,
                record.experiment_id,
            ],
        )
