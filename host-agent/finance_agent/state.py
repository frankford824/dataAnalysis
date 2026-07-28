from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .models import utc_now_iso


class AgentState:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.path = self.state_dir / "agent-state.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                endpoint TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS completed_jobs (
                idempotency_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                result TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.connection.close()

    def set_secret(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_secret(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def queue_event(
        self, event_key: str, endpoint: str, payload: dict[str, Any]
    ) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO events(event_key, endpoint, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                event_key,
                endpoint,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, event_key, endpoint, payload FROM events "
            "WHERE sent_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "event_key": row["event_key"],
                "endpoint": row["endpoint"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def mark_event_sent(self, event_id: int) -> None:
        self.connection.execute(
            "UPDATE events SET sent_at = ? WHERE id = ?", (utc_now_iso(), event_id)
        )
        self.connection.commit()

    def completed_result(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result FROM completed_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def mark_completed(
        self, idempotency_key: str, job_id: str, result: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO completed_jobs"
            "(idempotency_key, job_id, result, completed_at) VALUES (?, ?, ?, ?)",
            (
                idempotency_key,
                job_id,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
            ),
        )
        self.connection.commit()
