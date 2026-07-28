from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MetricDefinition, SemanticModelVersion


MODEL_KEY = "ecommerce_standard"
MODEL_VERSION = 1
AMOUNT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class MetricSpec:
    key: str
    name: str
    expression: str
    format: str = "currency"


METRICS = (
    MetricSpec("sales", "销售", "sum(revenue)"),
    MetricSpec("refund", "退款", "sum(refund)"),
    MetricSpec("platform_fee", "平台费", "sum(platform_fee)"),
    MetricSpec("advertising_fee", "广告费", "sum(advertising_fee)"),
    MetricSpec("shipping_fee", "运费", "sum(shipping_fee)"),
    MetricSpec("fees", "费用", "sum(platform_fee + advertising_fee + shipping_fee)"),
    MetricSpec("product_cost", "商品成本", "sum(product_cost)"),
    MetricSpec("profit", "经营利润", "sum(revenue - refund - platform_fee - advertising_fee - shipping_fee - product_cost)"),
    MetricSpec("order_count", "订单数", "count_distinct(order_id where event_type in sale,order)" , "integer"),
)


def registry_payload() -> dict[str, Any]:
    return {
        "key": MODEL_KEY,
        "version": MODEL_VERSION,
        "facts": ["sales", "refund", "platform_fee", "advertising_fee", "shipping_fee", "fees", "product_cost", "profit", "order_count"],
        "dimensions": ["store", "date", "platform"],
        "metrics": [spec.__dict__ for spec in METRICS],
        "amount_precision": "Numeric(20,4)",
        "order_event_types": ["sale", "order"],
    }


def registry_checksum() -> str:
    encoded = json.dumps(registry_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def published_definition() -> dict[str, Any]:
    return {**registry_payload(), "checksum": registry_checksum(), "editable": False}


def metric_definitions() -> tuple[MetricSpec, ...]:
    return METRICS


def quantize(value: Any) -> Decimal:
    parsed = Decimal(str(value if value is not None else 0)).quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN)
    if abs(parsed) >= Decimal("10000000000000000"):
        raise HTTPException(status_code=422, detail="amount exceeds Numeric(20,4)")
    return parsed


def calculate_amounts(values: dict[str, Any]) -> dict[str, Decimal]:
    revenue = quantize(values.get("revenue"))
    refund = quantize(values.get("refund"))
    platform_fee = quantize(values.get("platform_fee"))
    advertising_fee = quantize(values.get("advertising_fee"))
    shipping_fee = quantize(values.get("shipping_fee"))
    product_cost = quantize(values.get("product_cost"))
    fees = quantize(platform_fee + advertising_fee + shipping_fee)
    profit = quantize(revenue - refund - fees - product_cost)
    return {
        "revenue": revenue,
        "refund": refund,
        "platform_fee": platform_fee,
        "advertising_fee": advertising_fee,
        "shipping_fee": shipping_fee,
        "product_cost": product_cost,
        "fees": fees,
        "profit": profit,
    }


def is_order_event(event_type: str | None, source_kind: str) -> bool:
    if source_kind not in {"orders", "mixed"}:
        return False
    return str(event_type or "").strip().lower() in {"sale", "order"}


def validate_published_model(db: Session, model: SemanticModelVersion) -> str:
    definition = model.definition or {}
    expected_checksum = registry_checksum()
    if (
        model.industry_template != MODEL_KEY
        or definition.get("key") != MODEL_KEY
        or definition.get("version") != MODEL_VERSION
        or definition.get("checksum") != expected_checksum
        or definition.get("editable") is not False
    ):
        raise HTTPException(status_code=409, detail="标准经营模型版本校验失败，请由管理员重新发布内置模型")
    actual = {
        item.key: (item.expression, item.format)
        for item in db.scalars(
            select(MetricDefinition).where(
                MetricDefinition.enterprise_id == model.enterprise_id,
                MetricDefinition.semantic_model_id == model.id,
                MetricDefinition.status == "published",
            )
        ).all()
    }
    expected = {spec.key: (spec.expression, spec.format) for spec in METRICS}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(expected) & set(actual) if expected[key] != actual[key])
        detail = "；".join(
            part
            for part in (
                f"缺少：{', '.join(missing)}" if missing else "",
                f"多余：{', '.join(extra)}" if extra else "",
                f"定义变化：{', '.join(changed)}" if changed else "",
            )
            if part
        )
        raise HTTPException(
            status_code=409,
            detail=f"标准经营指标元数据与内置执行器不一致，已阻止发布（{detail}）",
        )
    return expected_checksum
