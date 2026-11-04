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
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shared_growth (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aspect TEXT NOT NULL,
                    content TEXT NOT NULL,
                    visibility TEXT NOT NULL
                        CHECK (visibility IN ('shareable', 'private')),
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

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
                    _utc_now(),
                    user_id,
                ),
            )
            return cursor.rowcount > 0

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
                      AND (visibility = 'shareable' OR ? = 1)
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 8
                    """,
                    [*mentioned_ids, 1 if include_private else 0],
                ).fetchall()
                for row in mentioned_rows:
                    selected[int(row["id"])] = row

            recent_rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility
                FROM shared_memories
                WHERE visibility = 'shareable' OR ? = 1
                ORDER BY updated_at DESC, id DESC
                LIMIT 6
                """,
                (1 if include_private else 0,),
            ).fetchall()
            for row in recent_rows:
                selected[int(row["id"])] = row

        rows = list(selected.values())[: max(1, limit)]
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
            lines.append(
                f"- {subject_name}（{row['visibility']}）：{row['content']}"
            )
        reflections = self.reflection_summary(limit=4)
        growth = self.growth_summary(limit=4)
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

    def memories_since(self, memory_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, subject_user_id, subject_name, content, visibility
                FROM shared_memories
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (memory_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

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
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shared_episodes
                    (
                        conversation_id, message_type, group_id,
                        speaker_user_id, speaker_name, user_content,
                        assistant_content, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            return int(cursor.lastrowid)

    def add_reflection(
        self,
        *,
        content: str,
        visibility: str,
        confidence: float,
    ) -> int | None:
        fact = content.strip()
        if not fact:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        score = min(1.0, max(0.0, float(confidence)))
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM shared_reflections WHERE content = ? LIMIT 1",
                (fact,),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_reflections
                    SET confidence = MAX(confidence, ?), visibility = ?
                    WHERE id = ?
                    """,
                    (score, normalized_visibility, int(existing["id"])),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO shared_reflections
                    (content, visibility, confidence, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (fact, normalized_visibility, score, _utc_now()),
            )
            return int(cursor.lastrowid)

    def add_growth(
        self,
        *,
        aspect: str,
        content: str,
        visibility: str,
    ) -> int | None:
        value = content.strip()
        if not value:
            return None
        normalized_visibility = (
            "private" if visibility == "private" else "shareable"
        )
        normalized_aspect = aspect.strip() or "general"
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM shared_growth
                WHERE aspect = ? AND content = ?
                LIMIT 1
                """,
                (normalized_aspect, value),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE shared_growth
                    SET evidence_count = evidence_count + 1,
                        visibility = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (normalized_visibility, now, int(existing["id"])),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO shared_growth
                    (aspect, content, visibility, evidence_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (normalized_aspect, value, normalized_visibility, now),
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
                SELECT content, confidence, created_at
                FROM shared_reflections
                WHERE visibility = 'shareable' OR ? = 1
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
                SELECT aspect, content, evidence_count, updated_at
                FROM shared_growth
                WHERE visibility = 'shareable' OR ? = 1
                ORDER BY evidence_count DESC, updated_at DESC
                LIMIT ?
                """,
                (1 if include_private else 0, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

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


@dataclass(frozen=True)
class PersonProfileCandidate:
    style_profile: str
    relationship_note: str


@dataclass(frozen=True)
class ReflectionCandidate:
    content: str
    visibility: str
    confidence: float


@dataclass(frozen=True)
class GrowthCandidate:
    aspect: str
    content: str
    visibility: str


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
        f"- {item.get('content', '')}" for item in existing_growth
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
        "字段值必须来自本次真实分析，不能使用占位符。\n\n"
        "新记忆：\n"
        + ("\n".join(memory_lines) or "无")
        + "\n\n已有成长笔记：\n"
        + ("\n".join(growth_lines) or "无")
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
    for item in memories[:3]:
        if isinstance(item, str):
            content = item
            visibility = "private"
        elif isinstance(item, dict):
            content = item.get("content") or item.get("fact") or item.get("memory")
            visibility = (
                "private" if item.get("visibility") == "private" else "shareable"
            )
        else:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        result.append(MemoryCandidate(content.strip(), visibility))
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
            reflections.append(
                ReflectionCandidate(
                    content=content.strip(),
                    visibility=(
                        "private"
                        if item.get("visibility") == "private"
                        else "shareable"
                    ),
                    confidence=min(1.0, max(0.0, confidence)),
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
                )
            )
    return ReflectionResult(reflections, growth)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
