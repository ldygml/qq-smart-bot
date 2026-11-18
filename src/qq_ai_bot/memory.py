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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_messages (
                    external_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    group_id INTEGER,
                    sender_user_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    event_time INTEGER NOT NULL DEFAULT 0,
                    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_external_messages_conversation
                ON external_messages (conversation_id, event_time)
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

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        max_rows: int,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (?, ?, ?)
                """,
                (conversation_id, role, content),
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
                (conversation_id, conversation_id, max(1, max_rows)),
            )

    def archive_message(
        self,
        *,
        external_key: str,
        conversation_id: str,
        message_type: str,
        group_id: int | None,
        sender_user_id: int,
        sender_name: str,
        role: str,
        content: str,
        event_time: int = 0,
        add_to_chat: bool = False,
        max_rows: int = 160,
    ) -> bool:
        """Archive one QQ message exactly once.

        History imports can also materialize the message in the short-term
        chat table. Real-time webhook events only archive their external id so
        the normal event pipeline remains responsible for reply handling.
        """
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO external_messages
                    (
                        external_key, conversation_id, message_type, group_id,
                        sender_user_id, sender_name, role, content, event_time
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    external_key,
                    conversation_id,
                    message_type,
                    group_id,
                    sender_user_id,
                    sender_name[:120],
                    role,
                    content,
                    int(event_time or 0),
                ),
            )
            if cursor.rowcount <= 0:
                return False
            if add_to_chat:
                connection.execute(
                    """
                    INSERT INTO messages (conversation_id, role, content)
                    VALUES (?, ?, ?)
                    """,
                    (conversation_id, role, content),
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
                    (
                        conversation_id,
                        conversation_id,
                        max(1, max_rows),
                    ),
                )
            return True

    def external_message_exists(self, external_key: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM external_messages
                WHERE external_key = ?
                LIMIT 1
                """,
                (external_key,),
            ).fetchone()
        return row is not None

    def archived_messages(
        self,
        *,
        conversation_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = min(1000, max(1, limit))
        with self._lock, self._connect() as connection:
            if conversation_id:
                rows = connection.execute(
                    """
                    SELECT external_key, conversation_id, message_type,
                           group_id, sender_user_id, sender_name, role,
                           content, event_time, archived_at
                    FROM external_messages
                    WHERE conversation_id = ?
                    ORDER BY event_time DESC, archived_at DESC
                    LIMIT ?
                    """,
                    (conversation_id, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT external_key, conversation_id, message_type,
                           group_id, sender_user_id, sender_name, role,
                           content, event_time, archived_at
                    FROM external_messages
                    ORDER BY event_time DESC, archived_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def reset(self, conversation_id: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            return cursor.rowcount

    def delete_for_user(self, user_id: int) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM messages
                WHERE conversation_id = ?
                   OR conversation_id LIKE ?
                """,
                (
                    f"private:{user_id}",
                    f"group:%:user:{user_id}",
                ),
            )
            connection.execute(
                """
                DELETE FROM external_messages
                WHERE conversation_id = ?
                   OR conversation_id LIKE ?
                """,
                (
                    f"private:{user_id}",
                    f"group:%:user:{user_id}",
                ),
            )
            return cursor.rowcount

    def delete_all(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM external_messages")
            return cursor.rowcount

    def count(self, conversation_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def last_message_at(self, conversation_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return str(row["created_at"]) if row else None

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM messages"
            ).fetchone()
            archive_row = connection.execute(
                "SELECT COUNT(*) AS total FROM external_messages"
            ).fetchone()
        return {
            "stored_messages": int(row["total"]) if row is not None else 0,
            "archived_messages": (
                int(archive_row["total"]) if archive_row is not None else 0
            ),
        }

