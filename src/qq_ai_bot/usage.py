from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .llm import ChatClient


class UsageStore:
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
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    input_chars INTEGER NOT NULL,
                    output_chars INTEGER NOT NULL,
                    approx_input_tokens INTEGER NOT NULL,
                    approx_output_tokens INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_model_usage_created
                ON model_usage (created_at DESC)
                """
            )

    def record(
        self,
        *,
        operation: str,
        model: str,
        duration_ms: float,
        input_chars: int,
        output_chars: int,
        success: bool,
        error: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_usage
                    (
                        created_at, operation, model, duration_ms,
                        input_chars, output_chars, approx_input_tokens,
                        approx_output_tokens, success, error
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    operation[:80] or "chat",
                    model[:200],
                    max(0.0, duration_ms),
                    input_chars,
                    output_chars,
                    estimate_tokens(input_chars),
                    estimate_tokens(output_chars),
                    1 if success else 0,
                    error[:300],
                ),
            )

    def summary(
        self,
        *,
        input_price_per_million: float = 0.0,
        output_price_per_million: float = 0.0,
        recent_limit: int = 40,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(success = 0), 0) AS errors,
                       COALESCE(AVG(duration_ms), 0) AS avg_ms,
                       COALESCE(MAX(duration_ms), 0) AS max_ms,
                       COALESCE(SUM(approx_input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(approx_output_tokens), 0) AS output_tokens
                FROM model_usage
                """
            ).fetchone()
            by_model = connection.execute(
                """
                SELECT model, COUNT(*) AS calls,
                       COALESCE(SUM(success = 0), 0) AS errors,
                       COALESCE(AVG(duration_ms), 0) AS avg_ms,
                       COALESCE(SUM(approx_input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(approx_output_tokens), 0) AS output_tokens
                FROM model_usage
                GROUP BY model
                ORDER BY calls DESC, model ASC
                """
            ).fetchall()
            by_operation = connection.execute(
                """
                SELECT operation, COUNT(*) AS calls,
                       COALESCE(AVG(duration_ms), 0) AS avg_ms,
                       COALESCE(SUM(success = 0), 0) AS errors
                FROM model_usage
                GROUP BY operation
                ORDER BY calls DESC
                """
            ).fetchall()
            recent = connection.execute(
                """
                SELECT created_at, operation, model, duration_ms,
                       approx_input_tokens, approx_output_tokens,
                       success, error
                FROM model_usage
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, recent_limit),),
            ).fetchall()
        data = dict(totals) if totals else {}
        input_tokens = int(data.get("input_tokens") or 0)
        output_tokens = int(data.get("output_tokens") or 0)
        estimated_cost = (
            input_tokens / 1_000_000 * max(0.0, input_price_per_million)
            + output_tokens / 1_000_000 * max(0.0, output_price_per_million)
        )
        model_rows: list[dict[str, Any]] = []
        for row in by_model:
            item = dict(row)
            calls = int(item.get("calls") or 0)
            errors = int(item.get("errors") or 0)
            item["success_rate"] = (
                (calls - errors) / calls if calls else 0.0
            )
            item["estimated_cost"] = round(
                int(item.get("input_tokens") or 0)
                / 1_000_000
                * max(0.0, input_price_per_million)
                + int(item.get("output_tokens") or 0)
                / 1_000_000
                * max(0.0, output_price_per_million),
                6,
            )
            model_rows.append(item)
        return {
            "totals": {
                **data,
                "estimated_cost": round(estimated_cost, 6),
                "pricing_configured": bool(
                    input_price_per_million or output_price_per_million
                ),
            },
            "by_model": model_rows,
            "by_operation": [dict(row) for row in by_operation],
            "recent": [dict(row) for row in recent],
        }


class MeteredChatClient:
    def __init__(self, client: ChatClient, usage: UsageStore) -> None:
        self.client = client
        self.usage = usage

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> str:
        operation = _operation_for(messages)
        selected_model = model or str(getattr(self.client, "model", "unknown"))
        input_chars = len(
            json.dumps(list(messages), ensure_ascii=False, default=str)
        )
        payload = dict(extra_payload or {})
        payload.pop("_allow_model_override", None)
        started = time.perf_counter()
        try:
            reply = await self.client.chat(
                messages,
                model=model,
                extra_payload=payload or None,
            )
        except Exception as exc:
            self.usage.record(
                operation=operation,
                model=selected_model,
                duration_ms=(time.perf_counter() - started) * 1000,
                input_chars=input_chars,
                output_chars=0,
                success=False,
                error=str(exc),
            )
            raise
        self.usage.record(
            operation=operation,
            model=selected_model,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_chars=input_chars,
            output_chars=len(reply),
            success=True,
        )
        return reply

    async def close(self) -> None:
        await self.client.close()


def estimate_tokens(characters: int) -> int:
    # Mixed Chinese/English text usually lands around two characters per token.
    return max(1, math.ceil(max(0, characters) / 2))


def _operation_for(messages: Sequence[dict[str, Any]]) -> str:
    text = " ".join(
        str(item.get("content", ""))
        for item in messages[:2]
        if isinstance(item, dict)
    )
    lowered = text.lower()
    if "judge" in lowered or "判断 qq 群" in text:
        return "group_policy"
    if "表情" in text and "sticker_id" in lowered:
        return "sticker_choice"
    if "长期记忆整理器" in text:
        return "memory_extract"
    if "互动画像" in text:
        return "profile"
    if "多条记忆" in text:
        return "reflection"
    if "代码文件生成器" in text or "代码修复器" in text:
        return "code_generation"
    if "文档" in text or "分段摘要" in text:
        return "document"
    if "ocr" in lowered or "转写" in text:
        return "ocr"
    return "chat"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
