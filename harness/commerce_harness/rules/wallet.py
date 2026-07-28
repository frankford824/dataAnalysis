"""Deterministic wallet classification and order-key rules.

The module translates only finite behavior supported by frozen Alipay/WeChat
evidence.  It never evaluates workbook formulas and deliberately leaves
unknown or ambiguous text unmatched.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum


class MatchOperator(StrEnum):
    """Supported, inspectable text predicates."""

    EXACT = "exact"
    STARTS_WITH = "starts_with"
    CONTAINS = "contains"
    REGEX = "regex"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """A stable rule definition included in the ruleset checksum."""

    rule_id: str
    version: str
    priority: int
    operator: MatchOperator
    pattern: str
    output: str
    output_kind: str


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """Auditable outcome for one classification or key extraction."""

    matched: bool
    value: str | None
    value_kind: str | None
    rule_id: str | None
    rule_version: str | None
    ruleset_version: str
    ruleset_checksum: str
    evidence_fields: tuple[str, ...]
    unmatched_reason: str | None


@dataclass(frozen=True, slots=True)
class WalletRecord:
    """Minimal wallet fields consumed by the deterministic rules."""

    amount: Decimal
    merchant_order_id: str | None = None
    remark: str | None = None
    occurred_at_text: str | None = None


@dataclass(frozen=True, slots=True)
class WalletEvaluation:
    """Rule outcomes plus the original, untouched monetary value."""

    amount: Decimal
    classification: RuleMatch
    order_key: RuleMatch


_CLASSIFICATION_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        "wallet.classify.order_payment",
        "1.0.0",
        10,
        MatchOperator.REGEX,
        r"^订单.*打款$",
        "0010001|交易收款-交易收款",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.order_refund",
        "1.0.0",
        20,
        MatchOperator.REGEX,
        r"^订单.*退款$",
        "0020001|交易退款-余额退款",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_first_order",
        "1.0.0",
        30,
        MatchOperator.STARTS_WITH,
        "品牌新享-首单拉新计划",
        "品牌新享-首单拉新计划",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_tmall_new_customer_managed",
        "1.0.0",
        40,
        MatchOperator.CONTAINS,
        "品牌新享天猫新客营销托管",
        "品牌新享天猫新客营销托管",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_super_traffic",
        "1.0.0",
        50,
        MatchOperator.STARTS_WITH,
        "品牌新享-超级流量加速软件服务费",
        "品牌新享-超级流量加速软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_new_product",
        "1.0.0",
        60,
        MatchOperator.STARTS_WITH,
        "品牌新享新品孵化软件服务费",
        "品牌新享新品孵化软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.deposit_control_transfer",
        "1.0.0",
        70,
        MatchOperator.REGEX,
        r"^订单.*保证金管控资金使用$",
        "008000200003|保证金-天猫-扣除转移",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.taobao_withdrawal",
        "1.0.0",
        80,
        MatchOperator.STARTS_WITH,
        "淘宝平台提现",
        r"其他支出\收入",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.taobao_new_product_gift",
        "1.0.0",
        90,
        MatchOperator.STARTS_WITH,
        "淘宝新品礼金技术服务费",
        "淘宝新品礼金技术服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.taobao_new_customer_gift",
        "1.0.0",
        100,
        MatchOperator.STARTS_WITH,
        "淘宝新客礼金技术服务费",
        "淘宝新客礼金技术服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_old_customer_gift",
        "1.0.0",
        110,
        MatchOperator.STARTS_WITH,
        "品牌新享淘宝老客礼金软件服务费",
        "品牌新享淘宝老客礼金软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_limited_gift",
        "1.0.0",
        120,
        MatchOperator.STARTS_WITH,
        "品牌新享淘宝限时礼金软件服务费",
        "品牌新享淘宝限时礼金软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_taobao_new_customer_managed",
        "1.0.0",
        130,
        MatchOperator.STARTS_WITH,
        "品牌新享淘宝新客营销托管",
        "品牌新享淘宝新客营销托管",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_tmall_old_customer_acceleration",
        "1.0.0",
        140,
        MatchOperator.STARTS_WITH,
        "品牌新享天猫超级老客加速软件服务费",
        "品牌新享天猫超级老客加速软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_taobao_marketing_managed",
        "1.0.0",
        150,
        MatchOperator.STARTS_WITH,
        "品牌新享-淘宝营销托管软件服务费",
        "品牌新享-淘宝营销托管软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.unbounded_account",
        "1.0.0",
        160,
        MatchOperator.STARTS_WITH,
        "阿里妈妈无界账户",
        "万相台",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.store_transfer_withdrawal",
        "1.0.0",
        170,
        MatchOperator.STARTS_WITH,
        "店铺过户自动提现",
        r"其他支出\收入",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.taote_marketing",
        "1.0.0",
        180,
        MatchOperator.STARTS_WITH,
        "淘特营销推广服务费",
        "淘特营销推广服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_limited_acceleration",
        "1.0.0",
        190,
        MatchOperator.STARTS_WITH,
        "品牌新享-限时加速服务费",
        "品牌新享-限时加速服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_tmall_marketing_managed",
        "1.0.0",
        200,
        MatchOperator.STARTS_WITH,
        "品牌新享-天猫营销托管软件服务费",
        "品牌新享-天猫营销托管软件服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.brand_tmall_new_product_managed",
        "1.0.0",
        210,
        MatchOperator.STARTS_WITH,
        "品牌新享天猫新品营销托管",
        "品牌新享天猫新品营销托管",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.cat_coin_advance",
        "1.0.0",
        220,
        MatchOperator.STARTS_WITH,
        "猫猫币抵扣项目平台垫付资金",
        "猫猫币抵扣项目平台垫付资金",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.pay_later_service",
        "1.0.0",
        230,
        MatchOperator.STARTS_WITH,
        "先用后付技术服务费",
        "先用后付技术服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.deposit_shortfall",
        "1.0.0",
        240,
        MatchOperator.EXACT,
        "保证金额度不足充值",
        "保证金额度不足充值",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.tmall_app_discount",
        "1.0.0",
        250,
        MatchOperator.STARTS_WITH,
        "天猫APP专享折扣服务费",
        "天猫APP专享折扣服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.cat_coin_promotion",
        "1.0.0",
        260,
        MatchOperator.STARTS_WITH,
        "猫猫币抵扣项目推广服务费",
        "猫猫币抵扣项目推广服务费",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.deposit_recharge",
        "1.0.0",
        270,
        MatchOperator.CONTAINS,
        "保证金充值",
        "保证金充值",
        "business_description",
    ),
    RuleSpec(
        "wallet.classify.photosynthesis_service",
        "1.0.0",
        280,
        MatchOperator.CONTAINS,
        "光合平台软件服务费",
        "光合平台软件服务费",
        "business_description",
    ),
)

_ORDER_RULE_MANIFEST: tuple[RuleSpec, ...] = (
    RuleSpec(
        "wallet.order_key.taobao_19",
        "1.0.0",
        10,
        MatchOperator.REGEX,
        r"^\d{19}$",
        "preserve",
        "order_key",
    ),
    RuleSpec(
        "wallet.order_key.t200p",
        "1.0.0",
        20,
        MatchOperator.REGEX,
        r"(?<![A-Z0-9])T200P(\d{19})(?!\d)",
        "captured_19_digits",
        "order_key",
    ),
    RuleSpec(
        "wallet.order_key.deposit_remark",
        "1.0.0",
        30,
        MatchOperator.REGEX,
        r"订单编号：(\d{19})(?!\d)",
        "captured_19_digits",
        "order_key",
    ),
    RuleSpec(
        "wallet.order_key.qt_legacy",
        "1.0.0",
        40,
        MatchOperator.REGEX,
        r"^.{5,}$",
        "QT + first five occurrence-time characters",
        "legacy_grouping_key",
    ),
)

_TAOBAO_ORDER_ID = re.compile(r"^\d{19}$")
_T200P_ORDER_ID = re.compile(r"(?<![A-Z0-9])T200P(\d{19})(?!\d)", re.IGNORECASE)
_DEPOSIT_ORDER_ID = re.compile(r"订单编号：(\d{19})(?!\d)")
_QT_CLASSIFICATIONS = frozenset({r"其他支出\收入", "保证金额度不足充值"})


class WalletRuleSet:
    """Finite deterministic rules extracted from frozen wallet evidence."""

    __slots__ = ("_checksum",)

    ruleset_id = "ecommerce.wallet"
    version = "1.0.0"

    def __init__(self) -> None:
        manifest = {
            "ruleset_id": self.ruleset_id,
            "version": self.version,
            "classification_rules": [asdict(rule) for rule in _CLASSIFICATION_RULES],
            "order_key_rules": [asdict(rule) for rule in _ORDER_RULE_MANIFEST],
        }
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def checksum(self) -> str:
        return self._checksum

    @property
    def classification_rules(self) -> tuple[RuleSpec, ...]:
        return _CLASSIFICATION_RULES

    @property
    def order_key_rules(self) -> tuple[RuleSpec, ...]:
        return _ORDER_RULE_MANIFEST

    def classify_business_description(self, remark: str | None) -> RuleMatch:
        text = (remark or "").strip()
        if not text:
            return self._unmatched(("remark",), "empty_business_text")

        for rule in _CLASSIFICATION_RULES:
            if self._matches(rule, text):
                return self._matched(rule, rule.output, ("remark",))
        return self._unmatched(("remark",), "no_classification_rule_matched")

    def extract_order_key(
        self,
        *,
        merchant_order_id: str | None,
        remark: str | None,
        occurred_at_text: str | None,
        classification_value: str | None,
    ) -> RuleMatch:
        merchant_text = (merchant_order_id or "").strip()
        remark_text = (remark or "").strip()

        if _TAOBAO_ORDER_ID.fullmatch(merchant_text):
            return self._matched(
                _ORDER_RULE_MANIFEST[0],
                merchant_text,
                ("merchant_order_id",),
            )

        exact_prefixed = re.fullmatch(r"T200P(\d{19})", merchant_text, re.IGNORECASE)
        if exact_prefixed:
            return self._matched(
                _ORDER_RULE_MANIFEST[1],
                exact_prefixed.group(1),
                ("merchant_order_id",),
            )

        prefixed_candidates: dict[str, set[str]] = {}
        for field_name, field_value in (
            ("merchant_order_id", merchant_text),
            ("remark", remark_text),
        ):
            matches = set(_T200P_ORDER_ID.findall(field_value))
            if matches:
                prefixed_candidates[field_name] = matches
        all_prefixed = set().union(*prefixed_candidates.values()) if prefixed_candidates else set()
        if len(all_prefixed) == 1:
            return self._matched(
                _ORDER_RULE_MANIFEST[1],
                next(iter(all_prefixed)),
                tuple(prefixed_candidates),
            )
        if len(all_prefixed) > 1:
            return self._unmatched(
                tuple(prefixed_candidates),
                "ambiguous_t200p_order_ids",
            )

        if classification_value == "保证金充值":
            deposit_candidates = set(_DEPOSIT_ORDER_ID.findall(remark_text))
            if len(deposit_candidates) == 1:
                return self._matched(
                    _ORDER_RULE_MANIFEST[2],
                    next(iter(deposit_candidates)),
                    ("remark", "classification"),
                )
            reason = (
                "ambiguous_deposit_order_ids"
                if len(deposit_candidates) > 1
                else "deposit_order_number_missing_or_invalid"
            )
            return self._unmatched(("remark", "classification"), reason)

        if classification_value in _QT_CLASSIFICATIONS:
            occurrence_text = (occurred_at_text or "").strip()
            if len(occurrence_text) < 5:
                return self._unmatched(
                    ("occurred_at_text", "classification"),
                    "qt_reference_time_missing_or_short",
                )
            return self._matched(
                _ORDER_RULE_MANIFEST[3],
                f"QT{occurrence_text[:5]}",
                ("occurred_at_text", "classification"),
            )

        evidence = tuple(
            name
            for name, value in (
                ("merchant_order_id", merchant_text),
                ("remark", remark_text),
            )
            if value
        )
        return self._unmatched(evidence, "no_order_key_rule_matched")

    def evaluate(self, record: WalletRecord) -> WalletEvaluation:
        classification = self.classify_business_description(record.remark)
        order_key = self.extract_order_key(
            merchant_order_id=record.merchant_order_id,
            remark=record.remark,
            occurred_at_text=record.occurred_at_text,
            classification_value=classification.value,
        )
        return WalletEvaluation(
            amount=record.amount,
            classification=classification,
            order_key=order_key,
        )

    def _matched(
        self,
        rule: RuleSpec,
        value: str,
        evidence_fields: tuple[str, ...],
    ) -> RuleMatch:
        return RuleMatch(
            matched=True,
            value=value,
            value_kind=rule.output_kind,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            ruleset_version=self.version,
            ruleset_checksum=self.checksum,
            evidence_fields=evidence_fields,
            unmatched_reason=None,
        )

    def _unmatched(
        self,
        evidence_fields: tuple[str, ...],
        reason: str,
    ) -> RuleMatch:
        return RuleMatch(
            matched=False,
            value=None,
            value_kind=None,
            rule_id=None,
            rule_version=None,
            ruleset_version=self.version,
            ruleset_checksum=self.checksum,
            evidence_fields=evidence_fields,
            unmatched_reason=reason,
        )

    @staticmethod
    def _matches(rule: RuleSpec, text: str) -> bool:
        if rule.operator is MatchOperator.EXACT:
            return text == rule.pattern
        if rule.operator is MatchOperator.STARTS_WITH:
            return text.startswith(rule.pattern)
        if rule.operator is MatchOperator.CONTAINS:
            return rule.pattern in text
        if rule.operator is MatchOperator.REGEX:
            return re.fullmatch(rule.pattern, text) is not None
        return False
