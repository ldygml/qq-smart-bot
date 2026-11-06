from __future__ import annotations

import sqlite3
import threading
import re
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
    poke_policy: str
    active_hours: str
    blocked_keywords: str
    quote_mode: str
    welcome_message: str
    reply_delay_min_ms: int
    reply_delay_max_ms: int
    announcement_digest_hours: int
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
                    poke_policy TEXT NOT NULL DEFAULT 'allow',
                    active_hours TEXT NOT NULL DEFAULT '',
                    blocked_keywords TEXT NOT NULL DEFAULT '',
                    quote_mode TEXT NOT NULL DEFAULT 'smart',
                    welcome_message TEXT NOT NULL DEFAULT '',
                    reply_delay_min_ms INTEGER NOT NULL DEFAULT 200,
                    reply_delay_max_ms INTEGER NOT NULL DEFAULT 900,
                    announcement_digest_hours INTEGER NOT NULL DEFAULT 24,
                    custom_prompt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            profile_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(group_profiles)"
                ).fetchall()
            }
            for column, definition in (
                ("poke_policy", "TEXT NOT NULL DEFAULT 'allow'"),
                ("active_hours", "TEXT NOT NULL DEFAULT ''"),
                ("blocked_keywords", "TEXT NOT NULL DEFAULT ''"),
                ("quote_mode", "TEXT NOT NULL DEFAULT 'smart'"),
                ("welcome_message", "TEXT NOT NULL DEFAULT ''"),
                ("reply_delay_min_ms", "INTEGER NOT NULL DEFAULT 200"),
                ("reply_delay_max_ms", "INTEGER NOT NULL DEFAULT 900"),
                ("announcement_digest_hours", "INTEGER NOT NULL DEFAULT 24"),
            ):
                if column not in profile_columns:
                    connection.execute(
                        f"ALTER TABLE group_profiles "
                        f"ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_group_knowledge_group
                ON group_knowledge (group_id, kind, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_member_preferences (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    no_jokes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, user_id)
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
                        avoid_last_word, poke_policy, active_hours,
                        blocked_keywords, quote_mode, welcome_message,
                        reply_delay_min_ms, reply_delay_max_ms,
                        announcement_digest_hours, custom_prompt,
                        created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    name = CASE
                        WHEN group_profiles.name = ''
                             AND excluded.name != ''
                        THEN excluded.name
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
                    str(defaults.get("poke_policy", "allow")),
                    str(defaults.get("active_hours", "")),
                    str(defaults.get("blocked_keywords", "")),
                    str(defaults.get("quote_mode", "smart")),
                    str(defaults.get("welcome_message", "")),
                    int(defaults.get("reply_delay_min_ms", 200)),
                    int(defaults.get("reply_delay_max_ms", 900)),
                    int(defaults.get("announcement_digest_hours", 24)),
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
            "poke_policy",
            "active_hours",
            "blocked_keywords",
            "quote_mode",
            "welcome_message",
            "reply_delay_min_ms",
            "reply_delay_max_ms",
            "announcement_digest_hours",
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

    def knowledge(
        self,
        group_id: int,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE group_id = ?"
        parameters: list[Any] = [group_id]
        if kind:
            where += " AND kind = ?"
            parameters.append(kind)
        parameters.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, group_id, kind, title, content,
                       created_at, updated_at
                FROM group_knowledge
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_knowledge(
        self,
        group_id: int,
        *,
        kind: str,
        title: str,
        content: str,
    ) -> int:
        now = _utc_now()
        normalized_kind = kind.strip().lower()[:40] or "note"
        normalized_title = title.strip()[:160] or "群资料"
        normalized_content = content.strip()[:20_000]
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO group_knowledge
                    (
                        group_id, kind, title, content,
                        created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    normalized_kind,
                    normalized_title,
                    normalized_content,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def delete_knowledge(self, group_id: int, knowledge_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM group_knowledge
                WHERE id = ? AND group_id = ?
                """,
                (knowledge_id, group_id),
            )
            return cursor.rowcount > 0

    def knowledge_context(
        self,
        group_id: int,
        *,
        query: str,
        limit: int = 8,
    ) -> str:
        rows = self.knowledge(group_id, limit=200)
        if not rows:
            return ""
        query_terms = set(
            part.lower()
            for part in re.findall(
                r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}",
                query,
            )
        )
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in rows:
            text = f"{item['title']} {item['content']}".lower()
            score = sum(1 for term in query_terms if term in text)
            if item["kind"] in {"rule", "faq"}:
                score += 1
            scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1]["updated_at"])), reverse=False)
        selected = [item for score, item in scored[:limit] if score > 0]
        if not selected:
            selected = [item for _, item in scored[:3]]
        return "\n".join(
            f"- [{item['kind']}] {item['title']}：{item['content'][:800]}"
            for item in selected
        )

    def member_preference(
        self,
        group_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT group_id, user_id, nickname, notes,
                       no_jokes, updated_at
                FROM group_member_preferences
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
        return _member_preference_row(row) if row else None

    def member_preferences(
        self,
        group_id: int,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT group_id, user_id, nickname, notes,
                       no_jokes, updated_at
                FROM group_member_preferences
                WHERE group_id = ?
                ORDER BY updated_at DESC
                """,
                (group_id,),
            ).fetchall()
        return [_member_preference_row(row) for row in rows]

    def save_member_preference(
        self,
        group_id: int,
        user_id: int,
        *,
        nickname: str,
        notes: str,
        no_jokes: bool,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_member_preferences
                    (
                        group_id, user_id, nickname, notes,
                        no_jokes, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    notes = excluded.notes,
                    no_jokes = excluded.no_jokes,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    user_id,
                    nickname.strip()[:120],
                    notes.strip()[:2000],
                    1 if no_jokes else 0,
                    _utc_now(),
                ),
            )


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
        poke_policy=str(row["poke_policy"] or "allow"),
        active_hours=str(row["active_hours"] or ""),
        blocked_keywords=str(row["blocked_keywords"] or ""),
        quote_mode=str(row["quote_mode"] or "smart"),
        welcome_message=str(row["welcome_message"] or ""),
        reply_delay_min_ms=int(row["reply_delay_min_ms"]),
        reply_delay_max_ms=int(row["reply_delay_max_ms"]),
        announcement_digest_hours=int(row["announcement_digest_hours"]),
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


def _member_preference_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["no_jokes"] = bool(item["no_jokes"])
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
    if key in {"reply_delay_min_ms", "reply_delay_max_ms"}:
        return min(10_000, max(0, int(value)))
    if key == "announcement_digest_hours":
        return min(720, max(0, int(value)))
    if key in {
        "name",
        "participation_model",
        "trigger_prefixes",
        "poke_policy",
        "active_hours",
        "blocked_keywords",
        "quote_mode",
        "welcome_message",
        "custom_prompt",
    }:
        return str(value or "").strip()
    return value


def _bool_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
