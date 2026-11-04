from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class VectorChunk:
    namespace: str
    source_id: str
    title: str
    location: str
    content: str


@dataclass(frozen=True)
class VectorMatch:
    namespace: str
    source_id: str
    title: str
    location: str
    content: str
    score: float
    metadata: dict[str, Any]


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        trust_env: bool = False,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.post(
            self._url,
            json={"model": self._model, "input": texts},
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Embedding API returned an unexpected response")
        embeddings: list[list[float]] = []
        for item in data:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                raise ValueError("Embedding API returned an invalid vector")
            embeddings.append([float(value) for value in vector])
        return embeddings

    async def close(self) -> None:
        await self._client.aclose()


class VectorMemoryStore:
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
                CREATE TABLE IF NOT EXISTS vector_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    location TEXT NOT NULL,
                    content TEXT NOT NULL,
                    chunk_hash TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(namespace, source_id, chunk_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_vector_namespace
                ON vector_chunks(namespace, source_id);
                """
            )

    def upsert_chunks(
        self,
        chunks: list[VectorChunk],
        embeddings: list[list[float]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError("Chunk and embedding counts do not match")
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        written = 0
        with self._lock, self._connect() as connection:
            for chunk, embedding in zip(chunks, embeddings):
                digest = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO vector_chunks
                        (
                            namespace, source_id, title, location, content,
                            chunk_hash, embedding, metadata
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.namespace,
                        chunk.source_id,
                        chunk.title,
                        chunk.location,
                        chunk.content,
                        digest,
                        json.dumps(embedding),
                        metadata_json,
                    ),
                )
                written += cursor.rowcount
        return written

    def search(
        self,
        embedding: list[float],
        *,
        namespaces: tuple[str, ...] | None = None,
        limit: int = 5,
        min_score: float = 0.2,
    ) -> list[VectorMatch]:
        query_norm = _norm(embedding)
        if query_norm == 0:
            return []
        with self._lock, self._connect() as connection:
            if namespaces:
                placeholders = ",".join("?" for _ in namespaces)
                rows = connection.execute(
                    f"""
                    SELECT namespace, source_id, title, location, content,
                           embedding, metadata
                    FROM vector_chunks
                    WHERE namespace IN ({placeholders})
                    """,
                    namespaces,
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT namespace, source_id, title, location, content,
                           embedding, metadata
                    FROM vector_chunks
                    """
                ).fetchall()
        matches: list[VectorMatch] = []
        for row in rows:
            try:
                stored = [float(value) for value in json.loads(row["embedding"])]
                stored_norm = _norm(stored)
                if stored_norm == 0:
                    continue
                score = sum(a * b for a, b in zip(embedding, stored))
                score /= query_norm * stored_norm
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if score < min_score:
                continue
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                metadata = {}
            matches.append(
                VectorMatch(
                    namespace=str(row["namespace"]),
                    source_id=str(row["source_id"]),
                    title=str(row["title"]),
                    location=str(row["location"]),
                    content=str(row["content"]),
                    score=score,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            )
        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[: max(1, limit)]

    def has_source(self, namespace: str, source_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM vector_chunks
                WHERE namespace = ? AND source_id = ?
                LIMIT 1
                """,
                (namespace, source_id),
            ).fetchone()
        return row is not None

    def delete_source(self, namespace: str, source_id: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM vector_chunks
                WHERE namespace = ? AND source_id = ?
                """,
                (namespace, source_id),
            )
            return cursor.rowcount

    def delete_sources(self, namespace: str, source_ids: list[str]) -> int:
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM vector_chunks
                WHERE namespace = ? AND source_id IN ({placeholders})
                """,
                [namespace, *source_ids],
            )
            return cursor.rowcount

    def list_sources(
        self,
        namespace: str | None = None,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where = "WHERE namespace = ?" if namespace else ""
        parameters: list[Any] = [namespace] if namespace else []
        parameters.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT namespace, source_id, MIN(title) AS title,
                       MIN(location) AS location,
                       MIN(created_at) AS created_at,
                       COUNT(*) AS chunk_count,
                       MAX(metadata) AS metadata
                FROM vector_chunks
                {where}
                GROUP BY namespace, source_id
                ORDER BY MIN(created_at) DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        sources: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(str(item.get("metadata") or "{}"))
            except json.JSONDecodeError:
                item["metadata"] = {}
            sources.append(item)
        return sources

    def count(self, namespace: str | None = None) -> int:
        with self._lock, self._connect() as connection:
            if namespace:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM vector_chunks WHERE namespace = ?",
                    (namespace,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM vector_chunks"
                ).fetchone()
        return int(row["total"]) if row else 0


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))
