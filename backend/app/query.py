from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlglot
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlglot import exp

from .config import get_settings


ALLOWED_TABLES = {"certified_sales"}
FORBIDDEN_FUNCTION_PREFIXES = ("read_", "write_", "pg_", "dblink", "http", "system")


def validate_readonly_sql(sql: str) -> str:
    if ";" in sql.rstrip().rstrip(";"):
        raise HTTPException(status_code=422, detail="multiple SQL statements are not allowed")
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise HTTPException(status_code=422, detail="invalid SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise HTTPException(status_code=422, detail="only a single SELECT query is allowed")
    statement = statements[0]
    if statement.args.get("with") or statement.args.get("with_"):
        raise HTTPException(status_code=422, detail="custom CTEs are not allowed")
    tables = {table.name.lower() for table in statement.find_all(exp.Table)}
    if not tables or not tables.issubset(ALLOWED_TABLES):
        raise HTTPException(status_code=422, detail="query may only read certified_sales")
    for function in statement.find_all(exp.Func):
        name = function.sql_name().lower()
        if name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise HTTPException(status_code=422, detail=f"function is not allowed: {name}")
    settings = get_settings()
    limit = statement.args.get("limit")
    if limit:
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int or int(expression.this) > settings.sql_max_rows:
            raise HTTPException(status_code=422, detail=f"LIMIT must be an integer no greater than {settings.sql_max_rows}")
    else:
        statement = statement.limit(settings.sql_max_rows)
    return statement.sql(dialect="postgres")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value.isoformat() if hasattr(value, "isoformat") else value


def execute_certified_query(db: Session, enterprise_id: str, sql: str, store_ids: set[str] | None = None) -> dict[str, Any]:
    safe_sql = validate_readonly_sql(sql)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # PostgreSQL does not accept a bind parameter in SET syntax. set_config
        # preserves parameterization while keeping the timeout transaction-local.
        db.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{get_settings().sql_timeout_seconds}s"},
        )
    store_clause = ""
    parameters: dict[str, Any] = {"enterprise_id": enterprise_id}
    if store_ids is not None:
        if not store_ids:
            store_clause = " AND 1 = 0"
        else:
            placeholders = []
            for index, store_id in enumerate(sorted(store_ids)):
                key = f"store_{index}"
                placeholders.append(f":{key}")
                parameters[key] = store_id
            store_clause = f" AND store_id IN ({', '.join(placeholders)})"
    certified_cte = f"""
        WITH certified_sales AS (
          SELECT enterprise_id, store_id, period_start, grain, row_count, order_count,
                 revenue, refund, platform_fee, advertising_fee, shipping_fee, product_cost, fees, profit
          FROM certified_aggregates
          WHERE enterprise_id = :enterprise_id {store_clause}
        )
    """
    result = db.execute(text(f"{certified_cte} {safe_sql}"), parameters)
    columns = list(result.keys())
    rows = [[_json_value(value) for value in row] for row in result.fetchall()]
    return {"columns": columns, "rows": rows, "row_count": len(rows)}
