from __future__ import annotations

import json
from decimal import Decimal

import pytest

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.trust_tier import TrustTier, decide_trust_tier

_CLEAN = {
    "blocking_violations": 0,
    "material_unresolved": 0,
    "unexplained_ratio": Decimal("0"),
    "incomplete_components": 0,
}


def test_clean_state_is_certified() -> None:
    assert decide_trust_tier(**_CLEAN) is TrustTier.CERTIFIED


@pytest.mark.parametrize(
    "override",
    [
        {"blocking_violations": 1},
        {"material_unresolved": 1},
    ],
)
def test_structural_failures_block(override: dict[str, object]) -> None:
    assert decide_trust_tier(**{**_CLEAN, **override}) is TrustTier.BLOCKED


@pytest.mark.parametrize(
    "override",
    [
        {"unexplained_ratio": Decimal("0.06")},
        {"incomplete_components": 1},
    ],
)
def test_incomplete_coverage_degrades_to_partial(override: dict[str, object]) -> None:
    assert decide_trust_tier(**{**_CLEAN, **override}) is TrustTier.PARTIAL


def test_unexplained_ratio_at_the_threshold_still_certifies() -> None:
    assert (
        decide_trust_tier(**{**_CLEAN, "unexplained_ratio": Decimal("0.05")})
        is TrustTier.CERTIFIED
    )


def test_blocking_violation_wins_over_partial_signals() -> None:
    assert (
        decide_trust_tier(
            blocking_violations=1,
            material_unresolved=0,
            unexplained_ratio=Decimal("0.5"),
            incomplete_components=3,
        )
        is TrustTier.BLOCKED
    )


def test_float_ratio_is_refused_so_money_never_meets_binary_rounding() -> None:
    with pytest.raises(TypeError):
        decide_trust_tier(**{**_CLEAN, "unexplained_ratio": 0.06})  # type: ignore[arg-type]


def _seed_pnl_cell(database: DuckDBMemory, *, tier: str, sku: str) -> None:
    database.execute(
        """
        INSERT INTO reconciliation_contract (
            contract_id, logical_key, enterprise_id, store_id, platform_code,
            contract_version, effective_from, status, definition_json
        ) VALUES (?, ?, 'e1', 's1', 'taobao', 1, DATE '2026-01-01', 'active', '{}')
        ON CONFLICT DO NOTHING
        """,
        [f"c-{tier}", f"c-{tier}"],
    )
    database.execute(
        """
        INSERT INTO accounting_period (
            period_id, contract_id, store_id, period_start, period_end, status
        ) VALUES (?, ?, 's1', DATE '2026-02-01', DATE '2026-02-28', 'open')
        """,
        [f"p-{tier}", f"c-{tier}"],
    )
    database.execute(
        """
        INSERT INTO run_log (run_id, contract_id, period_id, run_kind, status)
        VALUES (?, ?, ?, 'reconcile', 'succeeded')
        """,
        [f"r-{tier}", f"c-{tier}", f"p-{tier}"],
    )
    database.execute(
        """
        INSERT INTO pnl_cell (
            pnl_cell_id, run_id, period_id, store_id, sku_key, metric,
            definition_id, value, evidence_json, trust_tier
        ) VALUES (?, ?, ?, 's1', ?, 'profit', 'd1', 1.0000, ?, ?)
        """,
        [f"cell-{tier}", f"r-{tier}", f"p-{tier}", sku, json.dumps([]), tier],
    )


def test_only_certified_cells_are_visible_to_performance_pay() -> None:
    with DuckDBMemory() as database:
        database.initialize()
        _seed_pnl_cell(database, tier="certified", sku="SKU-1")
        _seed_pnl_cell(database, tier="partial", sku="SKU-2")
        visible = database.execute(
            """
            SELECT sku_key FROM pnl_cell
            WHERE coalesce(trust_tier, 'certified') = 'certified'
            ORDER BY sku_key
            """
        ).fetchall()
        assert visible == [("SKU-1",)]


def test_pnl_cell_rejects_an_invented_tier() -> None:
    with DuckDBMemory() as database:
        database.initialize()
        with pytest.raises(Exception, match="Constraint"):
            _seed_pnl_cell(database, tier="looks_fine", sku="SKU-3")