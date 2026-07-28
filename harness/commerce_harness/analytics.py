from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class AnalyticsValidationError(ValueError):
    """Raised when a public analytics filter is outside the known catalog."""


_ZERO = Decimal("0.0000")
_MONEY_QUANTUM = Decimal("0.0001")
_PERIOD_RE = re.compile(
    r"(?<!\d)(?:(20\d{2})(0[1-9]|1[0-2])|(\d{2})(0[1-9]|1[0-2]))"
)
_PATH_SPLIT_RE = re.compile(r"[\\/]+")
_MOJIBAKE_MARKERS = ("Ã", "Â", "â", "ð", "æ", "å", "ä", "ç", "é")
_NON_STORE_TOKENS = {
    "店铺",
    "店铺数据",
    "店铺资料",
    "模板",
    "模版",
    "示例",
    "样例",
    "参考",
    "汇总",
    "测试",
    "空白",
    "备份",
    "归档",
    "archive",
    "processed",
    "原始数据",
    "修改后数据",
    "千牛明细",
    "微信收款",
    "账单汇总",
}
_SOURCE_LABELS = {
    "alipay_ledger": "支付宝流水",
    "wechat_ledger": "微信流水",
    "baobei_order": "订单明细",
    "taobao_platform_fee": "平台费用",
    "freight_statement": "物流费用",
    "product_cost": "商品成本",
    "advertising_statement": "广告费用",
}
_PLATFORM_LABELS = {
    "1688": "1688",
    "douyin": "抖音电商",
    "jd": "京东",
    "pinduoduo": "拼多多",
    "taobao": "淘宝",
    "tmall": "天猫",
}
_CURRENT_CONTRACTS_CTE = """
prepared_contracts AS (
    SELECT
        contract.contract_id,
        contract.enterprise_id,
        contract.store_id,
        contract.definition_json,
        contract.contract_version,
        contract.effective_from,
        contract.created_at,
        coalesce(
            nullif(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                ''
            ),
            contract.store_id
        ) AS store_name,
        CASE
            WHEN lower(trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
            ))) LIKE 'pdd%'
              OR trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
              )) LIKE '拼多多%'
                THEN 'pinduoduo'
            WHEN trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
            )) LIKE '抖店%'
              OR trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
              )) LIKE '抖音%'
                THEN 'douyin'
            WHEN trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
            )) LIKE '京东%'
                THEN 'jd'
            WHEN lower(trim(coalesce(
                json_extract_string(
                    contract.definition_json,
                    '$.store_name'
                ),
                contract.store_id
            ))) LIKE '%1688'
                THEN '1688'
            WHEN lower(contract.platform_code) IN ('pdd', 'pinduoduo')
                THEN 'pinduoduo'
            ELSE lower(contract.platform_code)
        END AS platform_id
    FROM reconciliation_contract contract
    WHERE contract.status = 'active'
),
ranked_contracts AS (
    SELECT
        contract.*,
        row_number() OVER (
            PARTITION BY
                contract.enterprise_id,
                contract.platform_id,
                lower(trim(contract.store_name))
            ORDER BY
                contract.created_at DESC,
                contract.contract_version DESC,
                contract.effective_from DESC,
                contract.contract_id DESC
        ) AS position
    FROM prepared_contracts contract
),
current_contracts AS (
    SELECT
        contract_id,
        enterprise_id,
        store_id,
        definition_json,
        platform_id
    FROM ranked_contracts
    WHERE position = 1
)
"""


