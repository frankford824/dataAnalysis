"""SKU × month P&L projection derived from certified ledger entries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum

from .money import MONEY_PRECISION, MONEY_QUANTUM, amount, sum_money
from .recon import EvidenceRef


class PnlMetric(StrEnum):
    TRANSACTION_RECEIPT = "transaction_receipt"
    TRANSACTION_REFUND = "transaction_refund"
    TRANSACTION_COMPENSATION = "transaction_compensation"
    SOFTWARE_SERVICE_FEE = "software_service_fee"
    MARKETING_EXPENSE = "marketing_expense"
    SHIPPING_EXPENSE = "shipping_expense"
    ORDER_COST = "order_cost"
    RESHIP_COST = "reship_cost"
    COMMISSION = "commission"
    PROCUREMENT = "procurement"
    ADVERTISING = "advertising"


_GROSS_COMPONENTS = (
    PnlMetric.TRANSACTION_RECEIPT,
    PnlMetric.TRANSACTION_REFUND,
    PnlMetric.TRANSACTION_COMPENSATION,
    PnlMetric.SOFTWARE_SERVICE_FEE,
    PnlMetric.MARKETING_EXPENSE,
    PnlMetric.SHIPPING_EXPENSE,
    PnlMetric.ORDER_COST,
    PnlMetric.RESHIP_COST,
    PnlMetric.COMMISSION,
    PnlMetric.PROCUREMENT,
)


@dataclass(frozen=True, slots=True)
class PnlInput:
    sku: str
    sku_name: str
    period_key: str
    metric: PnlMetric
    amount: Decimal
    balance_id: str
    rule_version: str
    evidence: tuple[EvidenceRef, ...]
    certified: bool = True

    def __post_init__(self) -> None:
        if not self.sku or not self.period_key or not self.balance_id:
            raise ValueError("sku, period_key, and balance_id are required")
        if not self.rule_version or not self.evidence:
            raise ValueError("P&L inputs require rule version and evidence")
        object.__setattr__(self, "amount", amount(self.amount))


@dataclass(frozen=True, slots=True)
class PnlRow:
    sku_name: str
    sku: str
    period_key: str
    transaction_receipt: Decimal
    transaction_refund: Decimal
    transaction_compensation: Decimal
    software_service_fee: Decimal
    marketing_expense: Decimal
    shipping_expense: Decimal
    order_cost: Decimal
    reship_cost: Decimal
    commission: Decimal
    procurement: Decimal
    gross_profit: Decimal
    gross_margin: Decimal | None
    advertising: Decimal
    store_profit: Decimal
    profit_margin: Decimal | None
    balance_ids: tuple[str, ...]
    evidence_by_metric: Mapping[PnlMetric, tuple[EvidenceRef, ...]] = field(
        default_factory=dict
    )


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = MONEY_PRECISION + 8
        return (numerator / denominator).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
        )


def build_pnl_view(entries: Iterable[PnlInput]) -> tuple[PnlRow, ...]:
    grouped: dict[tuple[str, str], list[PnlInput]] = {}
    for entry in entries:
        if not entry.certified:
            raise ValueError("uncertified ledger entries cannot enter the P&L view")
        grouped.setdefault((entry.period_key, entry.sku), []).append(entry)

    result: list[PnlRow] = []
    for period_key, sku in sorted(grouped):
        group = sorted(
            grouped[(period_key, sku)],
            key=lambda entry: (
                entry.metric.value,
                entry.balance_id,
                entry.rule_version,
            ),
        )
        names = {entry.sku_name for entry in group}
        if len(names) != 1:
            raise ValueError(f"conflicting SKU names for {sku}")
        totals = {
            metric: sum_money(
                entry.amount for entry in group if entry.metric == metric
            )
            for metric in PnlMetric
        }
        gross_profit = sum_money(totals[metric] for metric in _GROSS_COMPONENTS)
        store_profit = sum_money((gross_profit, totals[PnlMetric.ADVERTISING]))
        evidence_by_metric = {
            metric: tuple(
                sorted(
                    {
                        evidence
                        for entry in group
                        if entry.metric == metric
                        for evidence in entry.evidence
                    }
                )
            )
            for metric in PnlMetric
        }
        receipt = totals[PnlMetric.TRANSACTION_RECEIPT]
        result.append(
            PnlRow(
                sku_name=next(iter(names)),
                sku=sku,
                period_key=period_key,
                transaction_receipt=receipt,
                transaction_refund=totals[PnlMetric.TRANSACTION_REFUND],
                transaction_compensation=totals[
                    PnlMetric.TRANSACTION_COMPENSATION
                ],
                software_service_fee=totals[PnlMetric.SOFTWARE_SERVICE_FEE],
                marketing_expense=totals[PnlMetric.MARKETING_EXPENSE],
                shipping_expense=totals[PnlMetric.SHIPPING_EXPENSE],
                order_cost=totals[PnlMetric.ORDER_COST],
                reship_cost=totals[PnlMetric.RESHIP_COST],
                commission=totals[PnlMetric.COMMISSION],
                procurement=totals[PnlMetric.PROCUREMENT],
                gross_profit=gross_profit,
                gross_margin=_ratio(gross_profit, receipt),
                advertising=totals[PnlMetric.ADVERTISING],
                store_profit=store_profit,
                profit_margin=_ratio(store_profit, receipt),
                balance_ids=tuple(sorted({entry.balance_id for entry in group})),
                evidence_by_metric=evidence_by_metric,
            )
        )
    return tuple(result)
