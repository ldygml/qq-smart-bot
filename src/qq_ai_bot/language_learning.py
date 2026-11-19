from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .llm import ChatClient


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(reply: str) -> dict[str, Any]:
    value = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", reply.strip(), flags=re.I)
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _clean_text(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_meme_phrase(value: object) -> str:
    phrase = _clean_text(value, 32).strip("“”\"'`《》<>")
    if not 2 <= len(phrase) <= 20:
        return ""
    if re.search(r"https?://|@\w|\d{5,}", phrase):
        return ""
    harmful = ("傻逼", "脑残", "去死", "滚你", "尼玛", "妈的", "贱人", "废物")
    if any(marker in phrase.lower() for marker in harmful):
        return ""
    return phrase


@dataclass(frozen=True)
class LanguageProfile:
    summary: str = ""
    rhythm: str = ""
    vocabulary: str = ""
    avoid: str = ""


@dataclass(frozen=True)
class ExpressionCandidate:
    phrase: str
    meaning: str = ""
    usage: str = ""
    tone: str = ""
    confidence: float = 0.5
    source_title: str = ""
    source_url: str = ""
    source_kind: str = "chat"


@dataclass(frozen=True)
class LanguageLearningResult:
    profile: LanguageProfile
    expressions: tuple[ExpressionCandidate, ...]


class LanguageLearningStore:
    """Scoped language observations, speaking profiles and meme usage cards."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        enabled: bool = True,
        web_enabled: bool = True,
        meme_usage_rate: float = 0.22,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initial_settings = {
            "enabled": "true" if enabled else "false",
            "web_learning_enabled": "true" if web_enabled else "false",
            "meme_usage_rate": str(
                min(1.0, max(0.0, float(meme_usage_rate)))
            ),
        }
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS language_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL,
                    scope_id INTEGER NOT NULL,
                    scope_name TEXT NOT NULL DEFAULT '',
                    speaker_user_id INTEGER NOT NULL,
                    speaker_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_language_observations_pending
                ON language_observations (processed, attempts, id ASC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS language_profiles (
                    scope_type TEXT NOT NULL,
                    scope_id INTEGER NOT NULL,
                    scope_name TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    rhythm TEXT NOT NULL DEFAULT '',
                    vocabulary TEXT NOT NULL DEFAULT '',
                    avoid TEXT NOT NULL DEFAULT '',
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_type, scope_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS language_expressions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL,
                    scope_id INTEGER NOT NULL,
                    scope_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'slang',
                    phrase TEXT NOT NULL,
                    meaning TEXT NOT NULL DEFAULT '',
                    usage TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL DEFAULT 'chat',
                    source_title TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'active',
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT,
                    updated_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_language_expressions_scope
                ON language_expressions
                    (scope_type, scope_id, status, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS language_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            defaults = {
                **self._initial_settings,
                "auto_profile": "true",
                "last_chat_learned_at": "",
                "last_web_learned_at": "",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO language_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def setting(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM language_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else default

    def setting_bool(self, key: str, default: bool = False) -> bool:
        value = self.setting(key, "true" if default else "false").lower()
        return value in {"1", "true", "yes", "on"}

    def setting_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.setting(key, str(default)))
        except ValueError:
            return default

    def set_setting(self, key: str, value: object) -> None:
        allowed = {
            "enabled",
            "web_learning_enabled",
            "meme_usage_rate",
            "auto_profile",
            "last_chat_learned_at",
            "last_web_learned_at",
        }
        if key not in allowed:
            raise ValueError("unsupported language learning setting")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO language_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    def observe(
        self,
        *,
        scope_type: str,
        scope_id: int,
        scope_name: str,
        speaker_user_id: int,
        speaker_name: str,
        content: str,
    ) -> int | None:
        if scope_type not in {"contact", "group"}:
            return None
        value = _clean_text(content, 2000)
        if len(value) < 4 or value.startswith("/"):
            return None
        if value.startswith(("[图片]", "[语音]", "[表情包]", "[文件:")):
            return None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO language_observations
                    (
                        scope_type, scope_id, scope_name, speaker_user_id,
                        speaker_name, content, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_type,
                    int(scope_id),
                    _clean_text(scope_name, 120),
                    int(speaker_user_id),
                    _clean_text(speaker_name, 120),
                    value,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def expressions(
        self,
        *,
        scope_type: str | None = None,
        scope_id: int | None = None,
        limit: int = 200,
        include_blocked: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        parameters: list[object] = []
        if scope_type is not None:
            clauses.append("scope_type = ?")
            parameters.append(scope_type)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            parameters.append(int(scope_id))
        if not include_blocked:
            clauses.append("status = 'active'")
        parameters.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM language_expressions
                WHERE {' AND '.join(clauses)}
                ORDER BY status ASC, updated_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_expression(
        self,
        expression_id: int,
        *,
        meaning: str | None = None,
        usage: str | None = None,
        status: str | None = None,
    ) -> bool:
        updates: list[str] = ["updated_at = ?"]
        parameters: list[object] = [_utc_now()]
        if meaning is not None:
            updates.append("meaning = ?")
            parameters.append(_clean_text(meaning, 500))
        if usage is not None:
            updates.append("usage = ?")
            parameters.append(_clean_text(usage, 500))
        if status is not None:
            normalized = status if status in {"active", "blocked"} else "active"
            updates.append("status = ?")
            parameters.append(normalized)
        parameters.append(int(expression_id))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE language_expressions SET {', '.join(updates)} WHERE id = ?",
                parameters,
            )
            return cursor.rowcount > 0

    def delete_expression(self, expression_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM language_expressions WHERE id = ?",
                (int(expression_id),),
            )
            return cursor.rowcount > 0

    def delete_scope(self, scope_type: str, scope_id: int) -> dict[str, int]:
        if scope_type not in {"contact", "group"}:
            raise ValueError("unsupported language scope")
        with self._lock, self._connect() as connection:
            observations = connection.execute(
                "DELETE FROM language_observations WHERE scope_type = ? AND scope_id = ?",
                (scope_type, int(scope_id)),
            ).rowcount
            expressions = connection.execute(
                "DELETE FROM language_expressions WHERE scope_type = ? AND scope_id = ?",
                (scope_type, int(scope_id)),
            ).rowcount
            profiles = connection.execute(
                "DELETE FROM language_profiles WHERE scope_type = ? AND scope_id = ?",
                (scope_type, int(scope_id)),
            ).rowcount
        return {
            "observations": observations,
            "expressions": expressions,
            "profiles": profiles,
        }

    def context_for(
        self,
        *,
        scope_type: str,
        scope_id: int,
        query: str,
        include_slang: bool = True,
        limit: int = 5,
    ) -> str:
        if not self.setting_bool("enabled", True):
            return ""
        profile = self.profile(scope_type, scope_id)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM language_expressions
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (
                    (scope_type = ? AND scope_id = ?)
                    OR (scope_type = 'global' AND scope_id = 0)
                  )
                ORDER BY updated_at DESC, seen_count DESC
                LIMIT 60
                """,
                (now, scope_type, int(scope_id)),
            ).fetchall()
        candidates = [dict(row) for row in rows]
        query_value = query.lower()
        candidates.sort(
            key=lambda item: (
                4 if str(item["phrase"]).lower() in query_value else 0,
                2
                if item["scope_type"] == scope_type
                and int(item["scope_id"]) == int(scope_id)
                else 0,
                float(item["confidence"]),
                int(item["seen_count"]),
            ),
            reverse=True,
        )
        lines: list[str] = []
        if profile:
            summary = str(profile.get("summary") or "").strip()
            rhythm = str(profile.get("rhythm") or "").strip()
            vocabulary = str(profile.get("vocabulary") or "").strip()
            avoid = str(profile.get("avoid") or "").strip()
            if summary or rhythm or vocabulary or avoid:
                lines.append(
                    "从近期真实聊天观察到的表达习惯（只模仿节奏和自然度，"
                    "不要复制任何人的独特原话，也不要点破你在观察）："
                )
                if summary:
                    lines.append(f"- 总体：{summary}")
                if rhythm:
                    lines.append(f"- 节奏：{rhythm}")
                if vocabulary:
                    lines.append(f"- 常用表达倾向：{vocabulary}")
                if avoid:
                    lines.append(f"- 这个场景不适合：{avoid}")
        if include_slang and candidates:
            lines.append(
                "可自然理解的近期表达（一次回复最多顺手用一个；"
                "语义不贴合、可能冒犯或像硬玩梗时宁可不用，也不要解释“我在用梗”）："
            )
            for item in candidates[: max(1, limit)]:
                meaning = str(item.get("meaning") or "").strip()
                usage = str(item.get("usage") or "").strip()
                tone = str(item.get("tone") or "").strip()
                detail = "；".join(
                    value
                    for value in (meaning, usage, f"语气：{tone}" if tone else "")
                    if value
                )
                lines.append(f"- {item['phrase']}：{detail or '近期聊天中的口语表达'}")
        return "\n".join(lines)

    def list_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM language_profiles
                ORDER BY updated_at DESC, evidence_count DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_observations(self, *, limit: int = 120) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, scope_type, scope_id, scope_name,
                       speaker_user_id, speaker_name, content, attempts,
                       created_at
                FROM language_observations
                WHERE processed = 0 AND attempts < 3
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, observation_ids: list[int]) -> None:
        if not observation_ids:
            return
        placeholders = ",".join("?" for _ in observation_ids)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE language_observations SET processed = 1 WHERE id IN ({placeholders})",
                observation_ids,
            )

    def mark_failed(self, observation_ids: list[int]) -> None:
        if not observation_ids:
            return
        placeholders = ",".join("?" for _ in observation_ids)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                UPDATE language_observations
                SET attempts = attempts + 1,
                    processed = CASE WHEN attempts + 1 >= 3 THEN 1 ELSE 0 END
                WHERE id IN ({placeholders})
                """,
                observation_ids,
            )

    def profile(
        self,
        scope_type: str,
        scope_id: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM language_profiles WHERE scope_type = ? AND scope_id = ?",
                (scope_type, int(scope_id)),
            ).fetchone()
        return dict(row) if row else None

    def upsert_profile(
        self,
        *,
        scope_type: str,
        scope_id: int,
        scope_name: str,
        profile: LanguageProfile,
        evidence_count: int,
    ) -> None:
        summary = _clean_text(profile.summary, 1200)
        rhythm = _clean_text(profile.rhythm, 600)
        vocabulary = _clean_text(profile.vocabulary, 600)
        avoid = _clean_text(profile.avoid, 600)
        if not any((summary, rhythm, vocabulary, avoid)):
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO language_profiles
                    (
                        scope_type, scope_id, scope_name, summary, rhythm,
                        vocabulary, avoid, evidence_count, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                    scope_name = excluded.scope_name,
                    summary = excluded.summary,
                    rhythm = excluded.rhythm,
                    vocabulary = excluded.vocabulary,
                    avoid = excluded.avoid,
                    evidence_count = language_profiles.evidence_count
                        + excluded.evidence_count,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_type,
                    int(scope_id),
                    _clean_text(scope_name, 120),
                    summary,
                    rhythm,
                    vocabulary,
                    avoid,
                    max(1, int(evidence_count)),
                    _utc_now(),
                ),
            )

    def upsert_expression(
        self,
        *,
        scope_type: str,
        scope_id: int,
        scope_name: str,
        kind: str,
        candidate: ExpressionCandidate,
        ttl_days: int,
    ) -> int | None:
        phrase = _safe_meme_phrase(candidate.phrase)
        if not phrase:
            return None
        normalized_kind = "style" if kind == "style" else "slang"
        fingerprint = hashlib.sha256(
            f"{scope_type}:{scope_id}:{normalized_kind}:{phrase}".encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        expires_at = (
            (datetime.now(timezone.utc) + timedelta(days=max(1, ttl_days))).isoformat()
            if normalized_kind == "slang"
            else None
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT id, status FROM language_expressions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None and existing["status"] == "blocked":
                return int(existing["id"])
            confidence = min(1.0, max(0.0, float(candidate.confidence)))
            if existing is not None:
                connection.execute(
                    """
                    UPDATE language_expressions
                    SET scope_name = ?, meaning = ?, usage = ?, tone = ?,
                        source_kind = ?, source_title = ?, source_url = ?,
                        confidence = MAX(confidence, ?), seen_count = seen_count + 1,
                        last_seen_at = ?, expires_at = COALESCE(?, expires_at),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _clean_text(scope_name, 120),
                        _clean_text(candidate.meaning, 500),
                        _clean_text(candidate.usage, 500),
                        _clean_text(candidate.tone, 120),
                        _clean_text(candidate.source_kind, 40) or "chat",
                        _clean_text(candidate.source_title, 300),
                        _clean_text(candidate.source_url, 1000),
                        confidence,
                        now,
                        expires_at,
                        now,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO language_expressions
                    (
                        scope_type, scope_id, scope_name, kind, phrase,
                        meaning, usage, tone, source_kind, source_title,
                        source_url, confidence, status, seen_count,
                        first_seen_at, last_seen_at, expires_at, updated_at,
                        fingerprint
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)
                """,
                (
                    scope_type,
                    int(scope_id),
                    _clean_text(scope_name, 120),
                    normalized_kind,
                    phrase,
                    _clean_text(candidate.meaning, 500),
                    _clean_text(candidate.usage, 500),
                    _clean_text(candidate.tone, 120),
                    _clean_text(candidate.source_kind, 40) or "chat",
                    _clean_text(candidate.source_title, 300),
                    _clean_text(candidate.source_url, 1000),
                    confidence,
                    now,
                    now,
                    expires_at,
                    now,
                    fingerprint,
                ),
            )
            return int(cursor.lastrowid)

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM language_observations WHERE processed = 0 AND attempts < 3"
            ).fetchone()[0]
            profiles = connection.execute(
                "SELECT COUNT(*) FROM language_profiles"
            ).fetchone()[0]
            expressions = connection.execute(
                "SELECT COUNT(*) FROM language_expressions WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?)",
                (_utc_now(),),
            ).fetchone()[0]
            learned = connection.execute(
                "SELECT COUNT(*) FROM language_observations WHERE processed = 1"
            ).fetchone()[0]
        return {
            "pending": int(pending),
            "profiles": int(profiles),
            "expressions": int(expressions),
            "learned": int(learned),
        }


async def learn_language_batch(
    chat_client: ChatClient,
    *,
    model: str,
    scope_label: str,
    observations: list[dict[str, Any]],
    current_profile: dict[str, Any] | None = None,
) -> LanguageLearningResult:
    transcript = "\n".join(
        f"- {_clean_text(item.get('speaker_name'), 40)}："
        f"{_clean_text(item.get('content'), 500)}"
        for item in observations[-80:]
    )
    current = current_profile or {}
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是 QQ 群聊语言观察员，只分析表达习惯，不总结人物隐私，"
                    "也不判断聊天内容里的客观事实。目标是在不冒充他人的前提下，"
                    "让聊天机器人更自然地适应当前场景。"
                    "只能总结多人身上可复用的节奏：句子长短、分段、语气词、"
                    "接梗速度、标点力度、先接情绪还是先回答、是否打字简短。"
                    "不要复制某个人的独特口头禅，不输出姓名、QQ号、地址、"
                    "联系方式或私事。网络词只有在有明确含义和用法时才收录。"
                    "辱骂、歧视、黄腔、引战词和一次性错别字一律不收录。"
                    "严格输出 JSON，不要 Markdown。"
                ),
            },
            {
                "role": "user",
                "content": (
                    '返回 {"profile":{"summary":"不超过120字",'
                    '"rhythm":"回复长度和节奏","vocabulary":"可复用的口语倾向",'
                    '"avoid":"不适合的表达"},'
                    '"expressions":[{"phrase":"2到12字","meaning":"含义",'
                    '"usage":"适合什么语境","tone":"语气","confidence":0到1}]}。'
                    "expressions 最多 4 个，没有可靠新词就返回空数组。"
                    f"\n当前场景：{scope_label}"
                    f"\n已有画像：{_clean_text(current.get('summary'), 600)}"
                    f"\n近期聊天：\n{transcript}"
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
    raw_profile = data.get("profile")
    profile_data = raw_profile if isinstance(raw_profile, dict) else {}
    profile = LanguageProfile(
        summary=_clean_text(profile_data.get("summary"), 1200),
        rhythm=_clean_text(profile_data.get("rhythm"), 600),
        vocabulary=_clean_text(profile_data.get("vocabulary"), 600),
        avoid=_clean_text(profile_data.get("avoid"), 600),
    )
    expressions: list[ExpressionCandidate] = []
    raw_expressions = data.get("expressions")
    if isinstance(raw_expressions, list):
        for item in raw_expressions[:6]:
            if not isinstance(item, dict):
                continue
            phrase = _safe_meme_phrase(item.get("phrase"))
            if not phrase:
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            expressions.append(
                ExpressionCandidate(
                    phrase=phrase,
                    meaning=_clean_text(item.get("meaning"), 500),
                    usage=_clean_text(item.get("usage"), 500),
                    tone=_clean_text(item.get("tone"), 120),
                    confidence=min(1.0, max(0.0, confidence)),
                    source_kind="chat",
                )
            )
    return LanguageLearningResult(profile=profile, expressions=tuple(expressions))


async def extract_trending_expressions(
    chat_client: ChatClient,
    *,
    model: str,
    research: str,
    sources: list[dict[str, str]],
    current_date: str,
) -> tuple[ExpressionCandidate, ...]:
    source_text = "\n".join(
        f"- {_clean_text(item.get('title'), 160)}：{_clean_text(item.get('url'), 1000)}"
        for item in sources[:6]
    )
    reply = await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "从给定联网资料中提取近期中文网络口语。不能使用资料外知识，"
                    "不能编造出处。只保留含义和用法清楚、不是侮辱攻击、"
                    "不是一次性缩写、适合正常聊天的词。严格输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"日期：{current_date}\n资料：\n{research[:10000]}\n"
                    f"来源：\n{source_text}\n"
                    '返回 {"expressions":[{"phrase":"2到12字","meaning":"含义",'
                    '"usage":"自然使用场景","tone":"语气","confidence":0到1,'
                    '"source_index":1}]}，最多 4 个；没有可靠内容就返回空数组。'
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
    result: list[ExpressionCandidate] = []
    raw_expressions = data.get("expressions")
    if not isinstance(raw_expressions, list):
        return ()
    for item in raw_expressions[:6]:
        if not isinstance(item, dict):
            continue
        phrase = _safe_meme_phrase(item.get("phrase"))
        if not phrase or phrase not in research:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
            source_index = int(item.get("source_index", 1)) - 1
        except (TypeError, ValueError):
            confidence = 0.5
            source_index = 0
        if not sources:
            continue
        source = sources[min(max(source_index, 0), len(sources) - 1)]
        result.append(
            ExpressionCandidate(
                phrase=phrase,
                meaning=_clean_text(item.get("meaning"), 500),
                usage=_clean_text(item.get("usage"), 500),
                tone=_clean_text(item.get("tone"), 120),
                confidence=min(1.0, max(0.0, confidence)),
                source_title=_clean_text(source.get("title"), 300),
                source_url=_clean_text(source.get("url"), 1000),
                source_kind="web",
            )
        )
    return tuple(result)

