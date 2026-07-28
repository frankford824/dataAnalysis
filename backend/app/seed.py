from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import DashboardAsset, Enterprise, MetricDefinition, PlatformAccount, SemanticModelVersion, SourceBinding, SourceDefinition, Store
from .standard_model import MODEL_KEY, MODEL_VERSION, metric_definitions, published_definition
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
            platform_account = PlatformAccount(enterprise_id=enterprise.id, name=f"{platform} main", platform=platform, external_account_id=f"{platform}-account", status="active", version=1, effective_from=activation, created_by="seed", approved_by="seed")
            db.add(platform_account)
            db.flush()
            store = Store(enterprise_id=enterprise.id, name=store_name, platform_account_id=platform_account.id, status="active", version=1, effective_from=activation, activation_at=activation, external_store_id=f"{platform}-demo", created_by="seed")
            db.add(store)
            db.flush()
            order_source = SourceDefinition(enterprise_id=enterprise.id, name="Standard order export", status="active", version=1, effective_from=activation, activation_at=activation, file_types=["csv", "xlsx", "zip"], recognition={"required_headers": ["order_id", "occurred_at"]}, field_aliases={}, coverage_time_field="occurred_at", data_granularity="day", arrival_frequency="monthly", dedupe_keys=["order_id"], validations=[{"type": "required_field", "field": "order_id"}], import_mode="monthly_snapshot", source_kind="orders", created_by="seed")
            fee_source = SourceDefinition(enterprise_id=enterprise.id, name="Standard platform fee export", status="active", version=1, effective_from=activation, activation_at=activation, file_types=["csv", "xlsx", "zip"], recognition={"required_headers": ["occurred_at", "event_type"]}, field_aliases={}, coverage_time_field="occurred_at", data_granularity="day", arrival_frequency="monthly", dedupe_keys=["event_type", "occurred_at", "store_id"], validations=[], import_mode="monthly_snapshot", source_kind="fees", created_by="seed")
            db.add_all([order_source, fee_source])
            db.flush()
            order_source.validations = [*order_source.validations, {"type": "cross_source_match", "mode": "required_source", "dependency_source_logical_id": fee_source.logical_id, "label": "platform fee export"}]
            fee_source.validations = [{"type": "cross_source_match", "mode": "required_source", "dependency_source_logical_id": order_source.logical_id, "label": "order export"}]
            db.add_all([
                SourceBinding(enterprise_id=enterprise.id, name="Order source binding", source_definition_id=order_source.id, scope_type="store", scope_id=store.id, status="active", version=1, effective_from=activation, created_by="seed", approved_by="seed"),
                SourceBinding(enterprise_id=enterprise.id, name="Fee source binding", source_definition_id=fee_source.id, scope_type="store", scope_id=store.id, status="active", version=1, effective_from=activation, created_by="seed", approved_by="seed"),
            ])
            model = SemanticModelVersion(enterprise_id=enterprise.id, name="E-commerce standard model", status="published", version=MODEL_VERSION, effective_from=activation, industry_template=MODEL_KEY, definition=published_definition(), quality_gates=[{"key": "reconciliation", "required": True}], created_by="seed", approved_by="seed")
            db.add(model)
            db.flush()
            for spec in metric_definitions():
                db.add(MetricDefinition(enterprise_id=enterprise.id, semantic_model_id=model.id, name=spec.name, key=spec.key, expression=spec.expression, format=spec.format, status="published", version=MODEL_VERSION, effective_from=activation, created_by="seed", approved_by="seed"))
            db.add(DashboardAsset(enterprise_id=enterprise.id, name="Commerce overview", status="published", version=1, effective_from=activation, created_by="seed", approved_by="seed", bi_adapter="superset", external_id=SUPERSET_EMBEDDED_DASHBOARD_ID, embed_url=f"/superset/embedded/{SUPERSET_EMBEDDED_DASHBOARD_ID}", definition={"template": "ecommerce_overview", "model_key": MODEL_KEY, "model_version": MODEL_VERSION, "model_checksum": published_definition()["checksum"]}))
        db.commit()


if __name__ == "__main__":
    seed()
