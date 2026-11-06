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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_style_preferences (
                    scope_key TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    length_bias REAL NOT NULL DEFAULT 0,
                    directness_bias REAL NOT NULL DEFAULT 0,
                    humor_bias REAL NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reply_strategy_experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quality_id INTEGER,
                    scope_key TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    challenger_name TEXT NOT NULL,
                    baseline_reply TEXT NOT NULL,
                    challenger_reply TEXT NOT NULL,
                    winner TEXT NOT NULL,
                    baseline_score REAL,
                    challenger_score REAL,
                    observed_outcome TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            experiment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(reply_strategy_experiments)"
                ).fetchall()
            }
            if "observed_outcome" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE reply_strategy_experiments "
                    "ADD COLUMN observed_outcome TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reply_strategy_rollouts (
                    scope_key TEXT PRIMARY KEY,
                    challenger_name TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
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
                "太人机",
                "不像人",
                "太机械",
                "别像机器人",
                "太凶",
                "刻薄",
                "模板",
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
            connection.execute(
                """
                UPDATE reply_strategy_experiments
                SET observed_outcome = ?
                WHERE quality_id = ?
                """,
                (status, int(row["id"])),
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
            stale_ids = [
                int(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM response_quality
                    WHERE status = 'new' AND created_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            cursor = connection.execute(
                """
                UPDATE response_quality
                SET status = 'ignored', reviewed_at = ?
                WHERE status = 'new' AND created_at < ?
                """,
                (_utc_now(), cutoff),
            )
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    f"""
                    UPDATE reply_strategy_experiments
                    SET observed_outcome = 'ignored'
                    WHERE quality_id IN ({placeholders})
                    """,
                    stale_ids,
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

    def observe_style_request(
        self,
        *,
        user_id: int,
        group_id: int | None,
        text: str,
    ) -> bool:
        value = text.strip().lower()
        length_bias = 0.0
        directness_bias = 0.0
        humor_bias = 0.0
        if any(
            marker in value
            for marker in ("简短", "短点", "别写这么长", "太长", "少说点")
        ):
            length_bias = -1.0
        elif any(marker in value for marker in ("详细点", "展开说", "多说点")):
            length_bias = 1.0
        if any(
            marker in value
            for marker in (
                "温柔",
                "别凶",
                "这么凶",
                "好好说",
                "太mean",
                "太刻薄",
            )
        ):
            directness_bias = -1.0
        if any(
            marker in value
            for marker in ("正经说", "别开玩笑", "不开玩笑")
        ):
            humor_bias = -1.0
        elif any(marker in value for marker in ("可以开玩笑", "随便聊")):
            humor_bias = 1.0
        if not any((length_bias, directness_bias, humor_bias)):
            return False
        scope_key = reply_scope_key(user_id, group_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO contact_style_preferences
                    (
                        scope_key, user_id, group_id, length_bias,
                        directness_bias, humor_bias, evidence_count,
                        updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    length_bias = contact_style_preferences.length_bias
                        + excluded.length_bias,
                    directness_bias = contact_style_preferences.directness_bias
                        + excluded.directness_bias,
                    humor_bias = contact_style_preferences.humor_bias
                        + excluded.humor_bias,
                    evidence_count = contact_style_preferences.evidence_count + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    user_id,
                    group_id,
                    length_bias,
                    directness_bias,
                    humor_bias,
                    _utc_now(),
                ),
            )
        return True

    def contact_policy(
        self,
        *,
        user_id: int,
        group_id: int | None,
    ) -> dict[str, Any]:
        scope_key = reply_scope_key(user_id, group_id)
        items = [
            item
            for item in self.list_recent(limit=160)
            if item.get("user_id") == user_id
            and (
                item.get("group_id") == group_id
                if group_id is not None
                else item.get("group_id") is None
            )
        ][:30]
        issues = [
            str(issue)
            for item in items
            for issue in (item.get("issues") or [])
        ]
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contact_style_preferences WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        preference = dict(row) if row else {}
        short_penalty = sum(
            issue in {"too_long", "canned_tone", "format_overload"}
            or any(
                marker in issue
                for marker in ("太长", "模板", "格式", "清单")
            )
            for issue in issues
        )
        soft_penalty = sum(
            issue in {"tone_mismatch", "overdone_edge"}
            or any(
                marker in issue
                for marker in ("语气不自然", "刻薄", "冷嘲", "说教")
            )
            for issue in issues
        )
        length_bias = float(preference.get("length_bias", 0.0))
        directness_bias = float(preference.get("directness_bias", 0.0))
        humor_bias = float(preference.get("humor_bias", 0.0))
        directives: list[str] = []
        if length_bias < 0 or short_penalty >= 2:
            directives.append("对方更适合短、直接、少解释的回复。")
        elif length_bias > 0:
            directives.append("对方能接受更完整一点的解释，但仍不要写报告。")
        if directness_bias < 0 or soft_penalty >= 2:
            directives.append("降低攻击性，不要冷嘲、反问或用教训口吻。")
        if humor_bias < 0:
            directives.append("当前联系人要求正经沟通，少玩梗。")
        elif humor_bias > 0:
            directives.append("可以自然接梗，但不要为了搞笑偏离话题。")
        return {
            "scope_key": scope_key,
            "directives": directives,
            "evidence_count": len(items) + int(
                preference.get("evidence_count", 0)
            ),
            "length_bias": length_bias,
            "directness_bias": directness_bias,
            "humor_bias": humor_bias,
        }

    def record_experiment(
        self,
        *,
        quality_id: int | None,
        user_id: int,
        group_id: int | None,
        challenger_name: str,
        baseline_reply: str,
        challenger_reply: str,
        winner: str,
        baseline_score: float,
        challenger_score: float,
    ) -> int:
        scope_key = reply_scope_key(user_id, group_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reply_strategy_experiments
                    (
                        quality_id, scope_key, user_id, group_id,
                        challenger_name, baseline_reply,
                        challenger_reply, winner, baseline_score,
                        challenger_score, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quality_id,
                    scope_key,
                    user_id,
                    group_id,
                    challenger_name[:80],
                    baseline_reply[:12000],
                    challenger_reply[:12000],
                    winner,
                    baseline_score,
                    challenger_score,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM reply_strategy_rollouts
                WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
            wins = int(row["wins"]) if row else 0
            losses = int(row["losses"]) if row else 0
            samples = int(row["samples"]) if row else 0
            if winner == "challenger":
                wins += 1
            else:
                losses += 1
            samples += 1
            win_rate = wins / samples if samples else 0.0
            if samples >= 12 and win_rate >= 0.65:
                weight = 0.75
            elif samples >= 8 and win_rate >= 0.62:
                weight = 0.5
            elif samples >= 6 and win_rate >= 0.6:
                weight = 0.25
            else:
                weight = 0.0
            connection.execute(
                """
                INSERT INTO reply_strategy_rollouts
                    (
                        scope_key, challenger_name, weight, wins, losses,
                        samples, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    challenger_name = excluded.challenger_name,
                    weight = excluded.weight,
                    wins = excluded.wins,
                    losses = excluded.losses,
                    samples = excluded.samples,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    challenger_name[:80],
                    weight,
                    wins,
                    losses,
                    samples,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def adaptive_style(
        self,
        *,
        user_id: int,
        group_id: int | None,
    ) -> dict[str, Any] | None:
        scope_key = reply_scope_key(user_id, group_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reply_strategy_rollouts WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if not row or float(row["weight"]) <= 0:
            return None
        return dict(row)

    def adaptive_snapshot(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            rollout_rows = connection.execute(
                """
                SELECT * FROM reply_strategy_rollouts
                ORDER BY updated_at DESC
                """
            ).fetchall()
            experiment_rows = connection.execute(
                """
                SELECT * FROM reply_strategy_experiments
                ORDER BY id DESC
                LIMIT 80
                """
            ).fetchall()
            preference_rows = connection.execute(
                """
                SELECT * FROM contact_style_preferences
                ORDER BY updated_at DESC
                LIMIT 100
                """
            ).fetchall()
        return {
            "rollouts": [dict(row) for row in rollout_rows],
            "experiments": [dict(row) for row in experiment_rows],
            "preferences": [dict(row) for row in preference_rows],
        }

    def coaching_notes(
        self,
        *,
        group_id: int | None,
        limit: int = 5,
    ) -> list[str]:
        issue_guidance = {
            "answer_drift": "先直接回答当前问题，不要绕去别的话题。",
            "too_long": "缩短回复，删掉复述、总结和多余建议。",
            "repetition": "换一种表达，不要重复刚说过的结论或句式。",
            "tone_mismatch": "匹配对方当前情绪，不要突然说教或卖弄犀利。",
            "group_interjection": "群里没必要接的话就不接。",
            "canned_tone": "避免客服腔、总结腔和固定模板。",
            "overdone_edge": "正常聊天不要为了显得有性格而冷嘲或攻击。",
            "format_overload": "闲聊不要排版，不要列清单或小标题。",
            "user_corrected": "用户已经纠正过，优先修正而不是继续辩解。",
        }
        notes: list[str] = []
        for item in self.list_recent(limit=200):
            if group_id is None:
                if item.get("group_id") is not None:
                    continue
            elif int(item.get("group_id") or 0) != group_id:
                continue
            score = item.get("score")
            issues = item.get("issues") or []
            if (
                item.get("status") != "corrected"
                and not issues
                and (score is None or float(score) >= 0.8)
            ):
                continue
            note = str(item.get("review_notes") or "").strip()
            if not note:
                note = " ".join(
                    issue_guidance.get(str(issue), "")
                    for issue in issues
                ).strip()
            if note and note not in notes:
                notes.append(note[:240])
            if len(notes) >= max(1, limit):
                break
        return notes


def reply_scope_key(user_id: int, group_id: int | None) -> str:
    return (
        f"group:{group_id}:user:{user_id}"
        if group_id is not None
        else f"user:{user_id}"
    )


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
                    "事实依据不足，以及回复是否像客服模板、固定话术或为了显得有个性而"
                    "过度冷嘲。普通问候被怼、正常请求被教育、连续使用相似开场，"
                    "都应明显降低评分。issues 优先使用 answer_drift、too_long、"
                    "repetition、tone_mismatch、canned_tone、overdone_edge、"
                    "format_overload、group_interjection。score 为 0 到 1。"
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


async def compare_replies(
    chat_client: ChatClient,
    *,
    model: str,
    user_text: str,
    baseline: str,
    challenger: str,
    contact_context: str = "",
) -> dict[str, Any]:
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "比较两个 QQ 回复，判断哪一个更像正常熟人聊天。"
                    "更自然、贴合当前消息、少模板、少说教、没有过度攻击性的优先。"
                    "严格返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"winner":"baseline"或"challenger",'
                    '"baseline_score":0到1,"challenger_score":0到1,'
                    '"reason":"一句话"}。\n'
                    f"联系人上下文：{contact_context[:1200] or '无'}\n"
                    f"用户：{user_text[:2000]}\n"
                    f"A：{baseline[:4000]}\n"
                    f"B：{challenger[:4000]}"
                ),
            },
        ],
        model=model,
        extra_payload={
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
        },
    )
    data = _parse_json(reply)
    winner = str(data.get("winner") or "baseline").strip().lower()
    if winner not in {"baseline", "challenger"}:
        winner = "baseline"

    def score(key: str) -> float:
        try:
            return min(1.0, max(0.0, float(data.get(key, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    return {
        "winner": winner,
        "baseline_score": score("baseline_score"),
        "challenger_score": score("challenger_score"),
        "reason": str(data.get("reason") or "")[:300],
    }


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
