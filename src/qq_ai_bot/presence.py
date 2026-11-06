from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class PresenceError(ValueError):
    pass


class PresenceStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_presence (
                    scope_key TEXT PRIMARY KEY,
                    busy INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    quiet_start TEXT NOT NULL DEFAULT '',
                    quiet_end TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS presence_backlog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_presence_backlog_scope
                ON presence_backlog (scope_key, id DESC)
                """
            )

    def set_busy(
        self,
        scope_key: str,
        *,
        busy: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_presence
                    (scope_key, busy, reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    busy = excluded.busy,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (scope_key, 1 if busy else 0, reason.strip()[:300], now),
            )
        return self.status(scope_key)

    def set_quiet_hours(
        self,
        scope_key: str,
        *,
        start: str,
        end: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if bool(start) != bool(end):
            raise PresenceError("勿扰时段必须同时填写开始和结束时间")
        if start and not (
            _valid_time(start) and _valid_time(end)
        ):
            raise PresenceError("勿扰时段格式应为 23:00-07:00")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_presence
                    (
                        scope_key, quiet_start, quiet_end,
                        reason, updated_at
                    )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    quiet_start = excluded.quiet_start,
                    quiet_end = excluded.quiet_end,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    start.strip(),
                    end.strip(),
                    reason.strip()[:300],
                    now,
                ),
            )
        return self.status(scope_key)

    def status(self, scope_key: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bot_presence WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if not row:
            return {
                "scope_key": scope_key,
                "busy": False,
                "reason": "",
                "quiet_start": "",
                "quiet_end": "",
                "in_quiet_hours": False,
            }
        item = dict(row)
        item["busy"] = bool(item["busy"])
        item["in_quiet_hours"] = self.is_quiet(scope_key, "Asia/Shanghai")
        return item

    def is_quiet(self, scope_key: str, timezone_name: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bot_presence WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if not row or bool(row["busy"]):
            return bool(row and row["busy"])
        start = str(row["quiet_start"] or "").strip()
        end = str(row["quiet_end"] or "").strip()
        if not start or not end:
            return False
        now = datetime.now(ZoneInfo(timezone_name)).strftime("%H:%M")
        return start <= now < end if start <= end else (
            now >= start or now < end
        )

    def queue_message(
        self,
        *,
        scope_key: str,
        user_id: int,
        group_id: int | None,
        message_type: str,
        content: str,
    ) -> int | None:
        value = content.strip()
        if not value:
            return None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO presence_backlog
                    (
                        scope_key, user_id, group_id, message_type,
                        content, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_key,
                    user_id,
                    group_id,
                    message_type,
                    value[:4000],
                    _utc_now(),
                ),
            )
            connection.execute(
                """
                DELETE FROM presence_backlog
                WHERE scope_key = ? AND id NOT IN (
                    SELECT id FROM presence_backlog
                    WHERE scope_key = ?
                    ORDER BY id DESC
                    LIMIT 1000
                )
                """,
                (scope_key, scope_key),
            )
            return int(cursor.lastrowid)

    def backlog(
        self,
        *,
        scope_key: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM presence_backlog
                WHERE scope_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope_key, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_backlog(self, scope_key: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM presence_backlog WHERE scope_key = ?",
                (scope_key,),
            )
            return cursor.rowcount

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bot_presence ORDER BY scope_key"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["busy"] = bool(item["busy"])
            item["in_quiet_hours"] = self.is_quiet(
                str(item["scope_key"]),
                "Asia/Shanghai",
            )
            result.append(item)
        return result


def _valid_time(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value.strip()))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
