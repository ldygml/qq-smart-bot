from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GroupPolicy:
    group_id: int
    name: str
    enabled: bool
    require_mention: bool
    smart_participation: bool
    behavior_learning: bool
    min_reply_confidence: float
    reply_cooldown_seconds: float
    hourly_reply_limit: int
    participation_model: str
    trigger_prefixes: str
    quote_replies: bool
    quote_counts_as_mention: bool
    auto_sticker_enabled: bool
    avoid_last_word: bool
    custom_prompt: str

    @property
    def trigger_prefix_values(self) -> tuple[str, ...]:
        return tuple(
            value.strip()
            for value in self.trigger_prefixes.replace("，", ",").split(",")
            if value.strip()
        )


class GroupProfileStore:
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
                CREATE TABLE IF NOT EXISTS group_profiles (
                    group_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    require_mention INTEGER NOT NULL DEFAULT 0,
                    smart_participation INTEGER NOT NULL DEFAULT 1,
                    behavior_learning INTEGER NOT NULL DEFAULT 1,
                    min_reply_confidence REAL NOT NULL DEFAULT 0.65,
                    reply_cooldown_seconds REAL NOT NULL DEFAULT 90,
                    hourly_reply_limit INTEGER NOT NULL DEFAULT 20,
                    participation_model TEXT NOT NULL DEFAULT '',
                    trigger_prefixes TEXT NOT NULL DEFAULT '',
                    quote_replies INTEGER NOT NULL DEFAULT 1,
                    quote_counts_as_mention INTEGER NOT NULL DEFAULT 1,
                    auto_sticker_enabled INTEGER NOT NULL DEFAULT 1,
                    avoid_last_word INTEGER NOT NULL DEFAULT 1,
                    custom_prompt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def ensure(
        self,
        group_id: int,
        *,
        defaults: dict[str, Any],
        name: str = "",
    ) -> GroupPolicy:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_profiles
                    (
                        group_id, name, enabled, require_mention,
                        smart_participation, behavior_learning,
                        min_reply_confidence, reply_cooldown_seconds,
                        hourly_reply_limit, participation_model,
                        trigger_prefixes, quote_replies,
                        quote_counts_as_mention, auto_sticker_enabled,
                        avoid_last_word, custom_prompt, created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    name = CASE
                        WHEN excluded.name != '' THEN excluded.name
                        ELSE group_profiles.name
                    END
                """,
                (
                    group_id,
                    name.strip()[:120],
                    _bool_int(defaults.get("enabled", True)),
                    _bool_int(defaults.get("require_mention", True)),
                    _bool_int(defaults.get("smart_participation", True)),
                    _bool_int(defaults.get("behavior_learning", True)),
                    float(defaults.get("min_reply_confidence", 0.65)),
                    float(defaults.get("reply_cooldown_seconds", 90)),
                    int(defaults.get("hourly_reply_limit", 20)),
                    str(defaults.get("participation_model", "")),
                    str(defaults.get("trigger_prefixes", "")),
                    _bool_int(defaults.get("quote_replies", True)),
                    _bool_int(defaults.get("quote_counts_as_mention", True)),
                    _bool_int(defaults.get("auto_sticker_enabled", True)),
                    _bool_int(defaults.get("avoid_last_word", True)),
                    str(defaults.get("custom_prompt", "")),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        assert row is not None
        return _policy(row)

    def get(self, group_id: int) -> GroupPolicy | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return _policy(row) if row else None

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            behavior_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'group_behavior'
                """
            ).fetchone()
            if behavior_exists:
                rows = connection.execute(
                    """
                    SELECT p.*,
                           COALESCE(b.messages_seen, 0) AS messages_seen,
                           COALESCE(b.replies_sent, 0) AS replies_sent,
                           b.last_message_at, b.last_reply_at
                    FROM group_profiles p
                    LEFT JOIN group_behavior b ON b.group_id = p.group_id
                    ORDER BY p.updated_at DESC, p.group_id ASC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT p.*, 0 AS messages_seen, 0 AS replies_sent,
                           NULL AS last_message_at, NULL AS last_reply_at
                    FROM group_profiles p
                    ORDER BY p.updated_at DESC, p.group_id ASC
                    """
                ).fetchall()
        return [_policy_row(row) for row in rows]

    def update(
        self,
        group_id: int,
        *,
        values: dict[str, Any],
    ) -> GroupPolicy | None:
        allowed = {
            "name",
            "enabled",
            "require_mention",
            "smart_participation",
            "behavior_learning",
            "min_reply_confidence",
            "reply_cooldown_seconds",
            "hourly_reply_limit",
            "participation_model",
            "trigger_prefixes",
            "quote_replies",
            "quote_counts_as_mention",
            "auto_sticker_enabled",
            "avoid_last_word",
            "custom_prompt",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get(group_id)
        updates["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        parameters = [
            _normalize_value(key, value)
            for key, value in updates.items()
        ]
        parameters.append(group_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE group_profiles SET {assignments} WHERE group_id = ?",
                parameters,
            )
            if cursor.rowcount <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return _policy(row) if row else None

    def delete(self, group_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM group_profiles WHERE group_id = ?",
                (group_id,),
            )
            return cursor.rowcount > 0


def _policy(row: sqlite3.Row) -> GroupPolicy:
    return GroupPolicy(
        group_id=int(row["group_id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        require_mention=bool(row["require_mention"]),
        smart_participation=bool(row["smart_participation"]),
        behavior_learning=bool(row["behavior_learning"]),
        min_reply_confidence=float(row["min_reply_confidence"]),
        reply_cooldown_seconds=float(row["reply_cooldown_seconds"]),
        hourly_reply_limit=int(row["hourly_reply_limit"]),
        participation_model=str(row["participation_model"]),
        trigger_prefixes=str(row["trigger_prefixes"]),
        quote_replies=bool(row["quote_replies"]),
        quote_counts_as_mention=bool(row["quote_counts_as_mention"]),
        auto_sticker_enabled=bool(row["auto_sticker_enabled"]),
        avoid_last_word=bool(row["avoid_last_word"]),
        custom_prompt=str(row["custom_prompt"]),
    )


def _policy_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in (
        "enabled",
        "require_mention",
        "smart_participation",
        "behavior_learning",
        "quote_replies",
        "quote_counts_as_mention",
        "auto_sticker_enabled",
        "avoid_last_word",
    ):
        item[key] = bool(item[key])
    return item


def _normalize_value(key: str, value: Any) -> Any:
    if key in {
        "enabled",
        "require_mention",
        "smart_participation",
        "behavior_learning",
        "quote_replies",
        "quote_counts_as_mention",
        "auto_sticker_enabled",
        "avoid_last_word",
    }:
        return _bool_int(value)
    if key == "min_reply_confidence":
        return min(1.0, max(0.0, float(value)))
    if key == "reply_cooldown_seconds":
        return min(3600.0, max(0.0, float(value)))
    if key == "hourly_reply_limit":
        return min(500, max(1, int(value)))
    if key in {"name", "participation_model", "trigger_prefixes", "custom_prompt"}:
        return str(value or "").strip()
    return value


def _bool_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
