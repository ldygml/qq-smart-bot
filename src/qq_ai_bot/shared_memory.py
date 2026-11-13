from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

_COMMON_TERM_STOPS = {
    "一个",
    "什么",
    "今天",
    "明天",
    "昨天",
    "最近",
    "下周",
    "时候",
    "我们",
    "他们",
    "自己",
    "这个",
    "那个",
    "可以",
    "没有",
    "还是",
    "就是",
    "正在",
    "觉得",
    "感觉",
    "准备",
    "开始",
    "喜欢",
    "学习",
}


class SharedMemoryStore:
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
                CREATE TABLE IF NOT EXISTS shared_people (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    style_profile TEXT NOT NULL DEFAULT '',
                    relationship_note TEXT NOT NULL DEFAULT '',
                    profile_updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS shared_profile_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT NOT NULL DEFAULT '',
                    new_value TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shared_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_user_id INTEGER NOT NULL,
                    subject_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    visibility TEXT NOT NULL
                        CHECK (visibility IN ('shareable', 'private')),
                    source_conversation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_memories_subject
                ON shared_memories (subject_user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_shared_memories_visibility
                ON shared_memories (visibility, updated_at DESC);

                CREATE TABLE IF NOT EXISTS shared_reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    visibility TEXT NOT NULL
                        CHECK (visibility IN ('shareable', 'private')),
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shared_growth (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aspect TEXT NOT NULL,
                    content TEXT NOT NULL,
                    visibility TEXT NOT NULL
                        CHECK (visibility IN ('shareable', 'private')),
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    subject_user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    old_content TEXT NOT NULL DEFAULT '',
                    new_content TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_events_memory
                ON memory_events (memory_id, id DESC);

                CREATE TABLE IF NOT EXISTS memory_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_user_id INTEGER NOT NULL,
                    old_memory_id INTEGER NOT NULL,
                    new_memory_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    resolution TEXT NOT NULL DEFAULT 'newer_wins',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_conflicts_subject
                ON memory_conflicts (subject_user_id, id DESC);

                CREATE TABLE IF NOT EXISTS mind_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shared_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    group_id INTEGER,
                    speaker_user_id INTEGER NOT NULL,
                    speaker_name TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_episodes_created
                ON shared_episodes (id DESC);
                CREATE INDEX IF NOT EXISTS idx_shared_episodes_speaker
                ON shared_episodes (speaker_user_id, id DESC);

                CREATE TABLE IF NOT EXISTS shared_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    group_id INTEGER,
                    speaker_user_id INTEGER NOT NULL,
                    speaker_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    replied INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_shared_observations_pending
                ON shared_observations (processed, id ASC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(shared_people)"
                ).fetchall()
            }
            for column, definition in (
                (
                    "style_profile",
                    "TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "relationship_note",
                    "TEXT NOT NULL DEFAULT ''",
                ),
                ("profile_updated_at", "TEXT"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE shared_people "
                        f"ADD COLUMN {column} {definition}"
                    )

            memory_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(shared_memories)"
                ).fetchall()
            }
            for column, definition in (
                ("category", "TEXT NOT NULL DEFAULT 'general'"),
                ("fact_key", "TEXT NOT NULL DEFAULT ''"),
                ("confidence", "REAL NOT NULL DEFAULT 0.7"),
                ("source_episode_id", "INTEGER"),
                ("source_observation_id", "INTEGER"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("superseded_by", "INTEGER"),
                ("superseded_at", "TEXT"),
                ("follow_up_at", "TEXT"),
                ("follow_up_status", "TEXT NOT NULL DEFAULT 'none'"),
            ):
                if column not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE shared_memories "
                        f"ADD COLUMN {column} {definition}"
                    )

            reflection_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(shared_reflections)"
                ).fetchall()
            }
            for column, definition in (
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("source_memory_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ("evidence_count", "INTEGER NOT NULL DEFAULT 1"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ):
                if column not in reflection_columns:
                    connection.execute(
                        f"ALTER TABLE shared_reflections "
                        f"ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                UPDATE shared_reflections
                SET updated_at = created_at
                WHERE updated_at = ''
                """
            )

            growth_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(shared_growth)"
                ).fetchall()
            }
            for column, definition in (
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("source_memory_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in growth_columns:
                    connection.execute(
                        f"ALTER TABLE shared_growth "
                        f"ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                UPDATE shared_growth
                SET created_at = updated_at
                WHERE created_at = ''
                """
            )

            conflict_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_conflicts)"
                ).fetchall()
            }
            if "review_status" not in conflict_columns:
                connection.execute(
                    "ALTER TABLE memory_conflicts "
                    "ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
                )

            episode_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(shared_episodes)"
                ).fetchall()
            }
            if "processed" not in episode_columns:
                connection.execute(
                    "ALTER TABLE shared_episodes "
                    "ADD COLUMN processed INTEGER NOT NULL DEFAULT 0"
                )

    def record_person(self, user_id: int, display_name: str) -> None:
        name = display_name.strip() or str(user_id)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO shared_people
                    (user_id, display_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen = excluded.last_seen
                """,
                (user_id, name, now, now),
            )

    def add_memory(
        self,
        *,
        subject_user_id: int,
        subject_name: str,
        content: str,
        visibility: str,
        source_conversation: str,
        category: str = "general",
        fact_key: str = "",
        confidence: float = 0.7,
        source_episode_id: int | None = None,
        source_observation_id: int | None = None,
        follow_up_at: str | None = None,
    ) -> int | None:
        fact = content.strip()
        if not fact:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        normalized_category = _normalize_label(category, "general")
        normalized_key = _normalize_label(fact_key, "")
        normalized_confidence = min(1.0, max(0.0, float(confidence)))
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, content, status FROM shared_memories
                WHERE subject_user_id = ? AND content = ?
                LIMIT 1
                """,
                (subject_user_id, fact),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET visibility = ?, category = ?, fact_key = ?,
                        confidence = MAX(confidence, ?), updated_at = ?,
                        status = 'active', superseded_by = NULL,
                        superseded_at = NULL
                    WHERE id = ?
                    """,
                    (
                        normalized_visibility,
                        normalized_category,
                        normalized_key,
                        normalized_confidence,
                        now,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])

            conflicted: sqlite3.Row | None = None
            if normalized_key:
                conflicted = connection.execute(
                    """
                    SELECT id, content
                    FROM shared_memories
                    WHERE subject_user_id = ? AND category = ?
                      AND fact_key = ? AND status = 'active'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (subject_user_id, normalized_category, normalized_key),
                ).fetchone()

            cursor = connection.execute(
                """
                INSERT INTO shared_memories
                    (
                        subject_user_id, subject_name, content, visibility,
                        source_conversation, created_at, updated_at,
                        category, fact_key, confidence, source_episode_id,
                        source_observation_id, follow_up_at,
                        follow_up_status, status
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    subject_user_id,
                    subject_name.strip() or str(subject_user_id),
                    fact,
                    normalized_visibility,
                    source_conversation,
                    now,
                    now,
                    normalized_category,
                    normalized_key,
                    normalized_confidence,
                    source_episode_id,
                    source_observation_id,
                    follow_up_at,
                    "pending" if follow_up_at else "none",
                ),
            )
            memory_id = int(cursor.lastrowid)
            if conflicted is not None:
                old_memory_id = int(conflicted["id"])
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET status = 'superseded', superseded_by = ?,
                        superseded_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (memory_id, now, now, old_memory_id),
                )
                connection.execute(
                    """
                    INSERT INTO memory_conflicts
                        (
                            subject_user_id, old_memory_id, new_memory_id,
                            reason, resolution, created_at
                        )
                    VALUES (?, ?, ?, ?, 'newer_wins', ?)
                    """,
                    (
                        subject_user_id,
                        old_memory_id,
                        memory_id,
                        f"同一主题“{normalized_key}”出现了更新后的说法",
                        now,
                    ),
                )
                self._record_memory_event(
                    connection,
                    memory_id=old_memory_id,
                    subject_user_id=subject_user_id,
                    event_type="superseded",
                    old_content=str(conflicted["content"]),
                    new_content=fact,
                    details=f"由记忆 #{memory_id} 取代",
                    created_at=now,
                )
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                subject_user_id=subject_user_id,
                event_type="created",
                old_content="",
                new_content=fact,
                details=(
                    f"来源会话：{source_conversation}"
                    + (
                        f"；来源片段：#{source_episode_id}"
                        if source_episode_id is not None
                        else ""
                    )
                ),
                created_at=now,
            )
            return memory_id

    def mentioned_people(self, text: str) -> list[tuple[int, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id, display_name FROM shared_people"
            ).fetchall()
        normalized = text.lower()
        matches: list[tuple[int, str]] = []
        for row in rows:
            user_id = int(row["user_id"])
            name = str(row["display_name"])
            if str(user_id) in normalized or (
                len(name.strip()) >= 2 and name.lower() in normalized
            ):
                matches.append((user_id, name))
        return matches

    def get_person(self, user_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, display_name, first_seen, last_seen,
                       style_profile, relationship_note, profile_updated_at
                FROM shared_people
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_people(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.user_id, p.display_name, p.first_seen, p.last_seen,
                       p.style_profile, p.relationship_note,
                       p.profile_updated_at,
                       (
                           SELECT COUNT(*)
                           FROM shared_episodes e
                           WHERE e.speaker_user_id = p.user_id
                       ) AS episode_count
                FROM shared_people p
                ORDER BY p.last_seen DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_person_profile(
        self,
        user_id: int,
        *,
        style_profile: str,
        relationship_note: str,
    ) -> bool:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT style_profile, relationship_note
                FROM shared_people WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if existing is None:
                return False
            now = _utc_now()
            if (
                str(existing["relationship_note"])
                != relationship_note.strip()
            ):
                connection.execute(
                    """
                    INSERT INTO shared_profile_events
                        (
                            user_id, field_name, old_value,
                            new_value, created_at
                        )
                    VALUES (?, 'relationship_note', ?, ?, ?)
                    """,
                    (
                        user_id,
                        str(existing["relationship_note"]),
                        relationship_note.strip(),
                        now,
                    ),
                )
            cursor = connection.execute(
                """
                UPDATE shared_people
                SET style_profile = ?, relationship_note = ?,
                    profile_updated_at = ?
                WHERE user_id = ?
                """,
                (
                    style_profile.strip()[:1000],
                    relationship_note.strip()[:1000],
                    now,
                    user_id,
                ),
            )
            return cursor.rowcount > 0

    def relationship_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.user_id, e.field_name, e.old_value,
                       e.new_value, e.created_at, p.display_name
                FROM shared_profile_events e
                LEFT JOIN shared_people p ON p.user_id = e.user_id
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def episodes_for_speaker(
        self,
        user_id: int,
        *,
        after_id: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_content, assistant_content, created_at
                FROM shared_episodes
                WHERE speaker_user_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (user_id, after_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def context_for(
        self,
        *,
        current_user_id: int,
        current_user_name: str,
        query: str,
        limit: int = 14,
        include_private: bool = False,
        current_conversation_id: str = "",
    ) -> str:
        mentioned = self.mentioned_people(query)
        broad_query = _is_broad_memory_query(query)
        selected: dict[int, tuple[float, sqlite3.Row]] = {}
        with self._lock, self._connect() as connection:
            own_rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility,
                       category, confidence, updated_at
                FROM shared_memories
                WHERE subject_user_id = ? AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 24
                """,
                (current_user_id,),
            ).fetchall()
            for row in own_rows:
                if not broad_query and not _text_matches_query(
                    str(row["content"]),
                    query,
                ):
                    continue
                selected[int(row["id"])] = (
                    _memory_effective_score(row, query) + 0.12,
                    row,
                )

            if mentioned:
                placeholders = ",".join("?" for _ in mentioned)
                mentioned_ids = [user_id for user_id, _ in mentioned]
                mentioned_rows = connection.execute(
                    f"""
                    SELECT id, subject_user_id, subject_name, content,
                           visibility, category, confidence, updated_at
                    FROM shared_memories
                    WHERE subject_user_id IN ({placeholders})
                      AND status = 'active'
                      AND (visibility = 'shareable' OR ? = 1)
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 32
                    """,
                    [*mentioned_ids, 1 if include_private else 0],
                ).fetchall()
                for row in mentioned_rows:
                    selected[int(row["id"])] = (
                        _memory_effective_score(row, query) + 0.08,
                        row,
                    )

            recent_rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility,
                       category, confidence, updated_at
                FROM shared_memories
                WHERE status = 'active'
                  AND (visibility = 'shareable' OR ? = 1)
                ORDER BY updated_at DESC, id DESC
                LIMIT 24
                """,
                (1 if include_private else 0,),
            ).fetchall()
            for row in recent_rows:
                if not broad_query and not _text_matches_query(
                    str(row["content"]),
                    query,
                ):
                    continue
                selected[int(row["id"])] = (
                    _memory_effective_score(row, query),
                    row,
                )

        rows = [
            row
            for _, row in sorted(
                selected.values(),
                key=lambda pair: (pair[0], str(pair[1]["updated_at"])),
                reverse=True,
            )[: max(1, limit)]
        ]
        lines = [
            "跨会话长期共享记忆：",
            f"当前联系人：{current_user_name}（QQ {current_user_id}）",
            (
                "只自然使用与话题相关的信息。private 记忆只属于当事人，"
                "不能告诉其他联系人，也不要复述私密原话。"
            ),
        ]
        current_person = self.get_person(current_user_id)
        if current_person:
            style_profile = str(current_person["style_profile"]).strip()
            relationship_note = str(
                current_person["relationship_note"]
            ).strip()
            if style_profile:
                lines.append(f"当前联系人互动画像：{style_profile}")
            if relationship_note:
                lines.append(f"当前关系备注：{relationship_note}")
        for row in rows:
            subject_name = str(row["subject_name"])
            if int(row["subject_user_id"]) == current_user_id:
                subject_name = f"当前联系人 {subject_name}"
            age_days = _memory_age_days(str(row["updated_at"]))
            confidence = float(row["confidence"])
            reliability = confidence * (
                0.5 ** (age_days / _memory_half_life(str(row["category"])))
            )
            caution = (
                "；旧信息，未经确认不要当成确定事实"
                if reliability < 0.45
                else ""
            )
            lines.append(
                f"- {subject_name}（{row['visibility']}，"
                f"可靠度 {reliability:.2f}{caution}）：{row['content']}"
            )
        include_abstract = bool(mentioned) or any(
            marker in query
            for marker in ("关系", "反思", "成长", "变化", "你怎么看")
        )
        reflections = [
            item
            for item in self.reflection_summary(limit=12)
            if (broad_query and include_abstract)
            or _text_matches_query(str(item["content"]), query)
        ][:4]
        growth = [
            item
            for item in self.growth_summary(limit=12)
            if (broad_query and include_abstract)
            or _text_matches_query(str(item["content"]), query)
        ][:4]
        if reflections:
            lines.append("长期反思（只是推测，不要说得比证据更确定）：")
            for reflection in reflections:
                lines.append(
                    f"- 置信度 {float(reflection['confidence']):.1f}："
                    f"{reflection['content']}"
                )
        if growth:
            lines.append("你的成长笔记（保持克制，不要刻意表现）：")
            for item in growth:
                lines.append(f"- {item['content']}")

        with self._lock, self._connect() as connection:
            episode_rows = connection.execute(
                """
                SELECT conversation_id, message_type, group_id,
                       speaker_user_id, speaker_name,
                       user_content, assistant_content
                FROM shared_episodes
                WHERE conversation_id != ?
                  AND (speaker_user_id = ? OR ? = 1)
                ORDER BY id DESC
                LIMIT 8
                """,
                (
                    current_conversation_id,
                    current_user_id,
                    1 if include_private else 0,
                ),
            ).fetchall()
        episode_rows = [
            row
            for row in episode_rows
            if (
                broad_query
                or _text_matches_query(
                    f"{row['user_content']} {row['assistant_content']}",
                    query,
                )
            )
        ][:4]
        if episode_rows:
            lines.append("最近其他会话片段（只在相关时自然引用）：")
            for row in episode_rows:
                context_label = (
                    f"群 {row['group_id']}"
                    if row["message_type"] == "group"
                    else "私聊"
                )
                user_preview = str(row["user_content"]).replace("\n", " ")[:240]
                bot_preview = str(row["assistant_content"]).replace("\n", " ")[:240]
                lines.append(
                    f"- [{context_label}] {row['speaker_name']}：{user_preview}；"
                    f"你当时回复：{bot_preview}"
                )
        with self._lock, self._connect() as connection:
            observation_rows = connection.execute(
                """
                SELECT conversation_id, message_type, group_id,
                       speaker_user_id, speaker_name, content
                FROM shared_observations
                WHERE replied = 0 AND conversation_id != ?
                  AND (speaker_user_id = ? OR ? = 1)
                ORDER BY id DESC
                LIMIT 6
                """,
                (
                    current_conversation_id,
                    current_user_id,
                    1 if include_private else 0,
                ),
            ).fetchall()
        observation_rows = [
            row
            for row in observation_rows
            if broad_query
            or _text_matches_query(str(row["content"]), query)
        ][:4]
        if observation_rows:
            lines.append("最近未回复的其他群聊消息（只在相关时参考）：")
            for row in observation_rows:
                context_label = (
                    f"群 {row['group_id']}"
                    if row["message_type"] == "group"
                    else "私聊"
                )
                content = str(row["content"]).replace("\n", " ")[:220]
                lines.append(
                    f"- [{context_label}] {row['speaker_name']}：{content}"
                )
        return "\n".join(lines)

    def list_memories(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility,
                       category, fact_key, confidence, source_conversation,
                       source_episode_id, status, superseded_by,
                       created_at, updated_at, superseded_at
                FROM shared_memories
                WHERE subject_user_id = ? AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 50
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def memories_since(self, memory_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility,
                       category, fact_key, confidence, source_conversation,
                       source_episode_id, status, created_at, updated_at
                FROM shared_memories
                WHERE id > ? AND status IN ('active', 'superseded')
                ORDER BY id ASC
                LIMIT ?
                """,
                (memory_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_active_memories(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility,
                       category, fact_key, confidence, source_conversation,
                       source_episode_id, status, created_at, updated_at
                FROM shared_memories
                WHERE status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def episodes_since(
        self,
        episode_id: int,
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, message_type, group_id,
                       speaker_user_id, speaker_name, user_content,
                       assistant_content, created_at
                FROM shared_episodes
                WHERE id > ? AND processed = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (episode_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def known_group_ids(self) -> list[int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT group_id FROM shared_episodes
                WHERE group_id IS NOT NULL
                UNION
                SELECT group_id FROM shared_observations
                WHERE group_id IS NOT NULL
                """
            ).fetchall()
        return sorted({int(row["group_id"]) for row in rows})

    def memory_timeline(
        self,
        user_id: int | None = None,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            if user_id is None:
                rows = connection.execute(
                    """
                    SELECT m.*,
                           COALESCE(e.user_content, o.content) AS source_text,
                           COALESCE(e.assistant_content, '') AS source_reply,
                           COALESCE(e.created_at, o.created_at)
                               AS source_created_at,
                           (
                               SELECT COUNT(*) FROM memory_conflicts c
                               WHERE c.old_memory_id = m.id
                                  OR c.new_memory_id = m.id
                           ) AS conflict_count
                    FROM shared_memories m
                    LEFT JOIN shared_episodes e
                      ON e.id = m.source_episode_id
                    LEFT JOIN shared_observations o
                      ON o.id = m.source_observation_id
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT m.*,
                           COALESCE(e.user_content, o.content) AS source_text,
                           COALESCE(e.assistant_content, '') AS source_reply,
                           COALESCE(e.created_at, o.created_at)
                               AS source_created_at,
                           (
                               SELECT COUNT(*) FROM memory_conflicts c
                               WHERE c.old_memory_id = m.id
                                  OR c.new_memory_id = m.id
                           ) AS conflict_count
                    FROM shared_memories m
                    LEFT JOIN shared_episodes e
                      ON e.id = m.source_episode_id
                    LEFT JOIN shared_observations o
                      ON o.id = m.source_observation_id
                    WHERE m.subject_user_id = ?
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    (user_id, max(1, limit)),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_conflicts(
        self,
        *,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE c.subject_user_id = ?" if user_id is not None else ""
        parameters: list[Any] = [user_id] if user_id is not None else []
        parameters.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, c.subject_user_id, c.old_memory_id,
                       c.new_memory_id, c.reason, c.resolution,
                       c.review_status, c.created_at, p.display_name,
                       old.content AS old_content,
                       new.content AS new_content,
                       old.fact_key AS fact_key
                FROM memory_conflicts c
                LEFT JOIN shared_people p ON p.user_id = c.subject_user_id
                LEFT JOIN shared_memories old ON old.id = c.old_memory_id
                LEFT JOIN shared_memories new ON new.id = c.new_memory_id
                {where}
                ORDER BY c.id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def superseded_ids_for(self, memory_id: int) -> list[int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT old_memory_id FROM memory_conflicts
                WHERE new_memory_id = ?
                ORDER BY id ASC
                """,
                (memory_id,),
            ).fetchall()
        return [int(row["old_memory_id"]) for row in rows]

    def resolve_conflict(
        self,
        conflict_id: int,
        *,
        decision: str,
    ) -> bool:
        if decision not in {"keep_new", "revert_old", "dismiss"}:
            raise ValueError("Invalid conflict decision")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT old_memory_id, new_memory_id
                FROM memory_conflicts
                WHERE id = ?
                """,
                (conflict_id,),
            ).fetchone()
            if row is None:
                return False
            old_id = int(row["old_memory_id"])
            new_id = int(row["new_memory_id"])
            now = _utc_now()
            if decision == "keep_new":
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET status = 'active', superseded_by = NULL,
                        superseded_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, new_id),
                )
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET status = 'superseded', superseded_by = ?,
                        superseded_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_id, now, now, old_id),
                )
                resolution = "newer_wins"
                review_status = "confirmed"
            elif decision == "revert_old":
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET status = 'active', superseded_by = NULL,
                        superseded_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, old_id),
                )
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET status = 'superseded', superseded_by = ?,
                        superseded_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (old_id, now, now, new_id),
                )
                resolution = "manual_old_wins"
                review_status = "reverted"
            else:
                resolution = "dismissed"
                review_status = "dismissed"
            connection.execute(
                """
                UPDATE memory_conflicts
                SET resolution = ?, review_status = ?
                WHERE id = ?
                """,
                (resolution, review_status, conflict_id),
            )
            return True

    def due_followups(
        self,
        *,
        user_id: int | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        before = before or _utc_now()
        where = """
            follow_up_status = 'pending'
            AND follow_up_at IS NOT NULL
            AND follow_up_at <= ?
        """
        parameters: list[Any] = [before]
        if user_id is not None:
            where += " AND subject_user_id = ?"
            parameters.append(user_id)
        parameters.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, subject_user_id, subject_name, content,
                       category, follow_up_at, source_conversation
                FROM shared_memories
                WHERE status = 'active' AND {where}
                ORDER BY follow_up_at ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_followup(
        self,
        memory_id: int,
        *,
        follow_up_at: str | None,
        status: str = "pending",
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shared_memories
                SET follow_up_at = ?, follow_up_status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    follow_up_at,
                    status if follow_up_at else "none",
                    _utc_now(),
                    memory_id,
                ),
            )
            return cursor.rowcount > 0

    def update_memory(
        self,
        memory_id: int,
        *,
        user_id: int | None = None,
        content: str | None = None,
        visibility: str | None = None,
        category: str | None = None,
        fact_key: str | None = None,
    ) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            parameters: list[Any] = [memory_id]
            owner_clause = ""
            if user_id is not None:
                owner_clause = " AND subject_user_id = ?"
                parameters.append(user_id)
            row = connection.execute(
                f"""
                SELECT * FROM shared_memories
                WHERE id = ?{owner_clause}
                """,
                parameters,
            ).fetchone()
            if row is None:
                return False
            updated_content = (
                content.strip() if content is not None else str(row["content"])
            )
            if not updated_content:
                return False
            normalized_visibility = (
                "private"
                if visibility == "private"
                else (
                    "shareable"
                    if visibility == "shareable"
                    else str(row["visibility"])
                )
            )
            connection.execute(
                """
                UPDATE shared_memories
                SET content = ?, visibility = ?, category = ?, fact_key = ?,
                    updated_at = ?, status = 'active'
                WHERE id = ?
                """,
                (
                    updated_content,
                    normalized_visibility,
                    (
                        _normalize_label(category, str(row["category"]))
                        if category is not None
                        else str(row["category"])
                    ),
                    (
                        _normalize_label(fact_key, "")
                        if fact_key is not None
                        else str(row["fact_key"])
                    ),
                    now,
                    memory_id,
                ),
            )
            if updated_content != str(row["content"]):
                self._record_memory_event(
                    connection,
                    memory_id=memory_id,
                    subject_user_id=int(row["subject_user_id"]),
                    event_type="edited",
                    old_content=str(row["content"]),
                    new_content=updated_content,
                    details="管理员手动编辑",
                    created_at=now,
                )
            return True

    def merge_memories(
        self,
        *,
        user_id: int,
        primary_memory_id: int,
        secondary_memory_id: int,
        content: str = "",
    ) -> int | None:
        if primary_memory_id == secondary_memory_id:
            return None
        now = _utc_now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shared_memories
                WHERE subject_user_id = ? AND id IN (?, ?)
                """,
                (user_id, primary_memory_id, secondary_memory_id),
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            primary = by_id.get(primary_memory_id)
            secondary = by_id.get(secondary_memory_id)
            if primary is None or secondary is None:
                return None
            merged_content = content.strip() or (
                f"{str(primary['content']).rstrip('。')}；"
                f"{str(secondary['content']).lstrip('。')}"
            )
            connection.execute(
                """
                UPDATE shared_memories
                SET content = ?, updated_at = ?, status = 'active'
                WHERE id = ?
                """,
                (merged_content, now, primary_memory_id),
            )
            connection.execute(
                """
                UPDATE shared_memories
                SET status = 'merged', superseded_by = ?,
                    superseded_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    primary_memory_id,
                    now,
                    now,
                    secondary_memory_id,
                ),
            )
            self._record_memory_event(
                connection,
                memory_id=primary_memory_id,
                subject_user_id=user_id,
                event_type="merged",
                old_content=str(primary["content"]),
                new_content=merged_content,
                details=f"合并记忆 #{secondary_memory_id}",
                created_at=now,
            )
            self._record_memory_event(
                connection,
                memory_id=secondary_memory_id,
                subject_user_id=user_id,
                event_type="merged_into",
                old_content=str(secondary["content"]),
                new_content=merged_content,
                details=f"并入记忆 #{primary_memory_id}",
                created_at=now,
            )
        return primary_memory_id

    def add_episode(
        self,
        *,
        conversation_id: str,
        message_type: str,
        group_id: int | None,
        speaker_user_id: int,
        speaker_name: str,
        user_content: str,
        assistant_content: str,
        processed: bool = False,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shared_episodes
                    (
                        conversation_id, message_type, group_id,
                        speaker_user_id, speaker_name, user_content,
                        assistant_content, created_at, processed
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    message_type,
                    group_id,
                    speaker_user_id,
                    speaker_name,
                    user_content.strip(),
                    assistant_content.strip(),
                    _utc_now(),
                    1 if processed else 0,
                ),
            )
            return int(cursor.lastrowid)

    def observe_message(
        self,
        *,
        conversation_id: str,
        message_type: str,
        group_id: int | None,
        speaker_user_id: int,
        speaker_name: str,
        content: str,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shared_observations
                    (
                        conversation_id, message_type, group_id,
                        speaker_user_id, speaker_name, content, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    message_type,
                    group_id,
                    speaker_user_id,
                    speaker_name,
                    content.strip(),
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def observations_since(
        self,
        observation_id: int,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, message_type, group_id,
                       speaker_user_id, speaker_name, content, created_at
                FROM shared_observations
                WHERE id > ? AND processed = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (observation_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_observation(
        self,
        observation_id: int,
        *,
        processed: bool,
        replied: bool,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE shared_observations
                SET processed = MAX(processed, ?), replied = MAX(replied, ?)
                WHERE id = ?
                """,
                (
                    1 if processed else 0,
                    1 if replied else 0,
                    observation_id,
                ),
            )

    def mark_episode_processed(self, episode_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE shared_episodes SET processed = 1 WHERE id = ?",
                (episode_id,),
            )

    def add_reflection(
        self,
        *,
        content: str,
        visibility: str,
        confidence: float,
        source_memory_ids: list[int] | None = None,
        supersedes_id: int | None = None,
    ) -> int | None:
        fact = content.strip()
        if not fact:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        score = min(1.0, max(0.0, float(confidence)))
        now = _utc_now()
        source_json = json.dumps(
            sorted(set(source_memory_ids or [])),
            ensure_ascii=False,
        )
        with self._lock, self._connect() as connection:
            if supersedes_id is not None:
                updated = connection.execute(
                    """
                    UPDATE shared_reflections
                    SET content = ?, confidence = ?, visibility = ?,
                        updated_at = ?, source_memory_ids = ?,
                        evidence_count = evidence_count + 1,
                        status = 'active'
                    WHERE id = ?
                    """,
                    (
                        fact,
                        score,
                        normalized_visibility,
                        now,
                        source_json,
                        supersedes_id,
                    ),
                )
                if updated.rowcount:
                    return supersedes_id
            existing = connection.execute(
                "SELECT id FROM shared_reflections WHERE content = ? LIMIT 1",
                (fact,),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_reflections
                    SET confidence = MAX(confidence, ?), visibility = ?,
                        updated_at = ?, source_memory_ids = ?,
                        evidence_count = evidence_count + 1,
                        status = 'active'
                    WHERE id = ?
                    """,
                    (
                        score,
                        normalized_visibility,
                        now,
                        source_json,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO shared_reflections
                    (
                        content, visibility, confidence, created_at,
                        updated_at, source_memory_ids, evidence_count, status
                    )
                VALUES (?, ?, ?, ?, ?, ?, 1, 'active')
                """,
                (
                    fact,
                    normalized_visibility,
                    score,
                    now,
                    now,
                    source_json,
                ),
            )
            return int(cursor.lastrowid)

    def add_growth(
        self,
        *,
        aspect: str,
        content: str,
        visibility: str,
        source_memory_ids: list[int] | None = None,
        supersedes_id: int | None = None,
    ) -> int | None:
        value = content.strip()
        if not value:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        normalized_aspect = aspect.strip() or "general"
        now = _utc_now()
        source_json = json.dumps(
            sorted(set(source_memory_ids or [])),
            ensure_ascii=False,
        )
        with self._lock, self._connect() as connection:
            if supersedes_id is not None:
                updated = connection.execute(
                    """
                    UPDATE shared_growth
                    SET aspect = ?, content = ?, visibility = ?,
                        updated_at = ?, source_memory_ids = ?,
                        evidence_count = evidence_count + 1
                    WHERE id = ?
                    """,
                    (
                        normalized_aspect,
                        value,
                        normalized_visibility,
                        now,
                        source_json,
                        supersedes_id,
                    ),
                )
                if updated.rowcount:
                    return supersedes_id
            existing = connection.execute(
                """
                SELECT id, content FROM shared_growth
                WHERE aspect = ?
                  AND (content = ? OR content LIKE ?)
                LIMIT 1
                """,
                (
                    normalized_aspect,
                    value,
                    f"%{value[:min(24, len(value))]}%",
                ),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_growth
                    SET content = ?, evidence_count = evidence_count + 1,
                        visibility = ?, source_memory_ids = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        value,
                        normalized_visibility,
                        source_json,
                        now,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO shared_growth
                    (
                        aspect, content, visibility, evidence_count,
                        created_at, updated_at, source_memory_ids
                    )
                VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    normalized_aspect,
                    value,
                    normalized_visibility,
                    now,
                    now,
                    source_json,
                ),
            )
            return int(cursor.lastrowid)

    def reflection_summary(
        self,
        limit: int = 8,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, visibility, confidence, created_at,
                       updated_at, evidence_count, status, source_memory_ids
                FROM shared_reflections
                WHERE status = 'active'
                  AND (visibility = 'shareable' OR ? = 1)
                ORDER BY confidence DESC, created_at DESC
                LIMIT ?
                """,
                (1 if include_private else 0, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def growth_summary(
        self,
        limit: int = 8,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, aspect, content, visibility, evidence_count,
                       created_at, updated_at, source_memory_ids
                FROM shared_growth
                WHERE visibility = 'shareable' OR ? = 1
                ORDER BY evidence_count DESC, updated_at DESC
                LIMIT ?
                """,
                (1 if include_private else 0, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reflection(
        self,
        reflection_id: int,
        *,
        content: str,
        visibility: str,
        confidence: float | None = None,
    ) -> bool:
        value = content.strip()
        if not value:
            return False
        with self._lock, self._connect() as connection:
            if confidence is None:
                cursor = connection.execute(
                    """
                    UPDATE shared_reflections
                    SET content = ?, visibility = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        value,
                        "private" if visibility == "private" else "shareable",
                        _utc_now(),
                        reflection_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE shared_reflections
                    SET content = ?, visibility = ?, confidence = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        value,
                        "private" if visibility == "private" else "shareable",
                        min(1.0, max(0.0, float(confidence))),
                        _utc_now(),
                        reflection_id,
                    ),
                )
            return cursor.rowcount > 0

    def update_growth(
        self,
        growth_id: int,
        *,
        aspect: str,
        content: str,
        visibility: str,
    ) -> bool:
        value = content.strip()
        if not value:
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shared_growth
                SET aspect = ?, content = ?, visibility = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    aspect.strip() or "general",
                    value,
                    "private" if visibility == "private" else "shareable",
                    _utc_now(),
                    growth_id,
                ),
            )
            return cursor.rowcount > 0

    def delete_reflection(self, reflection_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM shared_reflections WHERE id = ?",
                (reflection_id,),
            )
            return cursor.rowcount > 0

    def delete_growth(self, growth_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM shared_growth WHERE id = ?",
                (growth_id,),
            )
            return cursor.rowcount > 0

    def get_state(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM mind_state WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mind_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _utc_now()),
            )

    def delete_memory(self, user_id: int, memory_id: int) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT content FROM shared_memories
                WHERE id = ? AND subject_user_id = ?
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return False
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                subject_user_id=user_id,
                event_type="deleted",
                old_content=str(row["content"]),
                new_content="",
                details="管理员或用户删除",
                created_at=_utc_now(),
            )
            cursor = connection.execute(
                """
                DELETE FROM shared_memories
                WHERE id = ? AND subject_user_id = ?
                """,
                (memory_id, user_id),
            )
            return cursor.rowcount > 0

    def forget_user(self, user_id: int) -> int:
        return self.purge_person(user_id)["memories"]

    def purge_person(self, user_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            person_row = connection.execute(
                "SELECT display_name FROM shared_people WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            display_name = (
                str(person_row["display_name"]).strip()
                if person_row
                else ""
            )
            memory_rows = connection.execute(
                """
                SELECT id FROM shared_memories
                WHERE subject_user_id = ?
                """,
                (user_id,),
            ).fetchall()
            memory_ids = {int(row["id"]) for row in memory_rows}
            episode_rows = connection.execute(
                """
                SELECT id FROM shared_episodes
                WHERE speaker_user_id = ?
                """,
                (user_id,),
            ).fetchall()
            episode_ids = {int(row["id"]) for row in episode_rows}
            reflection_ids: set[int] = set()
            for row in connection.execute(
                """
                SELECT id, content, source_memory_ids
                FROM shared_reflections
                """
            ).fetchall():
                if memory_ids.intersection(
                    _source_ids(row["source_memory_ids"])
                ) or (
                    len(display_name) >= 2
                    and display_name in str(row["content"])
                ):
                    reflection_ids.add(int(row["id"]))
            growth_ids: set[int] = set()
            for row in connection.execute(
                """
                SELECT id, content, source_memory_ids
                FROM shared_growth
                """
            ).fetchall():
                if memory_ids.intersection(
                    _source_ids(row["source_memory_ids"])
                ) or (
                    len(display_name) >= 2
                    and display_name in str(row["content"])
                ):
                    growth_ids.add(int(row["id"]))

            counts = {
                "memories": len(memory_ids),
                "episodes": len(episode_ids),
                "observations": connection.execute(
                    """
                    DELETE FROM shared_observations
                    WHERE speaker_user_id = ?
                    """,
                    (user_id,),
                ).rowcount,
                "reflections": 0,
                "growth": 0,
                "people": connection.execute(
                    "DELETE FROM shared_people WHERE user_id = ?",
                    (user_id,),
                ).rowcount,
            }
            connection.execute(
                "DELETE FROM shared_memories WHERE subject_user_id = ?",
                (user_id,),
            )
            connection.execute(
                "DELETE FROM shared_episodes WHERE speaker_user_id = ?",
                (user_id,),
            )
            connection.execute(
                "DELETE FROM memory_conflicts WHERE subject_user_id = ?",
                (user_id,),
            )
            connection.execute(
                "DELETE FROM memory_events WHERE subject_user_id = ?",
                (user_id,),
            )
            if reflection_ids:
                placeholders = ",".join("?" for _ in reflection_ids)
                cursor = connection.execute(
                    f"DELETE FROM shared_reflections WHERE id IN ({placeholders})",
                    list(reflection_ids),
                )
                counts["reflections"] = cursor.rowcount
            if growth_ids:
                placeholders = ",".join("?" for _ in growth_ids)
                cursor = connection.execute(
                    f"DELETE FROM shared_growth WHERE id IN ({placeholders})",
                    list(growth_ids),
                )
                counts["growth"] = cursor.rowcount
            connection.execute(
                "DELETE FROM mind_state WHERE key = ?",
                (f"profile_episode_cursor:{user_id}",),
            )
        return {
            **counts,
            "memory_ids": sorted(memory_ids),
            "episode_ids": sorted(episode_ids),
        }

    def purge_all(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            counts = {
                "people": connection.execute(
                    "DELETE FROM shared_people"
                ).rowcount,
                "memories": connection.execute(
                    "DELETE FROM shared_memories"
                ).rowcount,
                "episodes": connection.execute(
                    "DELETE FROM shared_episodes"
                ).rowcount,
                "observations": connection.execute(
                    "DELETE FROM shared_observations"
                ).rowcount,
                "reflections": connection.execute(
                    "DELETE FROM shared_reflections"
                ).rowcount,
                "growth": connection.execute(
                    "DELETE FROM shared_growth"
                ).rowcount,
                "conflicts": connection.execute(
                    "DELETE FROM memory_conflicts"
                ).rowcount,
            }
            connection.execute("DELETE FROM memory_events")
            connection.execute(
                """
                DELETE FROM mind_state
                WHERE key = 'last_reflected_memory_id'
                   OR key = 'last_harvested_episode_id'
                   OR key = 'last_harvested_observation_id'
                   OR key LIKE 'profile_episode_cursor:%'
                """
            )
        return counts

    def export_data(self) -> dict[str, Any]:
        tables = (
            "shared_people",
            "shared_memories",
            "shared_episodes",
            "shared_observations",
            "shared_reflections",
            "shared_growth",
            "memory_conflicts",
            "memory_events",
            "mind_state",
        )
        with self._lock, self._connect() as connection:
            return {
                table: [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table}"
                    ).fetchall()
                ]
                for table in tables
            }

    def import_data(self, data: dict[str, Any]) -> dict[str, int]:
        imported: dict[str, int] = {}
        allowed_tables = {
            "shared_people",
            "shared_memories",
            "shared_episodes",
            "shared_observations",
            "shared_reflections",
            "shared_growth",
            "memory_conflicts",
            "memory_events",
            "mind_state",
        }
        with self._lock, self._connect() as connection:
            for table, rows in data.items():
                if table not in allowed_tables or not isinstance(rows, list):
                    continue
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                count = 0
                for item in rows[:100_000]:
                    if not isinstance(item, dict):
                        continue
                    values = {
                        key: value
                        for key, value in item.items()
                        if key in columns
                    }
                    if not values:
                        continue
                    placeholders = ",".join("?" for _ in values)
                    names = ",".join(values)
                    try:
                        cursor = connection.execute(
                            f"INSERT OR IGNORE INTO {table} "
                            f"({names}) VALUES ({placeholders})",
                            list(values.values()),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    count += max(0, cursor.rowcount)
                imported[table] = count
        return imported

    def purge_between(
        self,
        *,
        start_at: str,
        end_at: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            memory_rows = connection.execute(
                """
                SELECT id FROM shared_memories
                WHERE created_at >= ? AND created_at < ?
                """,
                (start_at, end_at),
            ).fetchall()
            memory_ids = {int(row["id"]) for row in memory_rows}
            episode_rows = connection.execute(
                """
                SELECT id FROM shared_episodes
                WHERE created_at >= ? AND created_at < ?
                """,
                (start_at, end_at),
            ).fetchall()
            episode_ids = {int(row["id"]) for row in episode_rows}
            observation_count = 0
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                connection.execute(
                    f"DELETE FROM shared_memories WHERE id IN ({placeholders})",
                    list(memory_ids),
                )
            if episode_ids:
                placeholders = ",".join("?" for _ in episode_ids)
                connection.execute(
                    f"DELETE FROM shared_episodes WHERE id IN ({placeholders})",
                    list(episode_ids),
                )
            observation_count = connection.execute(
                """
                DELETE FROM shared_observations
                WHERE created_at >= ? AND created_at < ?
                """,
                (start_at, end_at),
            ).rowcount
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                connection.execute(
                    f"""
                    DELETE FROM memory_conflicts
                    WHERE old_memory_id IN ({placeholders})
                       OR new_memory_id IN ({placeholders})
                    """,
                    [*memory_ids, *memory_ids],
                )
                connection.execute(
                    f"DELETE FROM memory_events "
                    f"WHERE memory_id IN ({placeholders})",
                    list(memory_ids),
                )
                for table in ("shared_reflections", "shared_growth"):
                    stale_ids: list[int] = []
                    for row in connection.execute(
                        f"SELECT id, source_memory_ids FROM {table}"
                    ).fetchall():
                        if memory_ids.intersection(
                            _source_ids(row["source_memory_ids"])
                        ):
                            stale_ids.append(int(row["id"]))
                    if stale_ids:
                        id_placeholders = ",".join(
                            "?" for _ in stale_ids
                        )
                        connection.execute(
                            f"DELETE FROM {table} "
                            f"WHERE id IN ({id_placeholders})",
                            stale_ids,
                        )
        return {
            "memories": sorted(memory_ids),
            "episodes": sorted(episode_ids),
            "observations": observation_count,
        }

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            counts: dict[str, int] = {}
            for name, table in (
                ("people", "shared_people"),
                ("memories", "shared_memories"),
                ("episodes", "shared_episodes"),
                ("reflections", "shared_reflections"),
                ("growth", "shared_growth"),
                ("conflicts", "memory_conflicts"),
            ):
                row = connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table}"
                ).fetchone()
                counts[name] = int(row["total"]) if row else 0
        return counts

    @staticmethod
    def _record_memory_event(
        connection: sqlite3.Connection,
        *,
        memory_id: int,
        subject_user_id: int,
        event_type: str,
        old_content: str,
        new_content: str,
        details: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_events
                (
                    memory_id, subject_user_id, event_type, old_content,
                    new_content, details, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                subject_user_id,
                event_type,
                old_content,
                new_content,
                details,
                created_at,
            ),
        )


class MemoryChatClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        extra_payload: Any = None,
    ) -> str: ...


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    visibility: str
    category: str = "general"
    fact_key: str = ""
    confidence: float = 0.7
    follow_up_days: int | None = None


@dataclass(frozen=True)
class PersonProfileCandidate:
    style_profile: str
    relationship_note: str


@dataclass(frozen=True)
class ReflectionCandidate:
    content: str
    visibility: str
    confidence: float
    supersedes_id: int | None = None


@dataclass(frozen=True)
class GrowthCandidate:
    aspect: str
    content: str
    visibility: str
    supersedes_id: int | None = None


@dataclass(frozen=True)
class ReflectionResult:
    reflections: list[ReflectionCandidate]
    growth: list[GrowthCandidate]


async def extract_person_profile(
    chat_client: MemoryChatClient,
    *,
    model: str,
    display_name: str,
    current_profile: str,
    current_relationship_note: str,
    samples: list[dict[str, Any]],
) -> PersonProfileCandidate:
    sample_lines = [
        (
            f"- 用户：{str(item.get('user_content', ''))[:400]}\n"
            f"  当时回复：{str(item.get('assistant_content', ''))[:300]}"
        )
        for item in samples[-12:]
    ]
    prompt = (
        "根据这个联系人的真实聊天样本，总结稳定的互动画像。不要做心理诊断，"
        "不要推断敏感身份。style_profile 说明对方偏好的回复长度、语气、"
        "直接程度、是否爱开玩笑和适合的沟通方式；relationship_note 说明"
        "当前关系更像陌生人、普通朋友、熟人还是工作关系。"
        "只总结有多次证据支持的稳定倾向，最多各两句话。"
        "若证据不足，保留已有内容或返回空字符串。\n"
        '只返回 JSON：{"style_profile":"...","relationship_note":"..."}\n\n'
        f"联系人：{display_name}\n"
        f"已有互动画像：{current_profile}\n"
        f"已有关系备注：{current_relationship_note}\n"
        "聊天样本：\n"
        + "\n".join(sample_lines)
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": "严格输出 JSON，不要 Markdown，不要解释。",
            },
            {"role": "user", "content": prompt},
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    return _parse_person_profile(reply)


async def extract_memory_candidates(
    chat_client: MemoryChatClient,
    *,
    model: str,
    speaker_name: str,
    speaker_id: int,
    user_message: str,
) -> list[MemoryCandidate]:
    prompt = (
        "你是长期记忆整理器。只从当前发言中提取以后仍有用、且不是临时闲聊的事实。"
        "最多五条，每条独立、简短、不得推测。"
        "必须判断事实类型 category，仅使用以下值："
        "preference（偏好）、commitment（承诺或约定）、relationship（关系变化）、"
        "project（长期项目）、profile（稳定个人信息）、event（重要事件）、"
        "general（其他长期事实）。"
        "fact_key 是同一主题的稳定中文名，例如“居住城市”“正在做的项目”"
        "“周末安排”“对某人的关系”；同一主题以后发生变化时必须沿用相同 fact_key。"
        "偏好、长期计划、关系、重要事件可记为 shareable；"
        "健康、财务、秘密、私人冲突、敏感身份信息记为 private。"
        "若事实包含明确承诺、约定或可检查的项目节点，可填写 follow_up_days"
        "表示多少天后适合自然询问进展；没有明确时间就返回 null。"
        "不要仅凭“我是某明星/某球员”、玩笑、角色扮演、假设、反问、"
        "玩梗或临时称呼提取稳定身份；必须结合明确语境。"
        "承诺必须是明确答应或计划执行的事情，不能把客套话当成承诺。"
        "证据不足时返回空数组。\n"
        "只返回 JSON："
        '{"memories":[{"content":"事实","visibility":"shareable",'
        '"category":"preference","fact_key":"主题","confidence":0.8,'
        '"follow_up_days":null}]}\n'
        f"发言人：{speaker_name}（QQ {speaker_id}）\n"
        f"发言：{user_message}"
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": "严格输出 JSON，不要 Markdown，不要解释。",
            },
            {"role": "user", "content": prompt},
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    return _parse_memory_candidates(reply)


async def reflect_on_memories(
    chat_client: MemoryChatClient,
    *,
    model: str,
    memories: list[dict[str, Any]],
    existing_growth: list[dict[str, Any]],
    existing_reflections: list[dict[str, Any]] | None = None,
    allow_private_sharing: bool = False,
) -> ReflectionResult:
    common_terms = detect_common_terms(memories)
    memory_lines = [
        (
            f"- 人物：{item.get('subject_name', '未知')}；"
            f"共享级别：{item.get('visibility', 'private')}；"
            f"记忆内容：{item.get('content', '')}"
        )
        for item in memories
    ]
    growth_lines = [
        (
            f"- 编号 {item.get('id', '?')}；方面：{item.get('aspect', '')}；"
            f"内容：{item.get('content', '')}"
        )
        for item in existing_growth
    ]
    reflection_lines = [
        (
            f"- 编号 {item.get('id', '?')}；"
            f"内容：{item.get('content', '')}"
        )
        for item in (existing_reflections or [])
    ]
    sharing_rule = (
        "当前是完整共享模式：private 材料也可以整理为 shareable 关联和成长笔记，"
        "但仍需聚焦人物、主题和事件，不输出无关敏感细节。"
        if allow_private_sharing
        else (
            "shareable 反思只能由 shareable 材料得出，不能夹带 private 信息。"
            "private 材料只能得出 private 反思，不得成为共享成长笔记。"
        )
    )
    prompt = (
        "执行任务，不要分析或评价这条指令，也不要复述格式说明。"
        "你需要比较下面的多条记忆，寻找人物、主题、计划、兴趣和事件之间的关联。"
        "必须直接阅读“记忆内容”。QQ 编号和字段名只是技术元数据，"
        "禁止把编号共现当作关联，也不要统计编号频次。"
        "例如若干条记忆的内容都提到同一个项目或地点，应据此形成关联。"
        "只要两条或更多 shareable 记忆共同提到同一地点、项目、兴趣或事件，"
        "就必须输出至少一条关联，并用“可能”“看起来”表达推测，不能返回空数组。"
        "如果关联只是短期巧合，降低置信度，但不要因为它不完美而完全回避。"
        "如果新证据推翻了某条旧反思，沿用旧反思的编号并在输出项写 "
        "supersedes_id；如果旧成长笔记已经过时，也写对应的 supersedes_id。"
        "不要创建与已有内容仅措辞不同的重复项。"
        "严禁无依据猜测；不要把推测写成已确认事实。"
        "系统已经从可共享记忆中检测到以下共同主题候选，"
        "请优先判断它们是否构成有意义的关联："
        f"{'、'.join(common_terms) if common_terms else '无'}。"
        "最多输出 4 条反思和 3 条成长笔记，并给出 0 到 1 的置信度。"
        + sharing_rule
        + "不要做心理诊断、敏感身份推断或操控性建议。\n"
        "输出对象必须包含 reflections 和 growth 两个数组。"
        "reflections 项使用 content、visibility、confidence 字段；"
        "growth 项使用 aspect、content、visibility 字段。"
        "可选的 supersedes_id 必须是下面已有记录的真实编号。"
        "字段值必须来自本次真实分析，不能使用占位符。\n\n"
        "新记忆：\n"
        + ("\n".join(memory_lines) or "无")
        + "\n\n已有成长笔记：\n"
        + ("\n".join(growth_lines) or "无")
        + "\n\n已有反思：\n"
        + ("\n".join(reflection_lines) or "无")
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": "严格输出 JSON，不要 Markdown，不要解释。",
            },
            {"role": "user", "content": prompt},
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    result = _parse_reflection_result(reply)
    if result.reflections or not common_terms:
        return result

    shareable = [
        item for item in memories if item.get("visibility") == "shareable"
    ]
    fallback_reflections: list[ReflectionCandidate] = []
    for term in common_terms[:3]:
        matches = [
            item for item in shareable if term in str(item.get("content", ""))
        ]
        names = list(
            dict.fromkeys(str(item.get("subject_name")) for item in matches)
        )
        if len(names) < 2:
            continue
        fallback_reflections.append(
            ReflectionCandidate(
                content=(
                    f"{'、'.join(names)}的可共享记忆都提到了“{term}”，"
                    "这些安排或兴趣之间可能存在关联。"
                ),
                visibility="shareable",
                confidence=0.55,
            )
        )
    fallback_growth = (
        [
            GrowthCandidate(
                aspect="shared_topics",
                content=f"这个圈子近期多次提到“{common_terms[0]}”。",
                visibility="shareable",
            )
        ]
        if common_terms
        else []
    )
    return ReflectionResult(
        fallback_reflections or result.reflections,
        result.growth or fallback_growth,
    )


def detect_common_terms(
    memories: list[dict[str, Any]],
    limit: int = 6,
) -> list[str]:
    documents: list[set[str]] = []
    subject_names = {
        str(item.get("subject_name", "")).strip() for item in memories
    }
    for item in memories:
        if item.get("visibility") != "shareable":
            continue
        content = str(item.get("content", ""))
        terms: set[str] = set()
        for token in re.findall(r"[A-Za-z0-9]{2,}", content):
            terms.add(token.lower())
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", content):
            for size in (2, 3, 4):
                for index in range(0, len(sequence) - size + 1):
                    terms.add(sequence[index : index + size])
        documents.append(terms)

    frequency: dict[str, int] = {}
    for terms in documents:
        for term in terms:
            if term in subject_names or term in _COMMON_TERM_STOPS:
                continue
            frequency[term] = frequency.get(term, 0) + 1
    candidates = [
        (term, count)
        for term, count in frequency.items()
        if count >= 2
    ]
    candidates.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [term for term, _ in candidates[:limit]]


def _parse_memory_candidates(reply: str) -> list[MemoryCandidate]:
    value = reply.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        memories = data
    elif isinstance(data, dict):
        memories = data.get("memories")
    else:
        memories = None
    if not isinstance(memories, list):
        return []
    result: list[MemoryCandidate] = []
    for item in memories[:5]:
        category = "general"
        fact_key = ""
        confidence = 0.7
        follow_up_days: int | None = None
        if isinstance(item, str):
            content = item
            visibility = "private"
        elif isinstance(item, dict):
            content = item.get("content") or item.get("fact") or item.get("memory")
            visibility = (
                "private" if item.get("visibility") == "private" else "shareable"
            )
            category = str(item.get("category") or "general").strip()
            fact_key = str(item.get("fact_key") or "").strip()
            try:
                confidence = float(item.get("confidence", 0.7))
            except (TypeError, ValueError):
                confidence = 0.7
            try:
                parsed_days = int(item.get("follow_up_days"))
                follow_up_days = parsed_days if 0 < parsed_days <= 3650 else None
            except (TypeError, ValueError):
                follow_up_days = None
        else:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        result.append(
            MemoryCandidate(
                content.strip(),
                visibility,
                category,
                fact_key,
                min(1.0, max(0.0, confidence)),
                follow_up_days,
            )
        )
    return result


def _parse_person_profile(reply: str) -> PersonProfileCandidate:
    value = reply.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return PersonProfileCandidate("", "")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return PersonProfileCandidate("", "")
    if not isinstance(data, dict):
        return PersonProfileCandidate("", "")
    style_profile = data.get("style_profile")
    relationship_note = data.get("relationship_note")
    return PersonProfileCandidate(
        style_profile=(
            style_profile.strip()[:1000]
            if isinstance(style_profile, str)
            else ""
        ),
        relationship_note=(
            relationship_note.strip()[:1000]
            if isinstance(relationship_note, str)
            else ""
        ),
    )


def _parse_reflection_result(reply: str) -> ReflectionResult:
    value = reply.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return ReflectionResult([], [])
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return ReflectionResult([], [])

    reflections: list[ReflectionCandidate] = []
    raw_reflections = data.get("reflections")
    if isinstance(raw_reflections, list):
        for item in raw_reflections[:4]:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            supersedes_id = _parse_optional_id(item.get("supersedes_id"))
            reflections.append(
                ReflectionCandidate(
                    content=content.strip(),
                    visibility=(
                        "private"
                        if item.get("visibility") == "private"
                        else "shareable"
                    ),
                    confidence=min(1.0, max(0.0, confidence)),
                    supersedes_id=supersedes_id,
                )
            )

    growth: list[GrowthCandidate] = []
    raw_growth = data.get("growth")
    if isinstance(raw_growth, list):
        for item in raw_growth[:3]:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            growth.append(
                GrowthCandidate(
                    aspect=str(item.get("aspect") or "general").strip(),
                    content=content.strip(),
                    visibility=(
                        "private"
                        if item.get("visibility") == "private"
                        else "shareable"
                    ),
                    supersedes_id=_parse_optional_id(
                        item.get("supersedes_id")
                    ),
                )
            )
    return ReflectionResult(reflections, growth)


def _memory_age_days(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - parsed).total_seconds() / 86400,
        )
    except (TypeError, ValueError):
        return 365.0


