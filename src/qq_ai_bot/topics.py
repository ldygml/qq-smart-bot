from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .llm import ChatClient


@dataclass(frozen=True)
class TopicCandidate:
    title: str
    summary: str
    status: str
    pending_question: str = ""
    next_action: str = ""
    follow_up_days: int | None = None


class TopicStore:
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
                CREATE TABLE IF NOT EXISTS conversation_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    pending_question TEXT NOT NULL DEFAULT '',
                    next_action TEXT NOT NULL DEFAULT '',
                    follow_up_at TEXT,
                    source_user_id INTEGER,
                    source_group_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_key, title)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_topics_scope_status
                ON conversation_topics (scope_key, status, updated_at DESC)
                """
            )

    def upsert(
        self,
        *,
        scope_key: str,
        candidate: TopicCandidate,
        user_id: int,
        group_id: int | None,
    ) -> int:
        now = datetime.now(timezone.utc)
        follow_up_at = (
            (
                now + timedelta(days=max(1, int(candidate.follow_up_days)))
            ).isoformat()
            if candidate.follow_up_days
            else None
        )
        status = (
            candidate.status
            if candidate.status in {"active", "waiting", "resolved"}
            else "active"
        )
        title = candidate.title.strip()[:160] or "当前话题"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_topics
                    (
                        scope_key, title, summary, status, pending_question,
                        next_action, follow_up_at, source_user_id,
                        source_group_id, created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key, title) DO UPDATE SET
                    summary = excluded.summary,
                    status = excluded.status,
                    pending_question = excluded.pending_question,
                    next_action = excluded.next_action,
                    follow_up_at = COALESCE(
                        excluded.follow_up_at,
                        conversation_topics.follow_up_at
                    ),
                    source_user_id = excluded.source_user_id,
                    source_group_id = excluded.source_group_id,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    title,
                    candidate.summary.strip()[:1000],
                    status,
                    candidate.pending_question.strip()[:500],
                    candidate.next_action.strip()[:500],
                    follow_up_at,
                    user_id,
                    group_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM conversation_topics
                WHERE scope_key = ? AND title = ?
                """,
                (scope_key, title),
            ).fetchone()
        return int(row["id"]) if row else 0

    def active(
        self,
        scope_key: str,
        *,
        limit: int = 6,
        mark_stale_after_hours: int = 12,
    ) -> list[dict[str, Any]]:
        self.mark_stale(mark_stale_after_hours)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, scope_key, title, summary, status,
                       pending_question, next_action, follow_up_at,
                       created_at, updated_at
                FROM conversation_topics
                WHERE scope_key = ?
                  AND status IN ('active', 'waiting')
                ORDER BY
                    CASE status WHEN 'waiting' THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT ?
                """,
                (scope_key, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def context(self, scope_key: str, *, limit: int = 5) -> str:
        items = self.active(scope_key, limit=limit)
        if not items:
            return ""
        lines = ["当前会话仍在进行的话题（只在相关时自然承接）："]
        for item in items:
            detail = f"- {item['title']}：{item['summary']}"
            if item["pending_question"]:
                detail += f"；等待对方回应：{item['pending_question']}"
            if item["next_action"]:
                detail += f"；下一步：{item['next_action']}"
            lines.append(detail)
        return "\n".join(lines)

    def mark_stale(self, hours: int) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
        ).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_topics
                SET status = 'stale', updated_at = ?
                WHERE status IN ('active', 'waiting') AND updated_at < ?
                """,
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            return cursor.rowcount

    def resolve(self, topic_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_topics
                SET status = 'resolved', updated_at = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), topic_id),
            )
            return cursor.rowcount > 0


async def infer_topics(
    chat_client: ChatClient,
    *,
    model: str,
    user_message: str,
    assistant_reply: str,
    existing_topics: list[dict[str, Any]],
) -> list[TopicCandidate]:
    existing = "\n".join(
        f"- {item['title']}（{item['status']}）：{item['summary']}"
        for item in existing_topics
    )
    prompt = (
        "整理这一段 QQ 对话中的话题状态。只记录对下一轮继续聊天有用的信息。"
        "status 只能是 active（正在讨论）、waiting（等对方回应）或 resolved（已经解决）。"
        "pending_question 只在机器人确实问了对方问题时填写。"
        "next_action 写接下来自然可做的事，没有就留空。"
        "follow_up_days 只在对方明确承诺或存在明确时间节点时填写整数，否则 null。"
        "不要记录寒暄、情绪表演、无后续价值的短反应。最多返回三条。\n"
        '只返回 JSON：{"topics":[{"title":"稳定短标题",'
        '"summary":"当前进展","status":"active","pending_question":"",'
        '"next_action":"","follow_up_days":null}]}\n\n'
        f"已有话题：\n{existing or '无'}\n\n"
        f"用户：{user_message[:2500]}\n"
        f"机器人：{assistant_reply[:3500]}"
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
    data = _parse_json(reply)
    raw_topics = data.get("topics")
    if not isinstance(raw_topics, list):
        return []
    candidates: list[TopicCandidate] = []
    for item in raw_topics[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:160]
        summary = str(item.get("summary") or "").strip()[:1000]
        if not title or not summary:
            continue
        follow_up_days: int | None = None
        try:
            if item.get("follow_up_days") is not None:
                follow_up_days = max(
                    1,
                    min(365, int(item["follow_up_days"])),
                )
        except (TypeError, ValueError):
            follow_up_days = None
        candidates.append(
            TopicCandidate(
                title=title,
                summary=summary,
                status=str(item.get("status") or "active").strip().lower(),
                pending_question=str(
                    item.get("pending_question") or ""
                ).strip()[:500],
                next_action=str(item.get("next_action") or "").strip()[:500],
                follow_up_days=follow_up_days,
            )
        )
    return candidates


def _parse_json(reply: str) -> dict[str, Any]:
    value = reply.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}
