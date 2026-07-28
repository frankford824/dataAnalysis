"""Phase 3 period-close service: persist kernel period state machine to DuckDB.

DuckDB implements UPDATE as delete+insert and therefore refuses status-only
updates to an ``accounting_period`` row that any child table references via
foreign key.  Period close state is consequently kept in the companion
``accounting_period_state`` table and readers resolve the effective status
with ``coalesce(state.status, period.status)`` -- the same pattern the
Harness already uses for ``input_revision`` / ``input_revision_state``.

Use :func:`effective_period_status` (or :func:`period_is_locked`) rather than
reading ``accounting_period.status`` directly.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .kernel.period import InvalidPeriodTransition, PeriodState

if TYPE_CHECKING:
    from .memory.database import DuckDBMemory

LOCKED_STATUSES = frozenset(
    {
        PeriodState.PRE_CLOSED.value,
        PeriodState.FINALIZED.value,
        PeriodState.RESTATED.value,
    }
)

EFFECTIVE_STATUS_SQL = """
    SELECT coalesce(state.status, period.status)
    FROM accounting_period period
    LEFT JOIN accounting_period_state state
      ON state.period_id = period.period_id
    WHERE period.period_id = ?
"""


def effective_period_status(database: DuckDBMemory, period_id: str) -> str:
    """Return the authoritative period status.

    Raises ``LookupError`` when the period does not exist.
    """
    row = database.fetchone_required(EFFECTIVE_STATUS_SQL, [period_id])
    return str(row[0])


def period_is_locked(database: DuckDBMemory, period_id: str) -> bool:
    """True when the period no longer accepts ordinary revisions."""
    row = database.execute(EFFECTIVE_STATUS_SQL, [period_id]).fetchone()
    if not row:
        return False
    return str(row[0]) in LOCKED_STATUSES


def _write_state(
    database: DuckDBMemory,
    period_id: str,
    *,
    status: str,
    closed_by: str,
    closed_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    database.execute(
        """
        INSERT INTO accounting_period_state (
            period_id, status, closed_at, closed_by, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (period_id) DO UPDATE SET
            status = excluded.status,
            closed_at = coalesce(excluded.closed_at, accounting_period_state.closed_at),
            closed_by = excluded.closed_by,
            updated_at = excluded.updated_at
        """,
        [period_id, status, closed_at, closed_by, now],
    )


def _record_maintenance(
    database: DuckDBMemory,
    *,
    action: str,
    details: dict[str, Any],
) -> None:
    database.execute(
        """
        INSERT INTO maintenance_log (
            action_id, action, affected_runs, affected_rows, details_json
        ) VALUES (?, ?, 0, 1, ?)
        """,
        [
            f"period_{uuid.uuid4().hex}",
            action,
            json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
        ],
    )


def preclose_period(
    database: DuckDBMemory,
    period_id: str,
    operator_name: str,
) -> dict[str, Any]:
    """Transition an open period to preclosed (kernel: open → preclosed)."""
    operator = (operator_name or "").strip()
    if not operator:
        raise ValueError("操作人 (operator_name) 不能为空")

    current = effective_period_status(database, period_id)
    if PeriodState(current) != PeriodState.OPEN:
        raise InvalidPeriodTransition(
            f"只有处于 open 状态的账期才能预关闭，当前状态为 {current}"
        )

    now = datetime.now(UTC)
    new_status = PeriodState.PRE_CLOSED.value
    _write_state(database, period_id, status=new_status, closed_by=operator)
    _record_maintenance(
        database,
        action="period_preclose",
        details={
            "period_id": period_id,
            "operator": operator,
            "previous_status": current,
            "new_status": new_status,
            "at": now.isoformat(),
        },
    )
    return {"period_id": period_id, "status": new_status}


def finalize_period(
    database: DuckDBMemory,
    period_id: str,
    operator_name: str,
) -> dict[str, Any]:
    """Transition a preclosed period to closed (kernel: preclosed → closed)."""
    operator = (operator_name or "").strip()
    if not operator:
        raise ValueError("操作人 (operator_name) 不能为空")

    current = effective_period_status(database, period_id)
    if PeriodState(current) != PeriodState.PRE_CLOSED:
        raise InvalidPeriodTransition(
            f"只有处于 preclosed 状态的账期才能最终关闭，当前状态为 {current}"
        )

    now = datetime.now(UTC)
    new_status = PeriodState.FINALIZED.value
    _write_state(
        database,
        period_id,
        status=new_status,
        closed_by=operator,
        closed_at=now,
    )
    _record_maintenance(
        database,
        action="period_finalize",
        details={
            "period_id": period_id,
            "operator": operator,
            "previous_status": current,
            "new_status": new_status,
            "at": now.isoformat(),
        },
    )
    return {
        "period_id": period_id,
        "status": new_status,
        "closed_at": now.isoformat(),
    }


def post_adjustment(
    database: DuckDBMemory,
    period_id: str,
    operator_name: str,
    *,
    amount: Decimal,
    reason: str,
    unresolved_id: str | None = None,
) -> dict[str, Any]:
    """Post an adjustment entry against a finalized period.

    Only ``closed`` or ``restated`` periods accept adjustments, matching
    ``AccountingPeriod.post_adjustment`` which requires a locked period.
    """
    operator = (operator_name or "").strip()
    if not operator:
        raise ValueError("操作人 (operator_name) 不能为空")
    reason_text = (reason or "").strip()
    if not reason_text:
        raise ValueError("调整原因 (reason) 不能为空")
    if amount == Decimal("0"):
        raise ValueError("调整金额不能为零")

    current = effective_period_status(database, period_id)
    if PeriodState(current) not in {PeriodState.FINALIZED, PeriodState.RESTATED}:
        raise InvalidPeriodTransition(
            f"只有已关闭的账期才能提交调整分录，当前状态为 {current}"
        )

    adjustment_id = f"adj_{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    new_status = PeriodState.RESTATED.value

    database.execute(
        """
        INSERT INTO adjustment_entry (
            adjustment_id, period_id, original_period_id,
            unresolved_id, amount, currency, reason,
            status, approved_by, approved_at, created_at
        ) VALUES (?, ?, ?, ?, ?, 'CNY', ?, 'posted', ?, ?, ?)
        """,
        [
            adjustment_id,
            period_id,
            period_id,
            unresolved_id,
            amount,
            reason_text,
            operator,
            now,
            now,
        ],
    )
    _write_state(database, period_id, status=new_status, closed_by=operator)
    _record_maintenance(
        database,
        action="period_adjustment",
        details={
            "period_id": period_id,
            "adjustment_id": adjustment_id,
            "operator": operator,
            "amount": str(amount),
            "reason": reason_text,
            "unresolved_id": unresolved_id,
            "previous_status": current,
            "new_status": new_status,
            "at": now.isoformat(),
        },
    )

    return {
        "adjustment_id": adjustment_id,
        "period_id": period_id,
        "status": new_status,
        "amount": str(amount),
    }


def get_period_detail(
    database: DuckDBMemory,
    period_id: str,
) -> dict[str, Any]:
    """Return period status and net posted adjustments."""
    row = database.fetchone_required(
        """
        SELECT period.period_id, period.contract_id, period.store_id,
               period.period_start, period.period_end,
               coalesce(state.status, period.status),
               period.revision_no,
               coalesce(state.closed_at, period.closed_at),
               coalesce(state.closed_by, period.closed_by)
        FROM accounting_period period
        LEFT JOIN accounting_period_state state
          ON state.period_id = period.period_id
        WHERE period.period_id = ?
        """,
        [period_id],
    )
    adj_row = database.execute(
        """
        SELECT coalesce(sum(amount), 0)
        FROM adjustment_entry
        WHERE period_id = ?
          AND status IN ('posted', 'approved')
        """,
        [period_id],
    ).fetchone()
    net_adjustments = Decimal(str(adj_row[0])) if adj_row else Decimal("0")

    return {
        "period_id": str(row[0]),
        "contract_id": str(row[1]),
        "store_id": str(row[2]),
        "period_start": str(row[3]),
        "period_end": str(row[4]),
        "status": str(row[5]),
        "revision_no": int(row[6]),
        "closed_at": str(row[7]) if row[7] else None,
        "closed_by": str(row[8]) if row[8] else None,
        "net_adjustments": str(net_adjustments),
    }
