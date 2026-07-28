"""Invariant contract definitions: five families of deterministic assertions.

Families: equality, conservation, proportionality, uniqueness, completeness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from commerce_harness.kernel.invariants import deterministic_checksum

from .predicate import parse_predicate

FAMILIES = frozenset({
    "equality",
    "conservation",
    "proportionality",
    "uniqueness",
    "completeness",
})

# ``invert`` negates unconditionally; use the side's ``select`` predicate to
# choose which rows it applies to. ``invert_expense`` is kept as an alias for
# packs written before the semantics were pinned down.
SIGN_MODES = frozenset({"as_declared", "invert", "invert_expense", "absolute"})

SIGN_MODE_ALIASES = {"invert_expense": "invert"}


@dataclass(frozen=True, slots=True)
class SideDefinition:
    kinds: tuple[str, ...]
    select: dict[str, Any] | None
    sign: str

    def __post_init__(self) -> None:
        if self.sign not in SIGN_MODES:
            raise ValueError(f"invalid sign mode: {self.sign!r}")
        canonical = SIGN_MODE_ALIASES.get(self.sign)
        if canonical:
            object.__setattr__(self, "sign", canonical)


@dataclass(frozen=True, slots=True)
class ToleranceSpec:
    absolute: Decimal
    relative: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.absolute, float) or isinstance(self.relative, float):
            raise TypeError("tolerance must use Decimal, not float")


@dataclass(frozen=True, slots=True)
class MaterialitySpec:
    single_item: Decimal
    category_cumulative: Decimal
    period_revenue_ratio: Decimal

    def __post_init__(self) -> None:
        for name in ("single_item", "category_cumulative", "period_revenue_ratio"):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(f"materiality.{name} must use Decimal, not float")


@dataclass(frozen=True, slots=True)
class ViolationPolicy:
    legal_dispositions: tuple[str, ...]
    blocks_certification: bool


@dataclass(frozen=True, slots=True)
class InvariantDefinition:
    family: str
    scope: dict[str, Any]
    sides: dict[str, SideDefinition]
    tolerance: ToleranceSpec
    materiality: MaterialitySpec
    on_violation: ViolationPolicy
    blocks_certification: bool
    invariant_id: str = field(default="", init=False)
    title: str = ""
    domain: str = "ecommerce_settlement"
    origin: str = "builtin"

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"invalid invariant family: {self.family!r}")
        object.__setattr__(self, "invariant_id", invariant_id_for(self.canonical_dict()))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "scope": self.scope,
            "sides": {
                name: {
                    "kinds": list(side.kinds),
                    "select": side.select,
                    "sign": side.sign,
                }
                for name, side in self.sides.items()
            },
            "tolerance": {
                "absolute": str(self.tolerance.absolute),
                "relative": str(self.tolerance.relative),
            },
            "materiality": {
                "single_item": str(self.materiality.single_item),
                "category_cumulative": str(self.materiality.category_cumulative),
                "period_revenue_ratio": str(self.materiality.period_revenue_ratio),
            },
            "on_violation": {
                "legal_dispositions": list(self.on_violation.legal_dispositions),
                "blocks_certification": self.on_violation.blocks_certification,
            },
            "blocks_certification": self.blocks_certification,
        }


def invariant_id_for(canonical_dict: dict[str, Any]) -> str:
    """SHA-256 of the canonical definition JSON."""
    return deterministic_checksum(canonical_dict)


def _parse_side(raw: dict[str, Any]) -> SideDefinition:
    kinds = raw.get("kinds", [])
    if not isinstance(kinds, list) or not kinds:
        raise ValueError("side.kinds must be a non-empty list")
    select = raw.get("select")
    if select is not None:
        parse_predicate(select)
    sign = raw.get("sign", "as_declared")
    return SideDefinition(
        kinds=tuple(kinds),
        select=select,
        sign=sign,
    )


def parse_invariant(raw: dict[str, Any]) -> InvariantDefinition:
    """Parse a dict into a validated ``InvariantDefinition``."""
    family = raw.get("family")
    if family not in FAMILIES:
        raise ValueError(f"invalid invariant family: {family!r}")

    scope = raw.get("scope", {})
    if not isinstance(scope, dict):
        raise ValueError("scope must be a dict")

    raw_sides = raw.get("sides", {})
    if not isinstance(raw_sides, dict):
        raise ValueError("sides must be a dict")
    sides = {name: _parse_side(side) for name, side in raw_sides.items()}

    tol_raw = raw.get("tolerance", {})
    tolerance = ToleranceSpec(
        absolute=Decimal(str(tol_raw.get("absolute", "0"))),
        relative=Decimal(str(tol_raw.get("relative", "0"))),
    )

    mat_raw = raw.get("materiality", {})
    materiality = MaterialitySpec(
        single_item=Decimal(str(mat_raw.get("single_item", "500.00"))),
        category_cumulative=Decimal(str(mat_raw.get("category_cumulative", "5000.00"))),
        period_revenue_ratio=Decimal(str(mat_raw.get("period_revenue_ratio", "0.001"))),
    )

    viol_raw = raw.get("on_violation", {})
    on_violation = ViolationPolicy(
        legal_dispositions=tuple(viol_raw.get("legal_dispositions", [])),
        blocks_certification=viol_raw.get("blocks_certification", True),
    )

    blocks_cert = raw.get("blocks_certification", on_violation.blocks_certification)

    return InvariantDefinition(
        family=family,
        scope=scope,
        sides=sides,
        tolerance=tolerance,
        materiality=materiality,
        on_violation=on_violation,
        blocks_certification=blocks_cert,
        title=raw.get("title", ""),
        domain=raw.get("domain", "ecommerce_settlement"),
        origin=raw.get("origin", "builtin"),
    )


def load_invariants_from_json_path(path: str | Path) -> list[InvariantDefinition]:
    """Load a list of invariant definitions from a JSON file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("invariants JSON must be a list")
    return [parse_invariant(item) for item in data]
