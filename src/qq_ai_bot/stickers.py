from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .onebot import StickerSource


class StickerStore:
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
                CREATE TABLE IF NOT EXISTS sticker_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    emoji_id TEXT NOT NULL,
                    emoji_package_id TEXT NOT NULL,
                    sticker_key TEXT NOT NULL,
                    image_source TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '未分类',
                    usage_hint TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    seen_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(summary, emoji_id, emoji_package_id, sticker_key)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(sticker_catalog)"
                ).fetchall()
            }
            for column, definition in (
                ("image_source", "TEXT NOT NULL DEFAULT ''"),
                ("category", "TEXT NOT NULL DEFAULT '未分类'"),
                ("usage_hint", "TEXT NOT NULL DEFAULT ''"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE sticker_catalog "
                        f"ADD COLUMN {column} {definition}"
                    )

    def record(self, sticker: StickerSource) -> None:
        if not (
            sticker.emoji_id
            or sticker.emoji_package_id
            or sticker.key
        ):
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sticker_catalog
                    (
                        summary, emoji_id, emoji_package_id,
                        sticker_key, image_source, seen_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(summary, emoji_id, emoji_package_id, sticker_key)
                DO UPDATE SET seen_at = excluded.seen_at,
                    image_source = CASE
                        WHEN excluded.image_source != ''
                        THEN excluded.image_source
                        ELSE sticker_catalog.image_source
                    END
                """,
                (
                    sticker.summary,
                    sticker.emoji_id,
                    sticker.emoji_package_id,
                    sticker.key,
                    sticker.image_source,
                    _utc_now(),
                ),
            )

    def random_sticker(self, keyword: str = "") -> StickerSource | None:
        with self._lock, self._connect() as connection:
            if keyword:
                rows = connection.execute(
                    """
                    SELECT summary, emoji_id, emoji_package_id, sticker_key
                    FROM sticker_catalog
                    WHERE enabled = 1
                      AND (summary LIKE ? OR usage_hint LIKE ?
                           OR category LIKE ?)
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (
                        f"%{keyword}%",
                        f"%{keyword}%",
                        f"%{keyword}%",
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT summary, emoji_id, emoji_package_id, sticker_key
                    FROM sticker_catalog
                    WHERE enabled = 1
                    ORDER BY RANDOM()
                    LIMIT 1
                    """
                ).fetchall()
        if not rows:
            return None
        row = rows[0]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE sticker_catalog
                SET use_count = use_count + 1
                WHERE summary = ? AND emoji_id = ?
                  AND emoji_package_id = ? AND sticker_key = ?
                """,
                (
                    row["summary"],
                    row["emoji_id"],
                    row["emoji_package_id"],
                    row["sticker_key"],
                ),
            )
        return StickerSource(
            summary=str(row["summary"]),
            emoji_id=str(row["emoji_id"]),
            emoji_package_id=str(row["emoji_package_id"]),
            key=str(row["sticker_key"]),
        )

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM sticker_catalog WHERE enabled = 1"
            ).fetchone()
        return int(row["total"]) if row else 0

    def list_recent(
        self,
        limit: int = 50,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        where = "" if include_disabled else "WHERE enabled = 1"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, category, usage_hint,
                       enabled, seen_at, use_count
                FROM sticker_catalog
                {where}
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_by_id(self, sticker_id: int) -> StickerSource | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, emoji_id, emoji_package_id, sticker_key
                FROM sticker_catalog
                WHERE id = ? AND enabled = 1
                """,
                (sticker_id,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE sticker_catalog SET use_count = use_count + 1 WHERE id = ?",
                (sticker_id,),
            )
        return StickerSource(
            summary=str(row["summary"]),
            emoji_id=str(row["emoji_id"]),
            emoji_package_id=str(row["emoji_package_id"]),
            key=str(row["sticker_key"]),
        )

    def get_metadata(self, sticker_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, summary, emoji_id, emoji_package_id,
                       sticker_key, image_source, category, usage_hint,
                       enabled, seen_at, use_count
                FROM sticker_catalog
                WHERE id = ?
                """,
                (sticker_id,),
            ).fetchone()
        return dict(row) if row else None

    def update(
        self,
        sticker_id: int,
        *,
        category: str,
        usage_hint: str,
        enabled: bool,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sticker_catalog
                SET category = ?, usage_hint = ?, enabled = ?
                WHERE id = ?
                """,
                (
                    category.strip()[:80] or "未分类",
                    usage_hint.strip()[:300],
                    1 if enabled else 0,
                    sticker_id,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, sticker_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sticker_catalog WHERE id = ?",
                (sticker_id,),
            )
            return cursor.rowcount > 0

    def categories(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT category
                FROM sticker_catalog
                WHERE enabled = 1
                ORDER BY category ASC
                """
            ).fetchall()
        return [str(row["category"]) for row in rows]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
