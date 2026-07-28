"""Rule DSL: five action types for the closed rule system.

Actions: classify, route, extract, map, derive.
Only ``route`` is fully executable; the other four are parse/validate only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum

from .predicate import Predicate, evaluate_predicate, parse_predicate

RULE_ACTIONS = frozenset({"classify", "route", "extract", "map", "derive"})

ROUTE_PARTICIPATIONS = frozenset({
    "two_sided",
    "legal_single_sided",
    "excluded",
})


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    rule_id: str
    action: str
    select: Predicate
    raw_select: dict[str, Any]
    participation: str | None = None
    posting_target: str | None = None
    rationale: str = ""
    extra: dict[str, Any] | None = None

    def checksum(self) -> str:
        return deterministic_checksum({
            "action": self.action,
            "select": self.raw_select,
            "participation": self.participation,
            "posting_target": self.posting_target,
        })


def _validate_classify(raw: dict[str, Any]) -> dict[str, Any]:
    if "category" not in raw and "posting_target" not in raw:
        raise ValueError("classify rule must specify 'category' or 'posting_target'")
    return {"category": raw.get("category"), "posting_target": raw.get("posting_target")}


def _validate_route(raw: dict[str, Any]) -> dict[str, Any]:
    participation = raw.get("participation")
    if participation not in ROUTE_PARTICIPATIONS:
        raise ValueError(
            f"route participation must be one of {ROUTE_PARTICIPATIONS}, got {participation!r}"
        )
    posting_target = raw.get("posting_target")
    if participation == "legal_single_sided" and not posting_target:
        raise ValueError("legal_single_sided routes must specify posting_target")
    return {"participation": participation, "posting_target": posting_target}


def _validate_extract(raw: dict[str, Any]) -> dict[str, Any]:
    if "source_field" not in raw or "target_field" not in raw:
        raise ValueError("extract rule must specify 'source_field' and 'target_field'")
    return {
        "source_field": raw["source_field"],
        "target_field": raw["target_field"],
        "shape": raw.get("shape"),
    }


def _validate_map(raw: dict[str, Any]) -> dict[str, Any]:
    if "lookup_table" not in raw:
        raise ValueError("map rule must specify 'lookup_table'")
    return {"lookup_table": raw["lookup_table"]}


def _validate_derive(raw: dict[str, Any]) -> dict[str, Any]:
    if "formula" not in raw:
        raise ValueError("derive rule must specify 'formula'")
    return {"formula": raw["formula"]}


_VALIDATORS = {
    "classify": _validate_classify,
    "route": _validate_route,
    "extract": _validate_extract,
    "map": _validate_map,
    "derive": _validate_derive,
}


def parse_rule(raw: dict[str, Any]) -> RuleDefinition:
    """Parse a dict into a validated ``RuleDefinition``.

    Raises ``ValueError`` on any structural violation.
    """
    action = raw.get("action")
    if action not in RULE_ACTIONS:
        raise ValueError(f"invalid rule action: {action!r}")

    raw_select = raw.get("select")
    if raw_select is None:
        raise ValueError("rule must have a 'select' predicate")
    select = parse_predicate(raw_select)

    validator = _VALIDATORS[action]
    extra = validator(raw)

    participation = extra.get("participation")
    posting_target = extra.get("posting_target")

    rule_id = raw.get("rule_id", deterministic_checksum({
        "action": action,
        "select": raw_select,
        "participation": participation,
        "posting_target": posting_target,
    }))

    return RuleDefinition(
        rule_id=rule_id,
        action=action,
        select=select,
        raw_select=raw_select,
        participation=participation,
        posting_target=posting_target,
        rationale=raw.get("rationale", ""),
        extra=extra if action not in ("route",) else None,
    )


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The routing verdict for a single row."""

    participation: str
    posting_target: str | None
    rule_id: str | None

    @property
    def enters_reconciliation(self) -> bool:
        return self.participation == "two_sided"


TWO_SIDED_DEFAULT = RouteDecision(
    participation="two_sided",
    posting_target=None,
    rule_id=None,
)


@dataclass(frozen=True, slots=True)
class RoutedRow:
    row: dict[str, Any]
    participation: str
    posting_target: str | None
    rule_id: str


def route_rules_only(
    rules: Sequence[RuleDefinition],
) -> tuple[RuleDefinition, ...]:
    """Filter to route actions once so callers can hoist it out of row loops."""
    return tuple(rule for rule in rules if rule.action == "route")


def decide_route(
    row: dict[str, Any],
    route_rules: Sequence[RuleDefinition],
) -> RouteDecision:
    """Decide how a single row participates in reconciliation.

    First matching rule wins, so rule order is part of the rule set identity.
    Rows matching nothing stay two-sided, which keeps routing opt-in.
    """
    for rule in route_rules:
        if evaluate_predicate(rule.select, row):
            return RouteDecision(
                participation=rule.participation or "legal_single_sided",
                posting_target=rule.posting_target,
                rule_id=rule.rule_id,
            )
    return TWO_SIDED_DEFAULT


def apply_route_rules(
    rows: Sequence[dict[str, Any]],
    rules: Sequence[RuleDefinition],
) -> tuple[list[dict[str, Any]], list[RoutedRow]]:
    """Apply route rules to rows, splitting into two-sided and routed.

    Returns ``(two_sided_rows, routed_rows)`` where routed rows carry
    ``participation`` and ``posting_target`` metadata.
    """
    selected = route_rules_only(rules)

    two_sided: list[dict[str, Any]] = []
    routed: list[RoutedRow] = []

    for row in rows:
        decision = decide_route(row, selected)
        if decision.enters_reconciliation:
            two_sided.append(row)
            continue
        routed.append(RoutedRow(
            row=row,
            participation=decision.participation,
            posting_target=decision.posting_target,
            rule_id=decision.rule_id or "",
        ))

    return two_sided, routed
