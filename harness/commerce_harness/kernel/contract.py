"""Explicit reconciliation contracts.

The v1 contract is intentionally not a generic expression language.  It fixes
the three carriers used for Taobao reconciliation and makes every required
source, normalized amount, and business key explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReconciliationSide(StrEnum):
    ORDER = "order"
    PLATFORM = "platform"
    CASH = "cash"


class ReconciliationMode(StrEnum):
    """Supported evidence contracts.

    ``PLATFORM_WALLET`` is a two-source reconciliation: order exports are
    compared with Alipay/WeChat wallet ledgers.  It deliberately does not
    claim an independent bank confirmation.

    ``BANK_THREE_WAY`` adds a separately sourced cash leg and therefore
    requires audited settlement-to-bank bridge records.
    """

    PLATFORM_WALLET = "platform_wallet"
    BANK_THREE_WAY = "bank_three_way"


@dataclass(frozen=True, slots=True)
class SourceRequirement:
    source_type: str
    side: ReconciliationSide
    amount_field: str
    business_key_fields: tuple[str, ...]
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise ValueError("source_type is required")
        if not self.amount_field.strip():
            raise ValueError("amount_field is required")
        if not self.business_key_fields or any(
            not field.strip() for field in self.business_key_fields
        ):
            raise ValueError("at least one business key field is required")


@dataclass(frozen=True, slots=True)
class ReconciliationContract:
    code: str
    version: str
    platform: str
    sources: tuple[SourceRequirement, ...]
    mode: ReconciliationMode = ReconciliationMode.BANK_THREE_WAY
    amount_scale: int = 4
    tolerance_text: str = "0.0100"
    primary_business_key: str = "order_id"
    platform_settlement_batch_key: str = "settlement_batch_id"
    cash_bridge_key: str = "cash_bridge_key"

    def __post_init__(self) -> None:
        if not self.code or not self.version:
            raise ValueError("contract code and version are required")
        source_types = [source.source_type for source in self.sources]
        if len(source_types) != len(set(source_types)):
            raise ValueError("contract source types must be unique")
        sides = {source.side for source in self.sources if source.required}
        required_sides = {
            ReconciliationSide.ORDER,
            ReconciliationSide.PLATFORM,
        }
        if self.mode == ReconciliationMode.BANK_THREE_WAY:
            required_sides.add(ReconciliationSide.CASH)
        if sides != required_sides:
            raise ValueError(
                f"{self.mode.value} contract must require "
                + ", ".join(sorted(side.value for side in required_sides))
                + " sides"
            )
        if self.amount_scale != 4:
            raise ValueError("v1 kernel supports DECIMAL(38, 4) only")
        if not self.primary_business_key.strip():
            raise ValueError("primary_business_key is required")
        if not self.platform_settlement_batch_key.strip():
            raise ValueError("platform_settlement_batch_key is required")
        if not self.cash_bridge_key.strip():
            raise ValueError("cash_bridge_key is required")

    def requirement(self, source_type: str) -> SourceRequirement:
        for source in self.sources:
            if source.source_type == source_type:
                return source
        raise KeyError(f"source type is not part of contract: {source_type}")

    @property
    def required_source_types(self) -> tuple[str, ...]:
        return tuple(
            source.source_type for source in self.sources if source.required
        )

    @property
    def required_sides(self) -> frozenset[ReconciliationSide]:
        return frozenset(
            source.side for source in self.sources if source.required
        )

    @property
    def requires_cash_bridge(self) -> bool:
        return self.mode == ReconciliationMode.BANK_THREE_WAY


def taobao_three_way_contract(version: str = "1.0.0") -> ReconciliationContract:
    """Return the fixed v1 Taobao order/platform/cash reconciliation contract."""

    return ReconciliationContract(
        code="taobao_three_way",
        version=version,
        platform="taobao",
        mode=ReconciliationMode.BANK_THREE_WAY,
        sources=(
            SourceRequirement(
                source_type="baobei_order",
                side=ReconciliationSide.ORDER,
                amount_field="net_order_amount",
                business_key_fields=("order_id",),
                description="宝贝报表或千牛订单侧净额",
            ),
            SourceRequirement(
                source_type="alipay_ledger",
                side=ReconciliationSide.PLATFORM,
                amount_field="platform_settled_amount",
                business_key_fields=("order_id",),
                description="支付宝账务明细中的平台确认金额",
            ),
            SourceRequirement(
                source_type="bank_statement",
                side=ReconciliationSide.CASH,
                amount_field="actual_cash_amount",
                business_key_fields=("settlement_batch_id",),
                description="银行流水中的实际收付金额",
            ),
            SourceRequirement(
                source_type="alipay_withdrawal",
                side=ReconciliationSide.CASH,
                amount_field="withdrawal_amount",
                business_key_fields=("settlement_batch_id",),
                required=False,
                description="支付宝提现批次，用于连接平台结算与银行流水",
            ),
        ),
    )


def taobao_wallet_contract(version: str = "1.0.0") -> ReconciliationContract:
    """Return the default order-to-platform-wallet contract.

    Alipay is the required Taobao settlement ledger. WeChat is accepted as an
    additional wallet stream when present, but its absence does not invent a
    missing bank leg. Both streams remain platform evidence; they are never
    duplicated onto the cash side.
    """

    return ReconciliationContract(
        code="taobao_wallet",
        version=version,
        platform="taobao",
        mode=ReconciliationMode.PLATFORM_WALLET,
        sources=(
            SourceRequirement(
                source_type="baobei_order",
                side=ReconciliationSide.ORDER,
                amount_field="net_order_amount",
                business_key_fields=("order_id",),
                description="宝贝报表或千牛订单侧净额",
            ),
            SourceRequirement(
                source_type="alipay_ledger",
                side=ReconciliationSide.PLATFORM,
                amount_field="platform_settled_amount",
                business_key_fields=("order_id",),
                description="支付宝平台钱包账务明细",
            ),
            SourceRequirement(
                source_type="wechat_ledger",
                side=ReconciliationSide.PLATFORM,
                amount_field="platform_settled_amount",
                business_key_fields=("order_id",),
                required=False,
                description="微信平台钱包账务明细（存在时参与核对）",
            ),
        ),
    )


def platform_wallet_contract(
    platform: str,
    version: str = "1.0.0",
) -> ReconciliationContract:
    """Return the finite order-to-wallet contract for a supported platform.

    All currently supported exports normalize into the same canonical order
    and platform-ledger carriers. Platform-specific meaning remains explicit
    in the contract code and persisted contract metadata; this is not a
    generic expression language.
    """

    normalized_platform = platform.strip().casefold()
    if not normalized_platform:
        raise ValueError("platform is required")
    if normalized_platform == "taobao":
        return taobao_wallet_contract(version)
    return ReconciliationContract(
        code=f"{normalized_platform}_wallet",
        version=version,
        platform=normalized_platform,
        mode=ReconciliationMode.PLATFORM_WALLET,
        sources=(
            SourceRequirement(
                source_type="baobei_order",
                side=ReconciliationSide.ORDER,
                amount_field="net_order_amount",
                business_key_fields=("order_id",),
                description=f"{normalized_platform} 订单侧净额",
            ),
            SourceRequirement(
                source_type="alipay_ledger",
                side=ReconciliationSide.PLATFORM,
                amount_field="platform_settled_amount",
                business_key_fields=("order_id",),
                description=f"{normalized_platform} 平台资金明细",
            ),
            SourceRequirement(
                source_type="wechat_ledger",
                side=ReconciliationSide.PLATFORM,
                amount_field="platform_settled_amount",
                business_key_fields=("order_id",),
                required=False,
                description=f"{normalized_platform} 补充资金明细",
            ),
        ),
    )
