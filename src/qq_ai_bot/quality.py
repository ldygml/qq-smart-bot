from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .llm import ChatClient


class QualityStore:
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
                CREATE TABLE IF NOT EXISTS response_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    issues TEXT NOT NULL DEFAULT '[]',
                    score REAL,
                    review_notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_response_quality_created
                ON response_quality (id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_response_quality_conversation
                ON response_quality (conversation_id, id DESC)
                """
            )

    def record(
        self,
        *,
        conversation_id: str,
        user_id: int,
        group_id: int | None,
        user_text: str,
        assistant_text: str,
        model: str,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO response_quality
                    (
                        conversation_id, user_id, group_id, user_text,
                        assistant_text, model, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    user_id,
                    group_id,
                    user_text[:4000],
                    assistant_text[:12000],
                    model[:200],
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def mark_follow_up(self, conversation_id: str, user_text: str) -> None:
        value = user_text.strip()
        correction = any(
            marker in value
            for marker in (
                "答非所问",
                "不是这个",
                "你错了",
                "重新",
                "我说的是",
                "没回答",
                "太长了",
                "别这么",
            )
        )
        status = "corrected" if correction else "follow_up"
        issues = ["user_corrected"] if correction else []
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM response_quality
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE response_quality
                SET status = ?, issues = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(issues, ensure_ascii=False),
                    _utc_now(),
                    int(row["id"]),
                ),
            )

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM response_quality
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["issues"] = json.loads(str(item["issues"]))
            except json.JSONDecodeError:
                item["issues"] = []
            result.append(item)
        return result

    def mark_review(
        self,
        quality_id: int,
        *,
        score: float,
        issues: list[str],
        notes: str,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE response_quality
                SET score = ?, issues = ?, review_notes = ?,
                    reviewed_at = ?
                WHERE id = ?
                """,
                (
                    min(1.0, max(0.0, float(score))),
                    json.dumps(issues, ensure_ascii=False),
                    notes[:1000],
                    _utc_now(),
                    quality_id,
                ),
            )
            return cursor.rowcount > 0

    def pending_review(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_recent(limit=200)
            if item["score"] is None
        ][: max(1, limit)]

    def mark_ignored_stale(self, *, minutes: int = 20) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=max(1, minutes))
        ).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE response_quality
                SET status = 'ignored', reviewed_at = ?
                WHERE status = 'new' AND created_at < ?
                """,
                (_utc_now(), cutoff),
            )
            return cursor.rowcount

    def stats(self) -> dict[str, Any]:
        items = self.list_recent(limit=500)
        reviewed = [item for item in items if item["score"] is not None]
        return {
            "total": len(items),
            "reviewed": len(reviewed),
            "corrected": sum(
                item["status"] == "corrected" for item in items
            ),
            "average_score": (
                sum(float(item["score"]) for item in reviewed)
                / len(reviewed)
                if reviewed
                else None
            ),
        }


async def evaluate_quality(
    chat_client: ChatClient,
    *,
    model: str,
    user_text: str,
    assistant_text: str,
    group_context: str = "",
) -> dict[str, Any]:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是 QQ 对话质量评估器。严格输出 JSON，不要解释。"
                    "评估是否答非所问、重复、过长、语气不自然、群聊乱插话、"
                    "事实依据不足。score 为 0 到 1。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"score":0到1,"issues":["问题标签"],'
                    '"notes":"一句改进建议"}。\n'
                    f"用户：{user_text[:2000]}\n"
                    f"机器人：{assistant_text[:4000]}\n"
                    f"场景：{group_context or '私聊'}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    try:
        value = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            reply.strip(),
            flags=re.I,
        )
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if not match:
            return {"score": 0.0, "issues": ["invalid_review"], "notes": ""}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"score": 0.0, "issues": ["invalid_review"], "notes": ""}
    issues = data.get("issues") if isinstance(data, dict) else []
    return {
        "score": float(data.get("score", 0.0)) if isinstance(data, dict) else 0.0,
        "issues": [
            str(item)[:80] for item in issues
        ][:12] if isinstance(issues, list) else [],
        "notes": str(data.get("notes", ""))[:1000]
        if isinstance(data, dict)
        else "",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
