from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Note:
    id: int
    title: str
    content: str
    updated_at: str


@dataclass(frozen=True)
class Todo:
    id: int
    content: str
    done: bool
    created_at: str


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    message_type: str
    group_id: int | None
    content: str
    due_at: str
    delivered: bool


class ReminderTimeError(ValueError):
    pass


class ProductivityStore:
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notes_user
                ON notes (user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_todos_user
                ON todos (user_id, done, id DESC);

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    group_id INTEGER,
                    content TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders (delivered, due_at, id);
                """
            )

    def add_note(self, user_id: int, title: str, content: str) -> int:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notes
                    (user_id, title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, title.strip(), content.strip(), now, now),
            )
            return int(cursor.lastrowid)

    def list_notes(self, user_id: int, limit: int = 10) -> list[Note]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, content, updated_at
                FROM notes
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, max(1, limit)),
            ).fetchall()
        return [_note_from_row(row) for row in rows]

    def search_notes(self, user_id: int, query: str, limit: int = 5) -> list[Note]:
        pattern = f"%{query.strip()}%"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, content, updated_at
                FROM notes
                WHERE user_id = ?
                  AND (title LIKE ? OR content LIKE ?)
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, pattern, pattern, max(1, limit)),
            ).fetchall()
        return [_note_from_row(row) for row in rows]

    def delete_note(self, user_id: int, note_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM notes WHERE id = ? AND user_id = ?",
                (note_id, user_id),
            )
            return cursor.rowcount > 0

    def add_todo(self, user_id: int, content: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO todos (user_id, content, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, content.strip(), _utc_now()),
            )
            return int(cursor.lastrowid)

    def list_todos(self, user_id: int) -> list[Todo]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, done, created_at
                FROM todos
                WHERE user_id = ?
                ORDER BY done ASC, id DESC
                LIMIT 30
                """,
                (user_id,),
            ).fetchall()
        return [
            Todo(
                id=int(row["id"]),
                content=str(row["content"]),
                done=bool(row["done"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def complete_todo(self, user_id: int, todo_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE todos
                SET done = 1, completed_at = ?
                WHERE id = ? AND user_id = ? AND done = 0
                """,
                (_utc_now(), todo_id, user_id),
            )
            return cursor.rowcount > 0

    def add_reminder(
        self,
        *,
        user_id: int,
        message_type: str,
        group_id: int | None,
        content: str,
        due_at: datetime,
    ) -> int:
        due_utc = due_at.astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reminders
                    (user_id, message_type, group_id, content, due_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    message_type,
                    group_id,
                    content.strip(),
                    due_utc,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_reminders(self, user_id: int, limit: int = 10) -> list[Reminder]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, message_type, group_id, content, due_at, delivered
                FROM reminders
                WHERE user_id = ? AND delivered = 0
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                (user_id, max(1, limit)),
            ).fetchall()
        return [_reminder_from_row(row) for row in rows]

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, message_type, group_id, content, due_at, delivered
                FROM reminders
                WHERE delivered = 0 AND due_at <= ?
                ORDER BY due_at ASC, id ASC
                LIMIT 20
                """,
                (current.isoformat(),),
            ).fetchall()
        return [_reminder_from_row(row) for row in rows]

    def mark_reminder_delivered(self, reminder_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE reminders SET delivered = 1 WHERE id = ?",
                (reminder_id,),
            )

    def cancel_reminder(self, user_id: int, reminder_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM reminders
                WHERE id = ? AND user_id = ? AND delivered = 0
                """,
                (reminder_id, user_id),
            )
            return cursor.rowcount > 0


def parse_reminder_time(raw: str, timezone_name: str) -> tuple[datetime, str]:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    value = raw.strip()
    parsed: datetime | None = None
    formats = (
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m-%d %H:%M",
        "%m/%d %H:%M",
        "%H:%M",
    )
    for fmt in formats:
        try:
            candidate = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt in {"%H:%M"}:
            candidate = candidate.replace(
                year=now.year,
                month=now.month,
                day=now.day,
            )
            if candidate <= now:
                candidate += _ONE_DAY
        elif fmt in {"%m-%d %H:%M", "%m/%d %H:%M"}:
            candidate = candidate.replace(year=now.year)
        parsed = candidate.replace(tzinfo=zone)
        break

    if parsed is None:
        raise ReminderTimeError(
            "时间格式不对。请使用：2026-10-28 09:00、10-28 09:00 或 09:00。"
        )
    if parsed <= now:
        raise ReminderTimeError("提醒时间必须晚于现在。")
    return parsed.astimezone(timezone.utc), parsed.strftime("%Y-%m-%d %H:%M")


def format_utc_for_zone(value: str, timezone_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")


def split_reminder_argument(argument: str) -> tuple[str, str]:
    match = re.match(
        r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}"
        r"|\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}"
        r"|\d{1,2}:\d{2})\s+(.+)$",
        argument.strip(),
    )
    if not match:
        raise ReminderTimeError(
            "请这样设置：/提醒 2026-10-28 09:00 交作业"
        )
    return match.group(1), match.group(2).strip()


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=int(row["id"]),
        title=str(row["title"]),
        content=str(row["content"]),
        updated_at=str(row["updated_at"]),
    )


def _reminder_from_row(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        message_type=str(row["message_type"]),
        group_id=int(row["group_id"]) if row["group_id"] is not None else None,
        content=str(row["content"]),
        due_at=str(row["due_at"]),
        delivered=bool(row["delivered"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
