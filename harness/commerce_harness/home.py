"""One-screen home: files / needs decision / conclusion / recoverable money."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter

from commerce_harness.memory.database import DuckDBMemory
from commerce_harness.run_scope import RunScope, latest_scope
from commerce_harness.workbench import WorkbenchPaths


def _money(value: Any) -> str:
    return format(Decimal(str(value or 0)), "f")


def build_home_router(workbench: WorkbenchPaths) -> APIRouter:
    router = APIRouter(prefix="/api/v1/home", tags=["home"])

    @router.get("")
    def home() -> dict[str, Any]:
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            # Every bar reports one period computed by one run; mixing runs or
            # periods would double count the money shown to the operator.
            scope = latest_scope(database)
            if scope is None:
                return _empty_home()

            missing_count, total_req = _checklist_counts(database, scope)
            open_count, open_amount = _open_decisions(database, scope)
            profit, partial_cells, blocked_cells = _store_profit(database, scope)
            claim_count, claim_amount = _open_claims(database, scope)

            notifications = _notifications(
                missing_count=missing_count,
                open_count=open_count,
                open_amount=open_amount,
                claim_count=claim_count,
                claim_amount=claim_amount,
                blocked_cells=blocked_cells,
            )

        if blocked_cells > 0:
            conclusion = "这个月我不敢出数，还有关键检查没过。"
            conclusion_tone = "blocked"
        elif partial_cells > 0:
            conclusion = (
                f"本月利润约 {_money(profit)} 元。"
                "其中有一部分我只敢认一部分，详情见结论页。"
            )
            conclusion_tone = "partial"
        else:
            conclusion = f"本月利润 {_money(profit)} 元。"
            conclusion_tone = "ok"

        return {
            "bars": [
                {
                    "id": "files",
                    "title": "文件",
                    "active": missing_count > 0,
                    "summary": (
                        f"本月应到 {total_req or '若干'} 份，"
                        f"还缺 {missing_count} 份。"
                        if missing_count
                        else "本月应到文件已齐。"
                    ),
                    "action": "去看缺哪些" if missing_count else None,
                },
                {
                    "id": "decide",
                    "title": "需要你定",
                    "active": open_count > 0 and missing_count == 0,
                    "summary": (
                        f"{open_count} 件事需要你定，涉及 {_money(open_amount)} 元。"
                        if open_count
                        else "目前没有需要你定的事。"
                    ),
                    "action": "开始处理" if open_count else None,
                },
                {
                    "id": "conclusion",
                    "title": "结论",
                    "active": False,
                    "summary": conclusion,
                    "tone": conclusion_tone,
                    "action": "查看明细",
                },
                {
                    "id": "recoverable",
                    "title": "能要回的钱",
                    "active": claim_count > 0,
                    "summary": (
                        f"发现可能少给 {_money(claim_amount)} 元，"
                        f"共 {claim_count} 笔，证据已备好。"
                        if claim_count
                        else "暂时没有发现能要回的钱。"
                    ),
                    "action": "下载证据包" if claim_count else None,
                },
            ],
            "notifications": notifications,
            "scope": {
                "periodId": scope.period_id,
                "runId": scope.run_id,
                "storeId": scope.store_id,
                "periodStart": scope.period_start,
                "periodEnd": scope.period_end,
            },
        }

    return router


def _empty_home() -> dict[str, Any]:
    """No successful run yet: say so instead of reporting a zero as a fact."""
    return {
        "bars": [
            {
                "id": "files",
                "title": "文件",
                "active": False,
                "summary": "还没开始收这个月的文件。",
                "action": None,
            },
            {
                "id": "decide",
                "title": "需要你定",
                "active": False,
                "summary": "目前没有需要你定的事。",
                "action": None,
            },
            {
                "id": "conclusion",
                "title": "结论",
                "active": False,
                "summary": "这个月还没算过，我先不给数。",
                "tone": "pending",
                "action": None,
            },
            {
                "id": "recoverable",
                "title": "能要回的钱",
                "active": False,
                "summary": "还没查过有没有能要回的钱。",
                "action": None,
            },
        ],
        "notifications": [],
        "scope": None,
    }


def _checklist_counts(
    database: DuckDBMemory, scope: RunScope
) -> tuple[int, int]:
    row = database.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'missing'), count(*)
        FROM checklist_result
        WHERE run_id = ? AND period_id = ?
        """,
        [scope.run_id, scope.period_id],
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def _open_decisions(
    database: DuckDBMemory, scope: RunScope
) -> tuple[int, Any]:
    row = database.execute(
        """
        SELECT count(*), coalesce(sum(abs(unresolved.amount)), 0)
        FROM unresolved_balance AS unresolved
        JOIN reconciliation_balance AS balance
             ON balance.balance_id = unresolved.balance_id
        WHERE unresolved.status = 'open'
          AND balance.run_id = ?
          AND balance.period_id = ?
        """,
        [scope.run_id, scope.period_id],
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row[0] or 0), row[1]


def _store_profit(
    database: DuckDBMemory, scope: RunScope
) -> tuple[Any, int, int]:
    row = database.execute(
        """
        SELECT coalesce(sum(value), 0),
               count(*) FILTER (WHERE trust_tier = 'partial'),
               count(*) FILTER (WHERE trust_tier = 'blocked')
        FROM pnl_cell
        WHERE run_id = ?
          AND period_id = ?
          AND metric = 'profit'
          AND sku_key = '__store_total__'
        """,
        [scope.run_id, scope.period_id],
    ).fetchone()
    if row is None:
        return 0, 0, 0
    return row[0], int(row[1] or 0), int(row[2] or 0)


def _open_claims(database: DuckDBMemory, scope: RunScope) -> tuple[int, Any]:
    if not _table_exists(database, "claim"):
        return 0, 0
    row = database.execute(
        """
        SELECT count(*), coalesce(sum(abs(claimed_amount)), 0)
        FROM claim
        WHERE status IN ('draft', 'packaged', 'submitted')
          AND period_id = ?
        """,
        [scope.period_id],
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row[0] or 0), row[1]


def _table_exists(database: DuckDBMemory, name: str) -> bool:
    row = database.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_name = ?
        LIMIT 1
        """,
        [name],
    ).fetchone()
    return row is not None


def _notifications(
    *,
    missing_count: int,
    open_count: int,
    open_amount: Any,
    claim_count: int,
    claim_amount: Any,
    blocked_cells: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if missing_count:
        items.append(
            {
                "id": "files-missing",
                "title": f"还缺 {missing_count} 份文件",
                "body": "文件不齐时我不会猜数。",
            }
        )
    if open_count:
        items.append(
            {
                "id": "decide-open",
                "title": f"{open_count} 件事等你定",
                "body": f"涉及 {_money(open_amount)} 元。",
            }
        )
    if claim_count:
        items.append(
            {
                "id": "claims-ready",
                "title": f"可能少给了 {_money(claim_amount)} 元",
                "body": "证据包已备好，可以拿去找平台。",
            }
        )
    if blocked_cells:
        items.append(
            {
                "id": "blocked",
                "title": "这个月我不敢出数",
                "body": "还有关键检查没过。",
            }
        )
    return items
