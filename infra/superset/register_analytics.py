"""Idempotently register the platform's read-only certified-data connection."""

from __future__ import annotations

import json
import os
import uuid

from superset.app import create_app


app = create_app()
with app.app_context():
    # Superset model modules read the Flask-local configuration while importing,
    # so they must be loaded only after the application context is active.
    from superset.extensions import db
    from superset.connectors.sqla.models import SqlaTable, SqlMetric, TableColumn
    from superset.models.dashboard import Dashboard
    from superset.models.embedded_dashboard import EmbeddedDashboard
    from superset.models.core import Database
    from superset.models.slice import Slice

    database = db.session.query(Database).filter_by(database_name="Certified Commerce Data").one_or_none()
    if database is None:
        database = Database(database_name="Certified Commerce Data")
        db.session.add(database)

    database.set_sqlalchemy_uri(os.environ["ANALYTICS_DATABASE_URI"])
    database.allow_dml = False
    database.allow_ctas = False
    database.allow_cvas = False
    database.allow_file_upload = False
    database.expose_in_sqllab = False
    database.extra = '{"metadata_params": {}, "engine_params": {"connect_args": {"options": "-c statement_timeout=60000"}}}'
    db.session.commit()

    # Ship a real, usable starter dataset and dashboard. The certified view is
    # created by the platform migration and remains the sole analytical source;
    # no raw or normalized tables are exposed to Superset.
    dataset = (
        db.session.query(SqlaTable)
        .filter_by(database_id=database.id, schema="certified", table_name="sales")
        .one_or_none()
    )
    if dataset is None:
        dataset = SqlaTable(
            database=database,
            schema="certified",
            table_name="sales",
            main_dttm_col="period_start",
            description="Platform-certified commerce facts only",
        )
        dataset.columns = [
            TableColumn(column_name="enterprise_id", type="VARCHAR(36)", groupby=True, filterable=True),
            TableColumn(column_name="store_id", type="VARCHAR(36)", groupby=True, filterable=True),
            TableColumn(column_name="period_start", type="TIMESTAMPTZ", is_dttm=True, filterable=True),
            TableColumn(column_name="grain", type="VARCHAR(16)", groupby=True, filterable=True),
            TableColumn(column_name="row_count", type="INTEGER"),
            TableColumn(column_name="order_count", type="INTEGER"),
            TableColumn(column_name="revenue", type="NUMERIC"),
            TableColumn(column_name="refund", type="NUMERIC"),
            TableColumn(column_name="fees", type="NUMERIC"),
            TableColumn(column_name="profit", type="NUMERIC"),
        ]
        dataset.metrics = [
            SqlMetric(metric_name="order_count", verbose_name="订单数", expression="SUM(order_count)", metric_type="sum"),
            SqlMetric(metric_name="revenue", verbose_name="净销售额", expression="SUM(revenue)", metric_type="sum"),
            SqlMetric(metric_name="refund", verbose_name="退款", expression="SUM(refund)", metric_type="sum"),
            SqlMetric(metric_name="fees", verbose_name="费用", expression="SUM(fees)", metric_type="sum"),
            SqlMetric(metric_name="profit", verbose_name="经营利润", expression="SUM(profit)", metric_type="sum"),
        ]
        db.session.add(dataset)
        db.session.flush()
    column_labels = {
        "enterprise_id": "企业",
        "store_id": "店铺",
        "period_start": "业务日期",
        "grain": "时间口径",
        "row_count": "记录数",
        "order_count": "订单数",
        "revenue": "净销售额",
        "refund": "退款",
        "fees": "平台与广告费用",
        "profit": "经营利润",
    }
    for column in dataset.columns:
        column.verbose_name = column_labels.get(column.column_name, column.column_name)
    if not any(metric.metric_name == "order_count" for metric in dataset.metrics):
        dataset.metrics.append(SqlMetric(metric_name="order_count", verbose_name="订单数", expression="SUM(order_count)", metric_type="sum"))

    chart = db.session.query(Slice).filter_by(slice_name="商析 · 认证经营数据").one_or_none()
    if chart is None:
        chart = Slice(
            slice_name="商析 · 认证经营数据",
            datasource_id=dataset.id,
            datasource_type="table",
            datasource_name="certified.sales",
            viz_type="table",
            params="{}",
            description="由平台质量门禁发布的只读认证数据",
            certified_by="Commerce Analytics Platform",
            certification_details="Deterministic, version-bound published data",
        )
        db.session.add(chart)
        db.session.flush()
    chart.params = json.dumps({
        "datasource": f"{dataset.id}__table",
        "viz_type": "table",
        "query_mode": "aggregate",
        "time_range": "No filter",
        "granularity_sqla": "period_start",
        "groupby": ["period_start"],
        "metrics": ["order_count", "revenue", "refund", "fees", "profit"],
        "row_limit": 1000,
        "order_desc": True,
    }, ensure_ascii=False)

    dashboard = db.session.query(Dashboard).filter_by(slug="commerce-overview").one_or_none()
    if dashboard is None:
        chart_node = f"CHART-{chart.id}"
        row_node = "ROW-CERTIFIED"
        position = {
            "DASHBOARD_VERSION_KEY": "v2",
            "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
            "GRID_ID": {"id": "GRID_ID", "type": "GRID", "parents": ["ROOT_ID"], "children": [row_node]},
            row_node: {
                "id": row_node,
                "type": "ROW",
                "parents": ["ROOT_ID", "GRID_ID"],
                "children": [chart_node],
                "meta": {"background": "BACKGROUND_TRANSPARENT"},
            },
            chart_node: {
                "id": chart_node,
                "type": "CHART",
                "parents": ["ROOT_ID", "GRID_ID", row_node],
                "children": [],
                "meta": {"chartId": chart.id, "height": 50, "width": 12},
            },
        }
        dashboard = Dashboard(
            dashboard_title="商析 · 电商经营概览",
            slug="commerce-overview",
            published=True,
            position_json=json.dumps(position),
            json_metadata=json.dumps({"timed_refresh_immune_slices": [], "show_native_filters": True}),
            description="Platform-managed starter dashboard over certified data",
            certified_by="Commerce Analytics Platform",
        )
        dashboard.slices.append(chart)
        db.session.add(dashboard)
        db.session.flush()
    embedded_id = uuid.UUID("741fec6d-5c6b-4f81-8df2-ec59cf16fb55")
    embedded = db.session.query(EmbeddedDashboard).filter_by(uuid=embedded_id).one_or_none()
    if embedded is None:
        embedded = EmbeddedDashboard(
            uuid=embedded_id,
            dashboard_id=dashboard.id,
            allow_domain_list=os.getenv("SUPERSET_EMBED_ALLOWED_DOMAINS", "http://localhost:3000"),
        )
        db.session.add(embedded)
    db.session.commit()

    # Embedded users get a minimal role. Dataset access is granted per enterprise
    # by the backend when a dashboard is published; no broad datasource grant is
    # assigned here.
    security_manager = app.appbuilder.sm
    embedded_role = security_manager.add_role("EmbeddedViewer")
    gamma_role = security_manager.find_role("Gamma")
    if gamma_role and embedded_role:
        blocked = {"datasource access", "database access", "schema access"}
        embedded_role.permissions = [
            permission for permission in gamma_role.permissions
            if permission.permission and permission.permission.name not in blocked
        ]
    db.session.commit()
