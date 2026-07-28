"""Deterministic order → platform settlement → cash reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .contract import ReconciliationContract, ReconciliationSide
from .money import amount, subtract_money, sum_money


@dataclass(frozen=True, slots=True, order=True)
class EvidenceRef:
    file_id: str
    row_no: int
    field: str = ""
    rule_version: str = ""
    source_value: str = ""
    artifact_id: str = ""
    source_member: str = ""
    source_sheet: str = ""
    rule_version_id: str = ""

    def __post_init__(self) -> None:
        if not self.file_id.strip():
            raise ValueError("evidence file_id is required")
        if self.row_no < 1:
            raise ValueError("evidence row_no must be positive")


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    item_id: str
    contract_code: str
    contract_version: str
    source_type: str
    side: ReconciliationSide
    business_key: str
    amount: Decimal
    occurred_at: datetime
    evidence: tuple[EvidenceRef, ...]
    settlement_batch_key: str | None = None
    cash_bridge_key: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id or not self.business_key:
            raise ValueError("item_id and business_key are required")
        if not self.evidence:
            raise ValueError("every reconciliation item requires source evidence")
        if self.settlement_batch_key is not None:
            normalized_batch_key = self.settlement_batch_key.strip()
            object.__setattr__(
                self, "settlement_batch_key", normalized_batch_key or None
            )
        if self.cash_bridge_key is not None:
            normalized_cash_key = self.cash_bridge_key.strip()
            object.__setattr__(self, "cash_bridge_key", normalized_cash_key or None)
        object.__setattr__(self, "amount", amount(self.amount))
        object.__setattr__(self, "attributes", dict(sorted(self.attributes.items())))


@dataclass(frozen=True, slots=True)
class SettlementCashBridge:
    """Audited mapping from a platform settlement batch to a cash-side key."""

    bridge_id: str
    settlement_batch_key: str
    cash_bridge_key: str
    rule_version: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.bridge_id.strip():
            raise ValueError("bridge_id is required")
        if not self.settlement_batch_key.strip():
            raise ValueError("settlement_batch_key is required")
        if not self.cash_bridge_key.strip():
            raise ValueError("cash_bridge_key is required")
        if not self.rule_version.strip():
            raise ValueError("bridge rule_version is required")
        if not self.evidence:
            raise ValueError("every cash bridge requires source evidence")
        object.__setattr__(self, "bridge_id", self.bridge_id.strip())
        object.__setattr__(
            self, "settlement_batch_key", self.settlement_batch_key.strip()
        )
        object.__setattr__(self, "cash_bridge_key", self.cash_bridge_key.strip())
        object.__setattr__(self, "rule_version", self.rule_version.strip())
        object.__setattr__(self, "evidence", tuple(sorted(set(self.evidence))))


class LinkKind(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


@dataclass(frozen=True, slots=True)
class ReconciliationLink:
    link_id: str
    business_key: str
    kind: LinkKind
    item_ids: tuple[str, ...]
    rule_version: str
    evidence: tuple[EvidenceRef, ...]
    scope: BalanceScope = field(
        default_factory=lambda: BalanceScope.ORDER_PLATFORM
    )
    cash_bridge_keys: tuple[str, ...] = ()
    bridge_ids: tuple[str, ...] = ()


class BalanceStatus(StrEnum):
    BALANCED = "balanced"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"


class BalanceScope(StrEnum):
    ORDER_PLATFORM = "order_platform"
    PLATFORM_CASH = "platform_cash"


@dataclass(frozen=True, slots=True)
class ReconciliationBalance:
    balance_id: str
    business_key: str
    order_amount: Decimal
    platform_amount: Decimal
    cash_amount: Decimal
    order_to_platform_difference: Decimal
    platform_to_cash_difference: Decimal
    status: BalanceStatus
    item_ids: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    scope: BalanceScope = BalanceScope.ORDER_PLATFORM
    settlement_batch_keys: tuple[str, ...] = ()
    cash_bridge_keys: tuple[str, ...] = ()
    bridge_ids: tuple[str, ...] = ()


class UnresolvedKind(StrEnum):
    MISSING_SIDE = "missing_side"
    AMOUNT_MISMATCH = "amount_mismatch"
    MISSING_CASH_BRIDGE = "missing_cash_bridge"
    AMBIGUOUS_CASH_BRIDGE = "ambiguous_cash_bridge"
    CASH_AMOUNT_MISMATCH = "cash_amount_mismatch"


@dataclass(frozen=True, slots=True)
class UnresolvedBalance:
    unresolved_id: str
    balance_id: str
    business_key: str
    kind: UnresolvedKind
    missing_sides: tuple[ReconciliationSide, ...]
    absolute_exposure: Decimal
    evidence: tuple[EvidenceRef, ...]
    scope: BalanceScope = BalanceScope.ORDER_PLATFORM
    settlement_batch_keys: tuple[str, ...] = ()
    cash_bridge_keys: tuple[str, ...] = ()
    bridge_ids: tuple[str, ...] = ()
    rule_versions: tuple[str, ...] = ()


class CashBridgeStatus(StrEnum):
    LINKED = "linked"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    AMOUNT_MISMATCH = "amount_mismatch"


@dataclass(frozen=True, slots=True)
class CashBridgeOutcome:
    outcome_id: str
    settlement_batch_keys: tuple[str, ...]
    cash_bridge_keys: tuple[str, ...]
    status: CashBridgeStatus
    platform_amount: Decimal
    cash_amount: Decimal
    difference: Decimal
    item_ids: tuple[str, ...]
    bridge_ids: tuple[str, ...]
    rule_versions: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    items: tuple[ReconciliationItem, ...]
    links: tuple[ReconciliationLink, ...]
    balances: tuple[ReconciliationBalance, ...]
    unresolved: tuple[UnresolvedBalance, ...]
    cash_bridge_outcomes: tuple[CashBridgeOutcome, ...]

    def checksum(self) -> str:
        from .invariants import deterministic_checksum

        return deterministic_checksum(self)


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def make_item(
    *,
    contract: ReconciliationContract,
    source_type: str,
    business_key: str,
    value: str | int | Decimal,
    occurred_at: datetime,
    evidence: Iterable[EvidenceRef],
    item_id: str | None = None,
    settlement_batch_key: str | None = None,
    cash_bridge_key: str | None = None,
    attributes: Mapping[str, str] | None = None,
) -> ReconciliationItem:
    requirement = contract.requirement(source_type)
    normalized = amount(value)
    evidence_tuple = tuple(sorted(evidence))
    generated_id = item_id or _stable_id(
        "item",
        {
            "contract": contract.code,
            "version": contract.version,
            "source_type": source_type,
            "business_key": business_key,
            "settlement_batch_key": settlement_batch_key,
            "cash_bridge_key": cash_bridge_key,
            "amount": format(normalized, "f"),
            "occurred_at": occurred_at.isoformat(),
            "evidence": [
                (
                    entry.file_id,
                    entry.row_no,
                    entry.field,
                    entry.rule_version,
                    entry.source_value,
                )
                for entry in evidence_tuple
            ],
        },
    )
    return ReconciliationItem(
        item_id=generated_id,
        contract_code=contract.code,
        contract_version=contract.version,
        source_type=source_type,
        side=requirement.side,
        business_key=business_key,
        amount=normalized,
        occurred_at=occurred_at,
        evidence=evidence_tuple,
        settlement_batch_key=settlement_batch_key,
        cash_bridge_key=cash_bridge_key,
        attributes=attributes or {},
    )


def _link_kind(
    items: list[ReconciliationItem],
    left_side: ReconciliationSide,
    right_side: ReconciliationSide,
) -> LinkKind:
    left_count = sum(item.side == left_side for item in items)
    right_count = sum(item.side == right_side for item in items)
    if left_count <= 1 and right_count <= 1:
        return LinkKind.ONE_TO_ONE
    if left_count == 1 and right_count > 1:
        return LinkKind.ONE_TO_MANY
    if left_count > 1 and right_count == 1:
        return LinkKind.MANY_TO_ONE
    return LinkKind.MANY_TO_MANY


def _unique_evidence(items: Iterable[ReconciliationItem]) -> tuple[EvidenceRef, ...]:
    return tuple(sorted({entry for item in items for entry in item.evidence}))


def _bridge_evidence(
    items: Iterable[ReconciliationItem],
    bridges: Iterable[SettlementCashBridge],
) -> tuple[EvidenceRef, ...]:
    return tuple(
        sorted(
            {entry for item in items for entry in item.evidence}
            | {entry for bridge in bridges for entry in bridge.evidence}
        )
    )


def _cash_bridge_components(
    bridges: tuple[SettlementCashBridge, ...],
) -> tuple[
    tuple[tuple[str, ...], tuple[str, ...], tuple[SettlementCashBridge, ...]], ...
]:
    """Return deterministic connected components of the bipartite bridge graph."""

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for bridge in bridges:
        settlement_node = ("settlement", bridge.settlement_batch_key)
        cash_node = ("cash", bridge.cash_bridge_key)
        adjacency.setdefault(settlement_node, set()).add(cash_node)
        adjacency.setdefault(cash_node, set()).add(settlement_node)

    components: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[SettlementCashBridge, ...]]
    ] = []
    visited: set[tuple[str, str]] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        pending = [start]
        component_nodes: set[tuple[str, str]] = set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            component_nodes.add(node)
            pending.extend(sorted(adjacency[node] - visited, reverse=True))
        settlement_keys = tuple(
            sorted(value for kind, value in component_nodes if kind == "settlement")
        )
        cash_keys = tuple(
            sorted(value for kind, value in component_nodes if kind == "cash")
        )
        component_bridges = tuple(
            bridge
            for bridge in bridges
            if bridge.settlement_batch_key in settlement_keys
            and bridge.cash_bridge_key in cash_keys
        )
        components.append((settlement_keys, cash_keys, component_bridges))
    return tuple(components)


def reconcile_items(
    items: Iterable[ReconciliationItem],
    contract: ReconciliationContract,
    *,
    link_rule_version: str,
    cash_bridges: Iterable[SettlementCashBridge] = (),
    tolerance: str | Decimal = "0.0100",
) -> ReconciliationResult:
    """Reconcile order keys, then bridge platform settlement batches to cash.

    A 1:N or N:1 bridge component is aggregated deterministically. N:M bridge
    components are kept unresolved because allocating them would require a
    separate, explicit business rule.
    """

    if not link_rule_version:
        raise ValueError("link_rule_version is required")
    tolerance_amount = abs(amount(tolerance))
    ordered_items = tuple(sorted(items, key=lambda item: item.item_id))
    if len({item.item_id for item in ordered_items}) != len(ordered_items):
        raise ValueError("item_id must be unique")
    ordered_bridges = tuple(sorted(cash_bridges, key=lambda bridge: bridge.bridge_id))
    if len({bridge.bridge_id for bridge in ordered_bridges}) != len(ordered_bridges):
        raise ValueError("bridge_id must be unique")

    order_platform_groups: dict[str, list[ReconciliationItem]] = {}
    for item in ordered_items:
        if (
            item.contract_code != contract.code
            or item.contract_version != contract.version
        ):
            raise ValueError("item contract does not match reconciliation contract")
        requirement = contract.requirement(item.source_type)
        if requirement.side != item.side:
            raise ValueError("item side conflicts with contract source requirement")
        if item.side in (ReconciliationSide.ORDER, ReconciliationSide.PLATFORM):
            order_platform_groups.setdefault(item.business_key, []).append(item)

    links: list[ReconciliationLink] = []
    balances: list[ReconciliationBalance] = []
    unresolved: list[UnresolvedBalance] = []
    bridge_outcomes: list[CashBridgeOutcome] = []

    # Stage 1: order and platform rows share the order-level business key.
    for business_key in sorted(order_platform_groups):
        group = sorted(
            order_platform_groups[business_key], key=lambda item: item.item_id
        )
        evidence = _unique_evidence(group)
        present_sides = {item.side for item in group}
        if {
            ReconciliationSide.ORDER,
            ReconciliationSide.PLATFORM,
        }.issubset(present_sides):
            item_ids = tuple(item.item_id for item in group)
            links.append(
                ReconciliationLink(
                    link_id=_stable_id(
                        "link",
                        (business_key, item_ids, link_rule_version),
                    ),
                    business_key=business_key,
                    kind=_link_kind(
                        group,
                        ReconciliationSide.ORDER,
                        ReconciliationSide.PLATFORM,
                    ),
                    item_ids=item_ids,
                    rule_version=link_rule_version,
                    evidence=evidence,
                    scope=BalanceScope.ORDER_PLATFORM,
                )
            )

        side_totals = {
            side: sum_money(item.amount for item in group if item.side == side)
            for side in (ReconciliationSide.ORDER, ReconciliationSide.PLATFORM)
        }
        order_difference = subtract_money(
            side_totals[ReconciliationSide.PLATFORM],
            side_totals[ReconciliationSide.ORDER],
        )
        missing = tuple(
            side
            for side in (ReconciliationSide.ORDER, ReconciliationSide.PLATFORM)
            if side not in present_sides
        )
        if not missing and abs(order_difference) <= tolerance_amount:
            status = BalanceStatus.BALANCED
        else:
            status = BalanceStatus.UNRESOLVED

        item_ids = tuple(item.item_id for item in group)
        balance_id = _stable_id(
            "balance",
            (
                business_key,
                item_ids,
                format(order_difference, "f"),
                BalanceScope.ORDER_PLATFORM.value,
            ),
        )
        balance = ReconciliationBalance(
            balance_id=balance_id,
            business_key=business_key,
            order_amount=side_totals[ReconciliationSide.ORDER],
            platform_amount=side_totals[ReconciliationSide.PLATFORM],
            cash_amount=amount("0"),
            order_to_platform_difference=order_difference,
            platform_to_cash_difference=amount("0"),
            status=status,
            item_ids=item_ids,
            evidence=evidence,
            scope=BalanceScope.ORDER_PLATFORM,
        )
        balances.append(balance)

        if status != BalanceStatus.BALANCED:
            kind = (
                UnresolvedKind.MISSING_SIDE
                if missing
                else UnresolvedKind.AMOUNT_MISMATCH
            )
            exposure = abs(order_difference)
            unresolved.append(
                UnresolvedBalance(
                    unresolved_id=_stable_id(
                        "unresolved", (balance_id, kind.value, tuple(missing))
                    ),
                    balance_id=balance_id,
                    business_key=business_key,
                    kind=kind,
                    missing_sides=missing,
                    absolute_exposure=exposure,
                    evidence=evidence,
                    scope=BalanceScope.ORDER_PLATFORM,
                    rule_versions=(link_rule_version,),
                )
            )

    if not contract.requires_cash_bridge:
        return ReconciliationResult(
            items=ordered_items,
            links=tuple(links),
            balances=tuple(balances),
            unresolved=tuple(unresolved),
            cash_bridge_outcomes=(),
        )

    platform_by_batch: dict[str, list[ReconciliationItem]] = {}
    platform_without_batch: list[ReconciliationItem] = []
    cash_by_bridge_key: dict[str, list[ReconciliationItem]] = {}
    cash_without_bridge_key: list[ReconciliationItem] = []
    for item in ordered_items:
        if item.side == ReconciliationSide.PLATFORM:
            if item.settlement_batch_key:
                platform_by_batch.setdefault(item.settlement_batch_key, []).append(
                    item
                )
            else:
                platform_without_batch.append(item)
        elif item.side == ReconciliationSide.CASH:
            if item.cash_bridge_key:
                cash_by_bridge_key.setdefault(item.cash_bridge_key, []).append(item)
            else:
                cash_without_bridge_key.append(item)

    linked_settlement_keys = {
        bridge.settlement_batch_key for bridge in ordered_bridges
    }
    linked_cash_keys = {bridge.cash_bridge_key for bridge in ordered_bridges}

    def add_missing_bridge_outcome(
        *,
        settlement_keys: tuple[str, ...],
        cash_keys: tuple[str, ...],
        group_items: list[ReconciliationItem],
        business_key: str,
    ) -> None:
        evidence = _unique_evidence(group_items)
        item_ids = tuple(sorted(item.item_id for item in group_items))
        platform_total = sum_money(
            item.amount
            for item in group_items
            if item.side == ReconciliationSide.PLATFORM
        )
        cash_total = sum_money(
            item.amount
            for item in group_items
            if item.side == ReconciliationSide.CASH
        )
        difference = subtract_money(cash_total, platform_total)
        outcome_id = _stable_id(
            "cash_bridge_outcome",
            ("missing", settlement_keys, cash_keys, item_ids),
        )
        bridge_outcomes.append(
            CashBridgeOutcome(
                outcome_id=outcome_id,
                settlement_batch_keys=settlement_keys,
                cash_bridge_keys=cash_keys,
                status=CashBridgeStatus.MISSING,
                platform_amount=platform_total,
                cash_amount=cash_total,
                difference=difference,
                item_ids=item_ids,
                bridge_ids=(),
                rule_versions=(),
                evidence=evidence,
            )
        )
        balance_id = _stable_id(
            "balance",
            (
                BalanceScope.PLATFORM_CASH.value,
                settlement_keys,
                cash_keys,
                item_ids,
            ),
        )
        balances.append(
            ReconciliationBalance(
                balance_id=balance_id,
                business_key=business_key,
                order_amount=amount("0"),
                platform_amount=platform_total,
                cash_amount=cash_total,
                order_to_platform_difference=amount("0"),
                platform_to_cash_difference=difference,
                status=BalanceStatus.UNRESOLVED,
                item_ids=item_ids,
                evidence=evidence,
                scope=BalanceScope.PLATFORM_CASH,
                settlement_batch_keys=settlement_keys,
                cash_bridge_keys=cash_keys,
            )
        )
        missing_sides: tuple[ReconciliationSide, ...]
        if not group_items:
            missing_sides = (
                ReconciliationSide.PLATFORM,
                ReconciliationSide.CASH,
            )
        elif all(item.side == ReconciliationSide.CASH for item in group_items):
            missing_sides = (ReconciliationSide.PLATFORM,)
        elif all(item.side == ReconciliationSide.PLATFORM for item in group_items):
            missing_sides = (ReconciliationSide.CASH,)
        else:
            missing_sides = ()
        unresolved.append(
            UnresolvedBalance(
                unresolved_id=_stable_id(
                    "unresolved",
                    (balance_id, UnresolvedKind.MISSING_CASH_BRIDGE.value),
                ),
                balance_id=balance_id,
                business_key=business_key,
                kind=UnresolvedKind.MISSING_CASH_BRIDGE,
                missing_sides=missing_sides,
                absolute_exposure=sum_money(
                    (abs(platform_total), abs(cash_total))
                ),
                evidence=evidence,
                scope=BalanceScope.PLATFORM_CASH,
                settlement_batch_keys=settlement_keys,
                cash_bridge_keys=cash_keys,
            )
        )

    platform_without_batch_by_business: dict[str, list[ReconciliationItem]] = {}
    for item in platform_without_batch:
        platform_without_batch_by_business.setdefault(
            item.business_key,
            [],
        ).append(item)
    for business_key in sorted(platform_without_batch_by_business):
        group_items = sorted(
            platform_without_batch_by_business[business_key],
            key=lambda value: value.item_id,
        )
        add_missing_bridge_outcome(
            settlement_keys=(),
            cash_keys=(),
            group_items=group_items,
            business_key=business_key,
        )
    for settlement_key in sorted(set(platform_by_batch) - linked_settlement_keys):
        add_missing_bridge_outcome(
            settlement_keys=(settlement_key,),
            cash_keys=(),
            group_items=sorted(
                platform_by_batch[settlement_key], key=lambda item: item.item_id
            ),
            business_key=settlement_key,
        )
    cash_without_bridge_by_business: dict[str, list[ReconciliationItem]] = {}
    for item in cash_without_bridge_key:
        cash_without_bridge_by_business.setdefault(
            item.business_key,
            [],
        ).append(item)
    for business_key in sorted(cash_without_bridge_by_business):
        group_items = sorted(
            cash_without_bridge_by_business[business_key],
            key=lambda value: value.item_id,
        )
        add_missing_bridge_outcome(
            settlement_keys=(),
            cash_keys=(),
            group_items=group_items,
            business_key=business_key,
        )
    for cash_key in sorted(set(cash_by_bridge_key) - linked_cash_keys):
        add_missing_bridge_outcome(
            settlement_keys=(),
            cash_keys=(cash_key,),
            group_items=sorted(
                cash_by_bridge_key[cash_key], key=lambda item: item.item_id
            ),
            business_key=cash_key,
        )

    # Stage 2: aggregate platform settlement batches and cash rows through
    # explicit bridge records.  1:N and N:1 are safe aggregate relationships;
    # N:M remains unresolved without a separate allocation rule.
    for settlement_keys, cash_keys, component_bridges in _cash_bridge_components(
        ordered_bridges
    ):
        group = sorted(
            [
                item
                for key in settlement_keys
                for item in platform_by_batch.get(key, ())
            ]
            + [
                item
                for key in cash_keys
                for item in cash_by_bridge_key.get(key, ())
            ],
            key=lambda item: item.item_id,
        )
        bridge_ids = tuple(bridge.bridge_id for bridge in component_bridges)
        rule_versions = tuple(
            sorted({bridge.rule_version for bridge in component_bridges})
        )
        evidence = _bridge_evidence(group, component_bridges)
        item_ids = tuple(item.item_id for item in group)
        platform_total = sum_money(
            item.amount for item in group if item.side == ReconciliationSide.PLATFORM
        )
        cash_total = sum_money(
            item.amount for item in group if item.side == ReconciliationSide.CASH
        )
        difference = subtract_money(cash_total, platform_total)
        pair_counts: dict[tuple[str, str], int] = {}
        for bridge in component_bridges:
            pair = (bridge.settlement_batch_key, bridge.cash_bridge_key)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
        ambiguous = (
            len(settlement_keys) > 1 and len(cash_keys) > 1
        ) or any(count > 1 for count in pair_counts.values())
        present_platform = any(
            item.side == ReconciliationSide.PLATFORM for item in group
        )
        present_cash = any(item.side == ReconciliationSide.CASH for item in group)
        missing = tuple(
            side
            for side, present in (
                (ReconciliationSide.PLATFORM, present_platform),
                (ReconciliationSide.CASH, present_cash),
            )
            if not present
        )
        if ambiguous:
            bridge_status = CashBridgeStatus.AMBIGUOUS
            balance_status = BalanceStatus.UNRESOLVED
            unresolved_kind = UnresolvedKind.AMBIGUOUS_CASH_BRIDGE
        elif missing:
            bridge_status = CashBridgeStatus.MISSING
            balance_status = BalanceStatus.UNRESOLVED
            unresolved_kind = UnresolvedKind.MISSING_SIDE
        elif abs(difference) > tolerance_amount:
            bridge_status = CashBridgeStatus.AMOUNT_MISMATCH
            balance_status = BalanceStatus.UNRESOLVED
            unresolved_kind = UnresolvedKind.CASH_AMOUNT_MISMATCH
        else:
            bridge_status = CashBridgeStatus.LINKED
            balance_status = BalanceStatus.BALANCED
            unresolved_kind = None

        outcome_id = _stable_id(
            "cash_bridge_outcome",
            (
                settlement_keys,
                cash_keys,
                bridge_ids,
                rule_versions,
                item_ids,
                bridge_status.value,
                format(difference, "f"),
            ),
        )
        bridge_outcomes.append(
            CashBridgeOutcome(
                outcome_id=outcome_id,
                settlement_batch_keys=settlement_keys,
                cash_bridge_keys=cash_keys,
                status=bridge_status,
                platform_amount=platform_total,
                cash_amount=cash_total,
                difference=difference,
                item_ids=item_ids,
                bridge_ids=bridge_ids,
                rule_versions=rule_versions,
                evidence=evidence,
            )
        )
        business_key = "|".join(settlement_keys)
        balance_id = _stable_id(
            "balance",
            (
                BalanceScope.PLATFORM_CASH.value,
                settlement_keys,
                cash_keys,
                item_ids,
                bridge_ids,
                rule_versions,
                format(difference, "f"),
            ),
        )
        balances.append(
            ReconciliationBalance(
                balance_id=balance_id,
                business_key=business_key,
                order_amount=amount("0"),
                platform_amount=platform_total,
                cash_amount=cash_total,
                order_to_platform_difference=amount("0"),
                platform_to_cash_difference=difference,
                status=balance_status,
                item_ids=item_ids,
                evidence=evidence,
                scope=BalanceScope.PLATFORM_CASH,
                settlement_batch_keys=settlement_keys,
                cash_bridge_keys=cash_keys,
                bridge_ids=bridge_ids,
            )
        )
        if not ambiguous and not missing and group:
            links.append(
                ReconciliationLink(
                    link_id=_stable_id(
                        "link",
                        (
                            BalanceScope.PLATFORM_CASH.value,
                            settlement_keys,
                            cash_keys,
                            item_ids,
                            bridge_ids,
                            rule_versions,
                        ),
                    ),
                    business_key=business_key,
                    kind=_link_kind(
                        group,
                        ReconciliationSide.PLATFORM,
                        ReconciliationSide.CASH,
                    ),
                    item_ids=item_ids,
                    rule_version="|".join(rule_versions),
                    evidence=evidence,
                    scope=BalanceScope.PLATFORM_CASH,
                    cash_bridge_keys=cash_keys,
                    bridge_ids=bridge_ids,
                )
            )
        if unresolved_kind is not None:
            unresolved.append(
                UnresolvedBalance(
                    unresolved_id=_stable_id(
                        "unresolved",
                        (
                            balance_id,
                            unresolved_kind.value,
                            settlement_keys,
                            cash_keys,
                            bridge_ids,
                        ),
                    ),
                    balance_id=balance_id,
                    business_key=business_key,
                    kind=unresolved_kind,
                    missing_sides=missing,
                    absolute_exposure=(
                        sum_money((abs(platform_total), abs(cash_total)))
                        if ambiguous or missing
                        else abs(difference)
                    ),
                    evidence=evidence,
                    scope=BalanceScope.PLATFORM_CASH,
                    settlement_batch_keys=settlement_keys,
                    cash_bridge_keys=cash_keys,
                    bridge_ids=bridge_ids,
                    rule_versions=rule_versions,
                )
            )

    return ReconciliationResult(
        items=ordered_items,
        links=tuple(links),
        balances=tuple(balances),
        unresolved=tuple(unresolved),
        cash_bridge_outcomes=tuple(bridge_outcomes),
    )
