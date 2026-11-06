from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MessageTraceStore:
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
                CREATE TABLE IF NOT EXISTS message_traces (
                    trace_id TEXT PRIMARY KEY,
                    message_id TEXT,
                    conversation_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    user_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    decision TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    latency_ms REAL,
                    memory_count INTEGER NOT NULL DEFAULT 0,
                    knowledge_count INTEGER NOT NULL DEFAULT 0,
                    reply TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_traces_created
                ON message_traces (created_at DESC, trace_id)
                """
            )

    def start(
        self,
        *,
        message_id: int | str | None,
        conversation_id: str,
        message_type: str,
        user_id: int,
        group_id: int | None,
        user_text: str,
    ) -> "MessageTrace":
        trace_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO message_traces
                    (
                        trace_id, message_id, conversation_id,
                        message_type, user_id, group_id, user_text,
                        created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    str(message_id or ""),
                    conversation_id,
                    message_type,
                    user_id,
                    group_id,
                    user_text[:4000],
                    _utc_now(),
                ),
            )
            connection.execute(
                """
                DELETE FROM message_traces
                WHERE trace_id NOT IN (
                    SELECT trace_id FROM message_traces
                    ORDER BY created_at DESC
                    LIMIT 3000
                )
                """
            )
        return MessageTrace(self, trace_id)

    def finish(
        self,
        trace_id: str,
        *,
        status: str,
        decision: str,
        reason: str,
        model: str,
        latency_ms: float,
        memory_count: int,
        knowledge_count: int,
        reply: str,
        details: dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE message_traces
                SET status = ?, decision = ?, reason = ?, model = ?,
                    latency_ms = ?, memory_count = ?,
                    knowledge_count = ?, reply = ?, details = ?,
                    finished_at = ?
                WHERE trace_id = ?
                """,
                (
                    status,
                    decision[:200],
                    reason[:1000],
                    model[:200],
                    round(max(0.0, float(latency_ms)), 2),
                    max(0, int(memory_count)),
                    max(0, int(knowledge_count)),
                    reply[:8000],
                    json.dumps(details, ensure_ascii=False)[:20000],
                    _utc_now(),
                    trace_id,
                ),
            )

    def list_recent(
        self,
        *,
        limit: int = 100,
        skipped_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = "WHERE status = 'skipped'" if skipped_only else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM message_traces
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, min(500, limit)),),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            try:
                item["details"] = json.loads(str(item["details"] or "{}"))
            except json.JSONDecodeError:
                item["details"] = {}
        return result

    def get(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM message_traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["details"] = json.loads(str(item["details"] or "{}"))
        except json.JSONDecodeError:
            item["details"] = {}
        return item


class MessageTrace:
    def __init__(self, store: MessageTraceStore, trace_id: str) -> None:
        self.store = store
        self.trace_id = trace_id
        self.started_at = time.perf_counter()
        self.status = "processing"
        self.decision = ""
        self.reason = ""
        self.model = ""
        self.memory_count = 0
        self.knowledge_count = 0
        self.reply = ""
        self.details: dict[str, Any] = {}
        self.finished = False

    def stage(self, stage_name: str, **values: Any) -> None:
        self.details[stage_name] = values

    def skip(self, reason: str, *, decision: str = "silent") -> None:
        if self.finished:
            return
        self.status = "skipped"
        self.decision = decision
        self.reason = reason
        self.finish()

    def replied(
        self,
        *,
        reply: str,
        model: str,
        memory_count: int = 0,
        knowledge_count: int = 0,
    ) -> None:
        if self.finished:
            if self.status == "replied":
                self.decision = "reply"
                self.model = model
                self.reply = reply
                self.memory_count = memory_count
                self.knowledge_count = knowledge_count
                self.store.finish(
                    self.trace_id,
                    status=self.status,
                    decision=self.decision,
                    reason=self.reason,
                    model=self.model,
                    latency_ms=(
                        time.perf_counter() - self.started_at
                    )
                    * 1000,
                    memory_count=self.memory_count,
                    knowledge_count=self.knowledge_count,
                    reply=self.reply,
                    details=self.details,
                )
            return
        self.status = "replied"
        self.decision = "reply"
        self.model = model
        self.reply = reply
        self.memory_count = memory_count
        self.knowledge_count = knowledge_count
        self.finish()

    def error(self, reason: str) -> None:
        if self.finished:
            return
        self.status = "error"
        self.decision = "error"
        self.reason = reason
        self.finish()

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.status == "processing":
            self.status = "no_reply"
            self.decision = "silent"
            self.reason = self.reason or "处理结束但未产生可发送回复"
        self.store.finish(
            self.trace_id,
            status=self.status,
            decision=self.decision,
            reason=self.reason,
            model=self.model,
            latency_ms=(time.perf_counter() - self.started_at) * 1000,
            memory_count=self.memory_count,
            knowledge_count=self.knowledge_count,
            reply=self.reply,
            details=self.details,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