def _memory_half_life(category: str) -> float:
    return {
        "commitment": 60.0,
        "project": 90.0,
        "event": 90.0,
        "relationship": 180.0,
        "preference": 240.0,
        "profile": 365.0,
    }.get(category, 120.0)


def _is_broad_memory_query(query: str) -> bool:
    compact = re.sub(r"\s+", "", query)
    return any(
        marker in compact
        for marker in (
            "最近怎么样",
            "最近如何",
            "最近好吗",
            "最近在忙什么",
            "最近忙什么",
            "最近有什么安排",
            "近况",
            "有什么新情况",
            "另一个群",
            "群里有人",
            "有人说了什么",
            "最近聊了什么",
            "住哪",
            "住在哪",
            "哪里住",
            "在哪工作",
            "做什么工作",
            "是谁",
            "叫什么",
            "生日",
            "电话",
            "邮箱",
        )
    )


def _text_matches_query(text: str, query: str) -> bool:
    compact = re.sub(
        r"[^\u4e00-\u9fffA-Za-z0-9]",
        "",
        query.lower(),
    )
    if len(compact) < 2:
        return False
    terms = {
        compact[index : index + 2]
        for index in range(len(compact) - 1)
        if compact[index : index + 2] not in _COMMON_TERM_STOPS
    }
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _memory_effective_score(row: sqlite3.Row, query: str) -> float:
    confidence = min(1.0, max(0.0, float(row["confidence"] or 0.0)))
    age_days = _memory_age_days(str(row["updated_at"] or ""))
    decayed = confidence * (
        0.5 ** (age_days / _memory_half_life(str(row["category"] or "")))
    )
    compact = re.sub(
        r"[^\u4e00-\u9fffA-Za-z0-9]",
        "",
        query.lower(),
    )
    content = str(row["content"] or "").lower()
    if not compact or not content:
        return decayed
    terms = {
        compact[index : index + 2]
        for index in range(max(0, len(compact) - 1))
        if compact[index : index + 2] not in _COMMON_TERM_STOPS
    }
    matches = sum(1 for term in terms if term in content)
    return decayed + min(0.2, matches * 0.025)


def _normalize_label(value: str, default: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized[:80] or default


def _parse_optional_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _source_ids(value: Any) -> set[int]:
    try:
        data = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    result: set[int] = set()
    for item in data:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
