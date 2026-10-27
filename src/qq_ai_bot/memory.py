from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class ChatMemory:
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
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages (conversation_id, id)
                """
            )

    def history(self, conversation_id: str, limit: int) -> list[dict[str, str]]:
        limit = max(0, limit)
        if limit == 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]

    def append_exchange(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        *,
        max_rows: int,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (?, ?, ?)
                """,
                (
                    (conversation_id, "user", user_text),
                    (conversation_id, "assistant", assistant_text),
                ),
            )
            connection.execute(
                """
                DELETE FROM messages
                WHERE conversation_id = ?
                  AND id NOT IN (
                    SELECT id
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (conversation_id, conversation_id, max(2, max_rows)),
            )

    def reset(self, conversation_id: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            return cursor.rowcount

    def count(self, conversation_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM messages"
            ).fetchone()
        return {"stored_messages": int(row["total"]) if row is not None else 0}

