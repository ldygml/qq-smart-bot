from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class GroupBehaviorStore:
    """Tracks how often a bot speaks in each group and learns a silence bias."""

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
                CREATE TABLE IF NOT EXISTS group_behavior (
                    group_id INTEGER PRIMARY KEY,
                    messages_seen INTEGER NOT NULL DEFAULT 0,
                    replies_sent INTEGER NOT NULL DEFAULT 0,
                    ordinary_candidates INTEGER NOT NULL DEFAULT 0,
                    ordinary_ignored INTEGER NOT NULL DEFAULT 0,
                    consecutive_ignored INTEGER NOT NULL DEFAULT 0,
                    silence_bias REAL NOT NULL DEFAULT 0.0,
                    last_message_at TEXT,
                    last_reply_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def observe_message(self, group_id: int) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_behavior
                    (group_id, messages_seen, last_message_at, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    messages_seen = messages_seen + 1,
                    last_message_at = excluded.last_message_at,
                    updated_at = excluded.updated_at
                """,
                (group_id, now, now),
            )

    def should_attempt_ordinary(
        self,
        group_id: int,
        *,
        cooldown_seconds: float,
    ) -> bool:
        policy = self.policy(group_id)
        if float(policy["silence_bias"]) >= 0.9:
            return False
        last_reply_at = policy.get("last_reply_at")
        if last_reply_at and cooldown_seconds > 0:
            try:
                elapsed = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(str(last_reply_at))
                ).total_seconds()
            except ValueError:
                elapsed = cooldown_seconds
            if elapsed < cooldown_seconds:
                return False
        return True

    def record_ordinary_ignored(self, group_id: int) -> None:
        self._record_candidate(group_id, replied=False)

    def record_reply(self, group_id: int) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_behavior
                    (
                        group_id, messages_seen, replies_sent,
                        ordinary_candidates, ordinary_ignored,
                        consecutive_ignored, last_reply_at, updated_at
                    )
                VALUES (?, 0, 1, 1, 0, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    replies_sent = replies_sent + 1,
                    ordinary_candidates = ordinary_candidates + 1,
                    consecutive_ignored = 0,
                    last_reply_at = excluded.last_reply_at,
                    updated_at = excluded.updated_at
                """,
                (group_id, now, now),
            )

    def policy(self, group_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM group_behavior WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return {
                "group_id": group_id,
                "messages_seen": 0,
                "replies_sent": 0,
                "ordinary_candidates": 0,
                "ordinary_ignored": 0,
                "consecutive_ignored": 0,
                "silence_bias": 0.0,
                "last_message_at": None,
                "last_reply_at": None,
                "reply_rate": 0.0,
            }
        item = dict(row)
        candidates = int(item.get("ordinary_candidates") or 0)
        replies = int(item.get("replies_sent") or 0)
        item["reply_rate"] = replies / candidates if candidates else 0.0
        return item

    def list_policies(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_behavior ORDER BY updated_at DESC"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            candidates = int(item.get("ordinary_candidates") or 0)
            replies = int(item.get("replies_sent") or 0)
            item["reply_rate"] = replies / candidates if candidates else 0.0
            result.append(item)
        return result

    def reset(self, group_id: int | None = None) -> int:
        with self._lock, self._connect() as connection:
            if group_id is None:
                cursor = connection.execute("DELETE FROM group_behavior")
            else:
                cursor = connection.execute(
                    "DELETE FROM group_behavior WHERE group_id = ?",
                    (group_id,),
                )
            return cursor.rowcount

    def _record_candidate(self, group_id: int, *, replied: bool) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT ordinary_candidates, ordinary_ignored,
                       consecutive_ignored
                FROM group_behavior WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            candidates = int(row["ordinary_candidates"]) if row else 0
            ignored = int(row["ordinary_ignored"]) if row else 0
            consecutive = int(row["consecutive_ignored"]) if row else 0
            candidates += 1
            if not replied:
                ignored += 1
                consecutive += 1
            rate = ignored / candidates if candidates else 0.0
            # A long run of healthy chit-chat should lower the bot's urge to join.
            silence_bias = min(1.0, max(0.0, (rate - 0.55) / 0.4))
            connection.execute(
                """
                INSERT INTO group_behavior
                    (
                        group_id, ordinary_candidates, ordinary_ignored,
                        consecutive_ignored, silence_bias, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    ordinary_candidates = excluded.ordinary_candidates,
                    ordinary_ignored = excluded.ordinary_ignored,
                    consecutive_ignored = excluded.consecutive_ignored,
                    silence_bias = excluded.silence_bias,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    candidates,
                    ignored,
                    consecutive,
                    silence_bias,
                    now,
                ),
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