def _money(value: object) -> str:
    if value is None:
        number = _ZERO
    elif isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            number = _ZERO
    return format(number.quantize(_MONEY_QUANTUM), "f")


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _store_name(definition: object, store_id: str) -> str:
    try:
        payload = json.loads(str(definition or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return store_id
    if isinstance(payload, dict):
        value = payload.get("store_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return store_id


def _filter_catalog(
    database: Any,
    *,
    enterprise_id: str | None = None,
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    records = _rows(
        database.execute(
            f"""
            WITH {_CURRENT_CONTRACTS_CTE}
            SELECT
                period.store_id,
                contract.definition_json,
                contract.platform_id,
                strftime(period.period_start, '%Y-%m') AS period_key,
                cast(min(period.period_start) AS VARCHAR) AS period_start,
                cast(max(period.period_end) AS VARCHAR) AS period_end
            FROM accounting_period period
            JOIN current_contracts contract
              ON contract.contract_id = period.contract_id
            WHERE (? IS NULL OR contract.enterprise_id = ?)
            GROUP BY
                period.store_id,
                contract.definition_json,
                contract.platform_id,
                period_key
            ORDER BY period_key, period.store_id
            """,
            [enterprise_id, enterprise_id],
        )
    )
    store_catalog: dict[str, dict[str, str]] = {}
    platform_ids: set[str] = set()
    periods: dict[str, dict[str, str]] = {}
    for row in records:
        store_id = str(row["store_id"])
        platform_id = str(row["platform_id"])
        platform_ids.add(platform_id)
        store_catalog.setdefault(
            store_id,
            {
                "id": store_id,
                "name": _store_name(row["definition_json"], store_id),
                "platformId": platform_id,
                "platformName": _PLATFORM_LABELS.get(platform_id, platform_id),
            },
        )
        key = str(row["period_key"])
        current = periods.get(key)
        if current is None:
            periods[key] = {
                "value": key,
                "label": f"{key[:4]}年{int(key[5:])}月",
                "fromDate": str(row["period_start"]),
                "toDate": str(row["period_end"]),
            }
        else:
            current["fromDate"] = min(current["fromDate"], str(row["period_start"]))
            current["toDate"] = max(current["toDate"], str(row["period_end"]))
    platforms = [
        {"id": platform_id, "name": _PLATFORM_LABELS.get(platform_id, platform_id)}
        for platform_id in sorted(platform_ids)
    ]
    stores = sorted(store_catalog.values(), key=lambda item: item["name"])
    return platforms, stores, list(periods.values())


def _create_selected_periods(
    database: Any,
    *,
    enterprise_id: str | None,
    platform_id: str,
    store_id: str,
    period: str,
    from_date: date | None,
    to_date: date | None,
) -> None:
    clauses = ["1 = 1"]
    parameters: list[object] = []
    if enterprise_id is not None:
        clauses.append("contract.enterprise_id = ?")
        parameters.append(enterprise_id)
    if platform_id != "all":
        clauses.append("contract.platform_id = ?")
        parameters.append(platform_id)
    if store_id != "all":
        clauses.append("period.store_id = ?")
        parameters.append(store_id)
    if period != "all":
        clauses.append("strftime(period.period_start, '%Y-%m') = ?")
        parameters.append(period)
    if from_date is not None:
        clauses.append("period.period_end >= ?")
        parameters.append(from_date)
    if to_date is not None:
        clauses.append("period.period_start <= ?")
        parameters.append(to_date)

    database.execute("DROP TABLE IF EXISTS analytics_selected_periods")
    database.execute(
        f"""
        CREATE TEMP TABLE analytics_selected_periods AS
        WITH {_CURRENT_CONTRACTS_CTE}
        SELECT
            period.period_id,
            period.store_id,
            contract.platform_id,
            coalesce(
                nullif(json_extract_string(contract.definition_json, '$.store_name'), ''),
                period.store_id
            ) AS store_name,
            strftime(period.period_start, '%Y-%m') AS period_key,
            period.period_start,
            period.period_end
        FROM accounting_period period
        JOIN current_contracts contract
          ON contract.contract_id = period.contract_id
        WHERE {' AND '.join(clauses)}
        """,
        parameters,
    )


def _create_selected_facts(
    database: Any,
    *,
    from_date: date | None,
    to_date: date | None,
) -> None:
    date_clauses: list[str] = []
    parameters: list[object] = []
    if from_date is not None:
        date_clauses.append("cast(effective_occurred_at AS DATE) >= ?")
        parameters.append(from_date)
    if to_date is not None:
        date_clauses.append("cast(effective_occurred_at AS DATE) <= ?")
        parameters.append(to_date)
    date_predicate = (
        f"WHERE {' AND '.join(date_clauses)}" if date_clauses else ""
    )

    database.execute("DROP TABLE IF EXISTS analytics_selected_facts")
    database.execute(
        f"""
        CREATE TEMP TABLE analytics_selected_facts AS
        WITH ranked_runs AS (
            SELECT
                run.run_id,
                run.period_id,
                row_number() OVER (
                    PARTITION BY run.period_id
                    ORDER BY
                        run.finished_at DESC NULLS LAST,
                        run.started_at DESC,
                        run.run_id DESC
                ) AS position
            FROM run_log run
            JOIN analytics_selected_periods selected
              ON selected.period_id = run.period_id
            WHERE run.run_kind = 'reconcile'
              AND run.status = 'succeeded'
        ),
        current_runs AS (
            SELECT run_id, period_id
            FROM ranked_runs
            WHERE position = 1
        ),
        prepared AS (
            SELECT
                item.item_id,
                selected.period_id,
                selected.period_key,
                selected.store_id,
                selected.store_name,
                item.source_kind,
                item.source_record_key,
                item.side,
                item.business_key,
                item.amount,
                item.attributes_json,
                item.created_at,
                coalesce(
                    try_cast(
                        json_extract_string(
                            item.attributes_json, '$.business_date'
                        ) AS TIMESTAMP
                    ),
                    try_cast(
                        json_extract_string(
                            item.attributes_json, '$.accounting_date'
                        ) AS TIMESTAMP
                    ),
                    try_cast(
                        json_extract_string(
                            item.attributes_json, '$.occurred_at'
                        ) AS TIMESTAMP
                    ),
                    cast(item.event_date AS TIMESTAMP)
                ) AS effective_occurred_at
            FROM reconciliation_item item
            JOIN current_runs current
              ON current.run_id = item.run_id
             AND current.period_id = item.period_id
            JOIN analytics_selected_periods selected
              ON selected.period_id = item.period_id
        )
        SELECT
            *,
            cast(effective_occurred_at AS DATE) AS occurred_date,
            CASE
                WHEN side = 'order' THEN coalesce(
                    try_cast(
                        json_extract_string(
                            attributes_json, '$.gross_paid_amount'
                        ) AS DECIMAL(38,4)
                    ),
                    greatest(amount, 0::DECIMAL(38,4))
                )
                ELSE 0::DECIMAL(38,4)
            END AS order_gross,
            CASE
                WHEN side = 'order' THEN abs(coalesce(
                    try_cast(
                        json_extract_string(
                            attributes_json, '$.refund_amount'
                        ) AS DECIMAL(38,4)
                    ),
                    least(amount, 0::DECIMAL(38,4))
                ))
                ELSE 0::DECIMAL(38,4)
            END AS refunds,
            CASE
                WHEN side = 'platform' THEN amount
                ELSE 0::DECIMAL(38,4)
            END AS wallet_net
        FROM prepared
        {date_predicate}
        """,
        parameters,
    )


def _summary(database: Any) -> dict[str, object]:
    row = database.execute(
        """
        SELECT
            coalesce(sum(order_gross), 0)::DECIMAL(38,4),
            coalesce(sum(refunds), 0)::DECIMAL(38,4),
            coalesce(sum(order_gross - refunds), 0)::DECIMAL(38,4),
            count(
                DISTINCT (store_id, business_key)
            ) FILTER (
                WHERE side = 'order'
                  AND business_key IS NOT NULL
                  AND business_key <> ''
            ),
            coalesce(sum(wallet_net), 0)::DECIMAL(38,4),
            count(*) FILTER (WHERE side = 'platform')
        FROM analytics_selected_facts
        """
    ).fetchone()
    assert row is not None
    return {
        "orderGross": _money(row[0]),
        "refunds": _money(row[1]),
        "netSales": _money(row[2]),
        "orderCount": int(row[3]),
        "walletNet": _money(row[4]),
        "transactionCount": int(row[5]),
    }


def _trend(database: Any) -> list[dict[str, object]]:
    rows = database.execute(
        """
        SELECT
            cast(occurred_date AS VARCHAR),
            sum(order_gross)::DECIMAL(38,4),
            sum(refunds)::DECIMAL(38,4),
            sum(order_gross - refunds)::DECIMAL(38,4),
            sum(wallet_net)::DECIMAL(38,4)
        FROM analytics_selected_facts
        WHERE occurred_date IS NOT NULL
        GROUP BY occurred_date
        ORDER BY occurred_date
        """
    ).fetchall()
    return [
        {
            "date": str(row[0]),
            "orderGross": _money(row[1]),
            "refunds": _money(row[2]),
            "netSales": _money(row[3]),
            "walletNet": _money(row[4]),
        }
        for row in rows
    ]


def _store_breakdown(database: Any) -> list[dict[str, object]]:
    rows = database.execute(
        """
        WITH stores AS (
            SELECT DISTINCT store_id, store_name
            FROM analytics_selected_periods
        ),
        totals AS (
            SELECT
                store_id,
                sum(order_gross)::DECIMAL(38,4) AS order_gross,
                sum(refunds)::DECIMAL(38,4) AS refunds,
                sum(order_gross - refunds)::DECIMAL(38,4) AS net_sales,
                count(DISTINCT business_key) FILTER (
                    WHERE side = 'order'
                      AND business_key IS NOT NULL
                      AND business_key <> ''
                ) AS order_count,
                sum(wallet_net)::DECIMAL(38,4) AS wallet_net,
                count(*) FILTER (WHERE side = 'platform') AS transaction_count
            FROM analytics_selected_facts
            GROUP BY store_id
        )
        SELECT
            stores.store_id,
            stores.store_name,
            coalesce(totals.order_gross, 0)::DECIMAL(38,4),
            coalesce(totals.refunds, 0)::DECIMAL(38,4),
            coalesce(totals.net_sales, 0)::DECIMAL(38,4),
            coalesce(totals.order_count, 0),
            coalesce(totals.wallet_net, 0)::DECIMAL(38,4),
            coalesce(totals.transaction_count, 0)
        FROM stores
        LEFT JOIN totals ON totals.store_id = stores.store_id
        ORDER BY stores.store_name
        """
    ).fetchall()
    return [
        {
            "storeId": str(row[0]),
            "storeName": str(row[1]),
            "orderGross": _money(row[2]),
            "refunds": _money(row[3]),
            "netSales": _money(row[4]),
            "orderCount": int(row[5]),
            "walletNet": _money(row[6]),
            "transactionCount": int(row[7]),
        }
        for row in rows
    ]


def _transactions(database: Any, limit: int) -> list[dict[str, object]]:
    records = _rows(
        database.execute(
            """
            SELECT
                CASE
                    WHEN effective_occurred_at IS NULL THEN NULL
                    ELSE strftime(
                        effective_occurred_at, '%Y-%m-%dT%H:%M:%S'
                    )
                END AS occurred_at,
                store_id,
                store_name,
                source_kind,
                amount,
                CASE
                    WHEN amount > 0 THEN 'income'
                    WHEN amount < 0 THEN 'expense'
                    ELSE 'neutral'
                END AS direction,
                coalesce(
                    nullif(
                        json_extract_string(
                            attributes_json, '$.business_description'
                        ),
                        ''
                    ),
                    nullif(
                        json_extract_string(attributes_json, '$.description'),
                        ''
                    ),
                    source_kind
                ) AS business_description,
                business_key
            FROM analytics_selected_facts
            ORDER BY
                effective_occurred_at DESC NULLS LAST,
                created_at DESC,
                item_id DESC
            LIMIT ?
            """,
            [limit],
        )
    )
    result: list[dict[str, object]] = []
    for record in records:
        source_kind = str(record["source_kind"])
        source_label = _SOURCE_LABELS.get(source_kind, "业务记录")
        description = str(record["business_description"]).strip()
        if not description or description == source_kind:
            description = {
                "baobei_order": "订单收入",
                "alipay_ledger": "支付宝收支",
                "wechat_ledger": "微信收支",
            }.get(source_kind, source_label)
        result.append(
            {
            "occurredAt": record["occurred_at"],
            "storeId": str(record["store_id"]),
            "storeName": str(record["store_name"]),
            "sourceKind": source_kind,
            "sourceLabel": source_label,
            "amount": _money(record["amount"]),
            "direction": str(record["direction"]),
            "businessDescription": description,
            "businessKey": (
                str(record["business_key"])
                if record["business_key"] is not None
                else None
            ),
            }
        )
    return result


def _monthly_pnl(database: Any) -> list[dict[str, object]]:
    records = _rows(
        database.execute(
            """
            SELECT
                output.period_id,
                selected.period_key,
                selected.store_id,
                selected.store_name,
                output.source_label,
                output.status AS source_status,
                output.totals_json
            FROM historical_output output
            JOIN analytics_selected_periods selected
              ON selected.period_id = output.period_id
            WHERE output.output_kind = 'pnl_16'
            ORDER BY selected.period_key, selected.store_name, output.created_at
            """
        )
    )
    result: list[dict[str, object]] = []
    for record in records:
        try:
            totals = json.loads(str(record["totals_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            totals = {}
        metrics: dict[str, str] = {}
        if isinstance(totals, dict):
            for metric, payload in totals.items():
                if (
                    isinstance(metric, str)
                    and isinstance(payload, dict)
                    and "value" in payload
                ):
                    metrics[metric] = _money(payload["value"])
        result.append(
            {
                "period": str(record["period_key"]),
                "storeId": str(record["store_id"]),
                "storeName": str(record["store_name"]),
                "sourceLabel": str(record["source_label"]),
                "sourceStatus": str(record["source_status"]),
                "status": "historical_reference",
                "metrics": metrics,
            }
        )
    return result


def _coverage(database: Any) -> dict[str, object]:
    run_row = database.execute(
        """
        WITH ranked AS (
            SELECT
                run.run_id,
                coalesce(
                    try_cast(
                        json_extract_string(
                            run.metrics_json, '$.certifiable'
                        ) AS BOOLEAN
                    ),
                    false
                ) AS certifiable,
                row_number() OVER (
                    PARTITION BY run.period_id
                    ORDER BY
                        run.finished_at DESC NULLS LAST,
                        run.started_at DESC,
                        run.run_id DESC
                ) AS position
            FROM run_log run
            JOIN analytics_selected_periods selected
              ON selected.period_id = run.period_id
            WHERE run.run_kind = 'reconcile'
              AND run.status = 'succeeded'
        )
        SELECT
            count(*),
            count(*) FILTER (WHERE certifiable)
        FROM ranked
        WHERE position = 1
        """
    ).fetchone()
    run_count = int(run_row[0]) if run_row else 0
    certifiable_count = int(run_row[1]) if run_row else 0
    pnl_count = int(
        database.execute(
            """
            WITH latest AS (
                SELECT run_id
                FROM (
                    SELECT
                        run.run_id,
                        run.period_id,
                        row_number() OVER (
                            PARTITION BY run.period_id
                            ORDER BY
                                run.finished_at DESC NULLS LAST,
                                run.started_at DESC,
                                run.run_id DESC
                        ) AS position
                    FROM run_log run
                    JOIN analytics_selected_periods selected
                      ON selected.period_id = run.period_id
                    WHERE run.run_kind = 'reconcile'
                      AND run.status = 'succeeded'
                )
                WHERE position = 1
            )
            SELECT count(*)
            FROM pnl_cell cell
            JOIN latest ON latest.run_id = cell.run_id
            WHERE cell.metric = 'profit'
              AND coalesce(cell.trust_tier, 'certified') = 'certified'
            """
        ).fetchone()[0]
    )

    if run_count == 0:
        status = "no_data"
        message = "当前范围还没有成功完成的核对结果。"
    elif certifiable_count != run_count:
        status = "review_required"
        message = "当前核对尚未达到正式确认条件；金额仅供核对，不能作为正式经营结果。"
    else:
        status = "system_checked"
        message = "当前范围已通过系统检查；正式发布仍需明确的人工批准记录。"

    if pnl_count == 0:
        profit_status = "historical_pending"
    elif status == "system_checked":
        profit_status = "system_checked"
    else:
        profit_status = "review_required"
    return {
        "status": status,
        "message": message,
        "profitStatus": profit_status,
        "periodCount": run_count,
    }


def analytics_overview(
    database: Any,
    *,
    enterprise_id: str | None = None,
    platform_id: str = "all",
    store_id: str = "all",
    period: str = "all",
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 50,
) -> dict[str, object]:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise AnalyticsValidationError("fromDate 不能晚于 toDate")
    if not 1 <= limit <= 100:
        raise AnalyticsValidationError("limit 必须在 1 到 100 之间")

    platforms, stores, periods = _filter_catalog(
        database,
        enterprise_id=enterprise_id,
    )
    valid_platform_ids = {item["id"] for item in platforms}
    valid_store_ids = {item["id"] for item in stores}
    valid_period_ids = {item["value"] for item in periods}
    if platform_id != "all" and platform_id not in valid_platform_ids:
        raise AnalyticsValidationError("platformId 不在当前事实数据范围内")
    if store_id != "all" and store_id not in valid_store_ids:
        raise AnalyticsValidationError("storeId 不在当前事实数据范围内")
    if period != "all" and period not in valid_period_ids:
        raise AnalyticsValidationError("period 不在当前事实数据范围内")

    _create_selected_periods(
        database,
        enterprise_id=enterprise_id,
        platform_id=platform_id,
        store_id=store_id,
        period=period,
        from_date=from_date,
        to_date=to_date,
    )
    _create_selected_facts(database, from_date=from_date, to_date=to_date)

    date_range = {
        "min": min((item["fromDate"] for item in periods), default=None),
        "max": max((item["toDate"] for item in periods), default=None),
    }
    return {
        "filters": {
            "platforms": platforms,
            "stores": stores,
            "periods": [
                {"value": item["value"], "label": item["label"]}
                for item in periods
            ],
            "dateRange": date_range,
        },
        "selection": {
            "platformId": platform_id,
            "storeId": store_id,
            "period": period,
            "fromDate": from_date.isoformat() if from_date else None,
            "toDate": to_date.isoformat() if to_date else None,
        },
        "metrics": _summary(database),
        "trend": _trend(database),
        "storeBreakdown": _store_breakdown(database),
        "transactions": _transactions(database, limit),
        "monthlyPnl": _monthly_pnl(database),
        "coverage": _coverage(database),
    }


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in _MOJIBAKE_MARKERS) + (
        value.count("\ufffd") * 4
    )


def repair_mojibake(value: object) -> str:
    """Conservatively undo one UTF-8-as-latin1 decode when it improves text."""

    text = str(value or "")
    if not text or _mojibake_score(text) == 0:
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if "\ufffd" in repaired:
        return text
    return repaired if _mojibake_score(repaired) < _mojibake_score(text) else text


def _periods_from_path(path: str) -> set[str]:
    values: set[str] = set()
    for match in _PERIOD_RE.finditer(path):
        if match.group(1):
            year, month = match.group(1), match.group(2)
        else:
            year, month = f"20{match.group(3)}", match.group(4)
        values.add(f"{year}-{month}")
    return values


def _valid_store_name(value: str) -> bool:
    normalized = value.strip().strip(". ")
    if not normalized or normalized.casefold() in {
        token.casefold() for token in _NON_STORE_TOKENS
    }:
        return False
    if re.fullmatch(r"(?:19|20)?\d{2,6}", normalized):
        return False
    if any(
        token in normalized.casefold()
        for token in (
            "模板",
            "模版",
            "示例",
            "样例",
            "sample",
            "template",
            "进销存",
            "报表",
            "导出成本",
            "退仓",
        )
    ):
        return False
    return "." not in normalized[-6:]


def _store_from_record(record: Mapping[str, object]) -> str | None:
    path = repair_mojibake(record.get("path"))
    purpose = str(record.get("purpose") or "")
    parts = [part.strip() for part in _PATH_SPLIT_RE.split(path) if part.strip()]
    candidates: list[str] = []

    for marker in ("店铺", "店铺数据", "店铺资料"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                candidates.append(parts[index + 1])

    for marker in (
        ("支付宝收支", "平台账单") if purpose == "settlement" else ()
    ):
        if marker not in parts:
            continue
        index = parts.index(marker) + 1
        if index < len(parts):
            candidates.append(parts[index])

    if purpose == "product_cost":
        for marker in ("聚水潭成本", "商品成本"):
            if marker not in parts:
                continue
            index = parts.index(marker) + 1
            # Cost roots also contain report-type folders. Only the verified
            # root/year/store layout is specific enough to call a segment a store.
            if (
                index + 1 < len(parts)
                and re.fullmatch(r"20\d{2}", parts[index])
            ):
                candidates.append(parts[index + 1])

    if purpose == "historical_workspace" and "Desktop" in parts:
        index = parts.index("Desktop")
        if index + 1 < len(parts):
            candidates.append(parts[index + 1])
    if purpose == "pbix_asset" and parts:
        candidates.append(Path(parts[-1]).stem)

    for candidate in candidates:
        candidate = repair_mojibake(candidate).strip()
        if _valid_store_name(candidate):
            return candidate
    return None


def _normalized_source_path(value: object) -> str:
    text = repair_mojibake(value).strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.replace("\\", "/").casefold()


def analytics_catalog(
    inventory_path: Path,
    *,
    processed_source_uris: Iterable[object] = (),
    known_stores: Mapping[str, str] | None = None,
    known_store_platforms: Mapping[str, str] | None = None,
) -> dict[str, object]:
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        payload = {}
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        records = []
    candidate_ids = {
        str(value)
        for value in (
            payload.get("candidate_source_ids", [])
            if isinstance(payload, dict)
            else []
        )
    }
    processed_paths = {
        _normalized_source_path(value)
        for value in processed_source_uris
        if value is not None
    }
    known_by_name = {
        repair_mojibake(name).strip().casefold(): store_id
        for name, store_id in (known_stores or {}).items()
    }
    platform_by_name = {
        repair_mojibake(name).strip().casefold(): platform_id
        for name, platform_id in (known_store_platforms or {}).items()
    }

    discovered: dict[str, dict[str, object]] = {}
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        record = {str(key): value for key, value in raw_record.items()}
        store_name = _store_from_record(record)
        if store_name is None:
            continue
        key = store_name.casefold()
        store = discovered.setdefault(
            key,
            {
                "name": store_name,
                "periods": set(),
                "fileCount": 0,
                "processed": False,
            },
        )
        file_count = store["fileCount"]
        assert isinstance(file_count, int)
        store["fileCount"] = file_count + 1
        periods = store["periods"]
        assert isinstance(periods, set)
        periods.update(_periods_from_path(repair_mojibake(record.get("path"))))
        source_id = str(record.get("source_id") or "")
        path = _normalized_source_path(record.get("path"))
        if path in processed_paths or (
            source_id in candidate_ids and path in processed_paths
        ):
            store["processed"] = True

    stores: list[dict[str, object]] = []
    for key, item in discovered.items():
        name = str(item["name"])
        item_periods = item["periods"]
        item_file_count = item["fileCount"]
        assert isinstance(item_periods, set)
        assert isinstance(item_file_count, int)
        store_id = known_by_name.get(key)
        if store_id is None:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            store_id = f"discovered_{digest}"
        stores.append(
            {
                "id": store_id,
                "name": name,
                **(
                    {
                        "platformId": platform_by_name[key],
                        "platformName": _PLATFORM_LABELS.get(
                            platform_by_name[key],
                            platform_by_name[key],
                        ),
                    }
                    if key in platform_by_name
                    else {}
                ),
                "periods": sorted(str(value) for value in item_periods),
                "fileCount": item_file_count,
                "processed": bool(item["processed"]),
            }
        )
    stores.sort(key=lambda item: str(item["name"]))

    counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
    all_count = (
        int(counts.get("all", len(records)))
        if isinstance(counts, dict)
        else len(records)
    )
    candidate_count = (
        int(counts.get("candidates", len(candidate_ids)))
        if isinstance(counts, dict)
        else len(candidate_ids)
    )
    return {
        "allRecordCount": all_count,
        "candidateRecordCount": candidate_count,
        "discoveredStoreCount": len(stores),
        "processedStoreCount": sum(1 for item in stores if item["processed"]),
        "platforms": [
            {
                "id": platform_id,
                "name": _PLATFORM_LABELS.get(platform_id, platform_id),
            }
            for platform_id in sorted(set(platform_by_name.values()))
        ],
        "stores": stores,
    }
