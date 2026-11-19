from __future__ import annotations

import json
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
            self._ensure_external_message_columns(connection)

    @staticmethod
    def _ensure_external_message_columns(
        connection: sqlite3.Connection,
    ) -> None:
        """Keep databases created by older releases readable."""
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(external_messages)"
            ).fetchall()
        }
        additions = {
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "deleted": "INTEGER NOT NULL DEFAULT 0",
            "attachment_json": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE external_messages ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_messages_message_id
            ON external_messages (message_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_messages_event_time
            ON external_messages (event_time DESC)
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
        message_id: int | str | None = None,
        attachments: list[dict[str, Any]] | None = None,
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
                        sender_user_id, sender_name, role, content, event_time,
                        message_id, attachment_json
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(message_id or ""),
                    json.dumps(attachments or [], ensure_ascii=False)[:100000],
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
                           content, event_time, archived_at, message_id,
                           deleted, attachment_json
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
                           content, event_time, archived_at, message_id,
                           deleted, attachment_json
                    FROM external_messages
                    ORDER BY event_time DESC, archived_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [self._archive_row(row) for row in rows]

    @staticmethod
    def _archive_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        raw_attachments = item.pop("attachment_json", "") or "[]"
        try:
            parsed = json.loads(raw_attachments)
        except (TypeError, ValueError):
            parsed = []
        item["attachments"] = parsed if isinstance(parsed, list) else []
        item["deleted"] = bool(item.get("deleted"))
        return item

    def search_messages(
        self,
        *,
        query: str = "",
        message_type: str = "",
        target_id: int | None = None,
        role: str = "",
        since: int | None = None,
        until: int | None = None,
        file_only: bool = False,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search archived QQ messages across conversations.

        This intentionally uses a portable LIKE query. It works with existing
        SQLite files and is fast enough for the bounded local archive used by
        the bot; no FTS extension is required.
        """
        safe_limit = max(1, min(500, int(limit)))
        clauses: list[str] = []
        values: list[Any] = []
        text = str(query or "").strip()
        if text:
            clauses.append("(content LIKE ? OR sender_name LIKE ?)")
            pattern = f"%{text}%"
            values.extend((pattern, pattern))
        if message_type in {"private", "group"}:
            clauses.append("message_type = ?")
            values.append(message_type)
        if target_id is not None and int(target_id) > 0:
            if message_type == "group":
                clauses.append("group_id = ?")
                values.append(int(target_id))
            elif message_type == "private":
                clauses.append("conversation_id = ?")
                values.append(f"private:{int(target_id)}")
            else:
                clauses.append(
                    "(conversation_id = ? OR group_id = ?)"
                )
                values.extend((f"private:{int(target_id)}", int(target_id)))
        if role in {"user", "assistant"}:
            clauses.append("role = ?")
            values.append(role)
        if since is not None:
            clauses.append("event_time >= ?")
            values.append(int(since))
        if until is not None:
            clauses.append("event_time <= ?")
            values.append(int(until))
        if file_only:
            clauses.append("attachment_json NOT IN ('', '[]')")
        if not include_deleted:
            clauses.append("COALESCE(deleted, 0) = 0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT external_key, conversation_id, message_type,
                       group_id, sender_user_id, sender_name, role,
                       content, event_time, archived_at, message_id,
                       deleted, attachment_json
                FROM external_messages
                """
                + where
                + """
                ORDER BY event_time DESC, archived_at DESC
                LIMIT ?
                """,
                (*values, safe_limit),
            ).fetchall()
        items = [self._archive_row(row) for row in rows]
        for item in items:
            item["snippet"] = str(item.get("content") or "")[:280]
            item["target_type"] = item.get("message_type")
            if item.get("message_type") == "group":
                item["target_id"] = item.get("group_id")
            else:
                conversation_id = str(item.get("conversation_id") or "")
                item["target_id"] = (
                    int(conversation_id.removeprefix("private:"))
                    if conversation_id.removeprefix("private:").isdigit()
                    else item.get("sender_user_id")
                )
        return items

    def mark_message_deleted(self, message_id: int | str) -> int:
        """Hide a QQ message from search after a successful recall."""
        identity = str(message_id or "").strip()
        if not identity:
            return 0
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE external_messages
                SET deleted = 1
                WHERE message_id = ? OR external_key = ?
                """,
                (identity, identity),
            )
        return int(cursor.rowcount)

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

