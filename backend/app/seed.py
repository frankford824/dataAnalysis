from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import DashboardAsset, Enterprise, MetricDefinition, SemanticModelVersion, SourceDefinition, Store


STANDARD_METRICS = {
    "sales": "sum(revenue)",
    "refund": "sum(refund)",
    "platform_fee": "sum(platform_fee)",
    "advertising_fee": "sum(advertising_fee)",
    "shipping_fee": "sum(shipping_fee)",
    "product_cost": "sum(product_cost)",
    "profit": "sum(revenue-refund-platform_fee-advertising_fee-shipping_fee-product_cost)",
}
SUPERSET_EMBEDDED_DASHBOARD_ID = "741fec6d-5c6b-4f81-8df2-ec59cf16fb55"


def seed() -> None:
    Base.metadata.create_all(engine)
    activation = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        for enterprise_name, store_name, platform in [
            ("Example Outdoor Retail", "Trail Shop", "shopify"),
            ("Example Home Goods", "Living Store", "amazon"),
        ]:
            existing_enterprise = db.scalar(select(Enterprise).where(Enterprise.name == enterprise_name))
            if existing_enterprise:
                dashboard = db.scalar(select(DashboardAsset).where(DashboardAsset.enterprise_id == existing_enterprise.id, DashboardAsset.bi_adapter == "superset"))
                if dashboard and not dashboard.external_id:
                    dashboard.external_id = SUPERSET_EMBEDDED_DASHBOARD_ID
                    dashboard.embed_url = f"/superset/embedded/{SUPERSET_EMBEDDED_DASHBOARD_ID}"
                continue
            enterprise = Enterprise(name=enterprise_name, activation_at=activation, effective_from=activation, created_by="seed", approved_by="seed")
            db.add(enterprise)
            db.flush()
            store = Store(enterprise_id=enterprise.id, name=store_name, status="active", version=1, effective_from=activation, activation_at=activation, external_store_id=f"{platform}-demo", created_by="seed")
            db.add(store)
            source = SourceDefinition(enterprise_id=enterprise.id, name="Standard order export", status="active", version=1, effective_from=activation, activation_at=activation, file_types=["csv", "xlsx", "zip"], recognition={"required_headers": ["order_id", "occurred_at"]}, field_aliases={}, coverage_time_field="occurred_at", data_granularity="day", arrival_frequency="daily", dedupe_keys=["order_id"], validations=[{"type": "required_field", "field": "order_id"}], created_by="seed")
            db.add(source)
            model = SemanticModelVersion(enterprise_id=enterprise.id, name="E-commerce standard model", status="published", version=1, effective_from=activation, industry_template="ecommerce_standard", definition={"facts": ["sales", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "profit"], "dimensions": ["store", "date"]}, quality_gates=[{"key": "reconciliation", "required": True}], created_by="seed", approved_by="seed")
            db.add(model)
            db.flush()
            for key, expression in STANDARD_METRICS.items():
                db.add(MetricDefinition(enterprise_id=enterprise.id, semantic_model_id=model.id, name=key.replace("_", " ").title(), key=key, expression=expression, status="published", version=1, effective_from=activation, created_by="seed", approved_by="seed"))
            db.add(DashboardAsset(enterprise_id=enterprise.id, name="Commerce overview", status="published", version=1, effective_from=activation, created_by="seed", approved_by="seed", bi_adapter="superset", external_id=SUPERSET_EMBEDDED_DASHBOARD_ID, embed_url=f"/superset/embedded/{SUPERSET_EMBEDDED_DASHBOARD_ID}", definition={"template": "ecommerce_overview", "metrics": list(STANDARD_METRICS)}))
        db.commit()


if __name__ == "__main__":
    seed()
