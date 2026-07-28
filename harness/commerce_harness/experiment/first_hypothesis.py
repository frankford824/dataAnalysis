"""First falsification experiment: alipay service-fee rows as legal single-sided.

This module only builds the hypothesis payload. The counterfactual is executed
by ``experiment.wiring.build_shadow_run``, which re-runs the production kernel
over the period's frozen rows; estimating the outcome instead would defeat the
purpose of the experiment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLATFORM_FEE_RULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "packs"
    / "builtin"
    / "ecommerce_settlement"
    / "rules"
    / "platform_fee_route.json"
)


def load_platform_fee_route_rule() -> dict[str, Any]:
    return json.loads(PLATFORM_FEE_RULE_PATH.read_text(encoding="utf-8"))


def build_first_hypothesis() -> dict[str, Any]:
    rule = load_platform_fee_route_rule()
    return {
        "kind": "rule_add",
        "period_token": "2602",
        "store_id": "store_xibishun",
        "rule": rule,
        "note": (
            "Route negative alipay service-fee rows as legal_single_sided "
            "into platform_fee; wechat control gap stays a separate experiment."
        ),
    }


def first_hypothesis_scope() -> dict[str, Any]:
    """Experiment scope for the first hypothesis, as stored on the record."""
    hypothesis = build_first_hypothesis()
    return {
        "period_token": hypothesis["period_token"],
        "store_id": hypothesis["store_id"],
    }
