from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


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
                    last_seen TEXT NOT NULL
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
                """
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
    ) -> int | None:
        fact = content.strip()
        if not fact:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM shared_memories
                WHERE subject_user_id = ? AND content = ?
                LIMIT 1
                """,
                (subject_user_id, fact),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_memories
                    SET visibility = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (normalized_visibility, now, int(existing["id"])),
                )
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO shared_memories
                    (
                        subject_user_id, subject_name, content, visibility,
                        source_conversation, created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_user_id,
                    subject_name.strip() or str(subject_user_id),
                    fact,
                    normalized_visibility,
                    source_conversation,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

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

    def context_for(
        self,
        *,
        current_user_id: int,
        current_user_name: str,
        query: str,
        limit: int = 14,
    ) -> str:
        mentioned = self.mentioned_people(query)
        selected: dict[int, sqlite3.Row] = {}
        with self._lock, self._connect() as connection:
            own_rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility
                FROM shared_memories
                WHERE subject_user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 6
                """,
                (current_user_id,),
            ).fetchall()
            for row in own_rows:
                selected[int(row["id"])] = row

            if mentioned:
                placeholders = ",".join("?" for _ in mentioned)
                mentioned_ids = [user_id for user_id, _ in mentioned]
                mentioned_rows = connection.execute(
                    f"""
                    SELECT id, subject_user_id, subject_name, content, visibility
                    FROM shared_memories
                    WHERE subject_user_id IN ({placeholders})
                      AND visibility = 'shareable'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 8
                    """,
                    mentioned_ids,
                ).fetchall()
                for row in mentioned_rows:
                    selected[int(row["id"])] = row

            recent_rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility
                FROM shared_memories
                WHERE visibility = 'shareable'
                ORDER BY updated_at DESC, id DESC
                LIMIT 6
                """
            ).fetchall()
            for row in recent_rows:
                selected[int(row["id"])] = row

        rows = list(selected.values())[: max(1, limit)]
        if not rows:
            return ""
        lines = [
            "跨会话长期共享记忆：",
            f"当前联系人：{current_user_name}（QQ {current_user_id}）",
            (
                "只自然使用与话题相关的信息。private 记忆只属于当事人，"
                "不能告诉其他联系人，也不要复述私密原话。"
            ),
        ]
        for row in rows:
            subject_name = str(row["subject_name"])
            if int(row["subject_user_id"]) == current_user_id:
                subject_name = f"当前联系人 {subject_name}"
            lines.append(
                f"- {subject_name}（{row['visibility']}）：{row['content']}"
            )
        return "\n".join(lines)

    def list_memories(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, visibility, updated_at
                FROM shared_memories
                WHERE subject_user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 50
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self, user_id: int, memory_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM shared_memories
                WHERE id = ? AND subject_user_id = ?
                """,
                (memory_id, user_id),
            )
            return cursor.rowcount > 0

    def forget_user(self, user_id: int) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM shared_memories WHERE subject_user_id = ?",
                (user_id,),
            )
            connection.execute(
                "DELETE FROM shared_people WHERE user_id = ?",
                (user_id,),
            )
            return cursor.rowcount


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
        "最多三条，每条独立、简短、不得推测。"
        "偏好、长期计划、关系、重要事件可记为 shareable；"
        "健康、财务、秘密、私人冲突、敏感身份信息记为 private。"
        "证据不足时返回空数组。\n"
        '只返回 JSON：{"memories":[{"content":"事实","visibility":"shareable"}]}\n'
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
        extra_payload={"reasoning_effort": "none"},
    )
    return _parse_memory_candidates(reply)


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
    memories = data.get("memories")
    if not isinstance(memories, list):
        return []
    result: list[MemoryCandidate] = []
    for item in memories[:3]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        visibility = (
            "private" if item.get("visibility") == "private" else "shareable"
        )
        result.append(MemoryCandidate(content.strip(), visibility))
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
